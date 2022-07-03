import asyncio
import re
import time
from typing import Tuple, List

import click
import psutil
from httpx import AsyncClient, Response, __version__
from rich.markup import escape
from rich.progress import Progress, DownloadColumn, TransferSpeedColumn
from rich.console import Console
from pathlib import Path
from io import BytesIO


USER_AGENTS = {
    "chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127"
    " Safari/537.36",
    "chromium": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Ubuntu Chromium/83.0.4103.61 "
    "Chrome/83.0.4103.61 Safari/537.36",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; rv:102.0) Gecko/20100101 Firefox/102.0",
    "safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/15.3 Safari/605.1.15",
    "default": "python-httpx/" + __version__,
}


async def check_ram(console: Console, event: asyncio.Event, warning_at: int = 1):
    worry_level = 1024**3 * warning_at
    while not event.is_set():
        result = await asyncio.to_thread(psutil.virtual_memory)
        if result.available < worry_level:
            console.log(
                "[red]Warning: Less than %d gigabyte%s of memory is available!"
                % (warning_at, "s" if warning_at != 1 else "")
            )
        await asyncio.sleep(5)


async def downloader(
    client: AsyncClient,
    progress: Progress,
    url: str,
    file: Path = None,
    directory: Path = Path.cwd(),
    *,
    authorisation: Tuple[str, str] = None,
    buffer: bool = True,
):
    task = progress.add_task("Get %s - headers" % url, completed=50)
    async with client.stream("GET", url, auth=authorisation) as response:
        response: Response
        if response.status_code not in range(200, 300):
            progress.update(
                task,
                total=1,
                completed=1,
                description="Get %s - status %d" % (response.url.netloc.decode(), response.status_code),
            )
            return

        if file is None:
            url = response.url.path
            fp = url.split("/")[-1]
            file = directory / fp
            file = file.with_name(re.sub(r"\W", "-", file.name))
            progress.console.log(f"Saving {escape(repr(url))} to [blue][link=file://{file.absolute()}]{file.name}[/].")

        if not file.suffix and response.headers.get("Content-Type"):
            ct = response.headers["Content-Type"].split(";")[0].split("/")[1]
            try:
                file = file.with_suffix(ct)
            except ValueError:
                progress.console.log(f"[orange]Failed to detect file suffix for {file.name}.")
                file = file.with_suffix("")

        try:
            length = response.headers.get("Content-Length")
            if length is not None:
                length = int(length)
            else:
                progress.console.log(
                    f"[red bold]{response.url.path} did not return a content length! "
                    f"Download time is unknown and ETA will be unavailable."
                )
        except (TypeError, ValueError):
            progress.update(task, total=1, completed=1, description="Get %s - failed!" % response.url.netloc.decode())
        else:
            progress.update(
                task,
                total=length,
                completed=0,
                description="Get %s (%r)"
                % (
                    response.url.netloc.decode(),
                    file.name,
                ),
                pulse=True,
            )

            if buffer:
                write_file = BytesIO()
            else:
                write_file = file.open("wb+")

            try:
                async for chunk in response.aiter_bytes(256):
                    write_file.write(chunk)
                    # noinspection PyProtectedMember
                    if progress._tasks[task].total <= response.num_bytes_downloaded:
                        _total = response.num_bytes_downloaded
                    else:
                        # noinspection PyProtectedMember
                        _total = progress._tasks[task].total
                    progress.update(
                        task,
                        total=_total,
                        completed=response.num_bytes_downloaded,
                        description="Get %s (%r)"
                        % (
                            response.url.netloc.decode(),
                            file.name,
                        ),
                        pulse=True,
                    )
            except (KeyboardInterrupt, RuntimeError):
                write_file.close()
                await response.aclose()
                return
            write_file.flush()
            if buffer:
                with file.open("wb+") as foo:
                    foo.write(write_file.read())
            write_file.close()


@click.command()
@click.argument("urls", nargs=-1)
@click.option("--username", default=None)
@click.option("--password", default=None, help="The password to access websites (only sent if requested)")
@click.option("--buffer", default=False, help="Whether to write data to memory before disk (faster)")
@click.option("--output-directory", "-O", type=Path, default=Path.cwd())
@click.option("--user-agent", default="default", help="Which user agent to select. Can be custom or a browser name.")
@click.option(
    "--ram-warning-at",
    type=int,
    default=1,
    help="At what point to worry about ram usage " "(display a warning when there is less than this much ram free)",
)
@click.option(
    "--from-list",
    "--from-file",
    default=None,
    type=Path,
    help="A file containing a list of URLs to grab. One URL per line. Defaults to None.",
)
def download_file(
    urls: List[str],
    username: str = None,
    password: str = None,
    output_directory: Path = Path.cwd(),
    buffer: bool = False,
    user_agent: str = "default",
    ram_warning_at: int = 1,
    from_list: Path = None,
):
    console = Console()
    if from_list is not None:
        with from_list.open("r") as _file:
            urls = list(filter(lambda ln: bool(ln), _file.readlines()))
            console.log(
                f"Loaded [green]{len(urls)}[/] urls from [file={from_list.absolute().as_uri()}]{from_list.name}"
            )

    ua = USER_AGENTS.get(user_agent.lower().strip())
    if ua is None:
        if len(user_agent.split(" ")) == 1:
            console.log(
                "Warning: it appears you tried to select a preset user-agent that does not exist. The only presets"
                " available are: firefox, chrome, safari. If this is a false positive, you can ignore this message."
                " Otherwise, you have 3 seconds to cancel (keyboard interrupt) before the downloader starts."
            )
            try:
                time.sleep(3)
            except KeyboardInterrupt:
                return
        ua = user_agent
    else:
        console.log("Using the user agent %r" % user_agent.lower())
    if buffer:
        console.log(
            "Notice: Data will be written to data before writing to disk. "
            "If the download is interrupted, data will be lost!"
        )
    if type(username) != type(password):
        console.log("If username is provided, password must also be, and vice-versa.")
        return
    elif username is None and password is None:
        auth = None
    else:
        auth = (username, password)

    async def run():
        threads = []
        timer = None
        timer_event = asyncio.Event()
        async with AsyncClient(http2=True, headers={"User-Agent": ua}) as client:
            with Progress(
                *Progress.get_default_columns(),
                DownloadColumn(),
                TransferSpeedColumn(),
                speed_estimate_period=10,
                refresh_per_second=5,
                expand=True,
                console=console,
            ) as progress:
                if buffer:
                    timer = asyncio.create_task(check_ram(progress.console, timer_event, ram_warning_at))
                for url in urls:
                    url = url.strip()
                    task = asyncio.create_task(
                        downloader(client, progress, url, authorisation=auth, directory=output_directory, buffer=buffer)
                    )
                    threads.append(task)
                try:
                    await asyncio.wait(threads, timeout=None, return_when=asyncio.ALL_COMPLETED)
                except (KeyboardInterrupt, asyncio.CancelledError, RuntimeError):
                    for task in progress.tasks:
                        progress.update(
                            task.id,
                            total=task.total,
                            completed=task.total,
                            description=task.description + " (cancelled)",
                        )
                    progress.stop()
                    for thread in threads:
                        try:
                            if thread.exception():
                                pass
                            else:
                                thread.cancel()
                        except asyncio.InvalidStateError:
                            pass
                finally:
                    timer_event.set()
                    if timer:
                        timer.cancel()

    try:
        asyncio.run(run())
    except RuntimeError:
        pass


if __name__ == "__main__":
    download_file()
