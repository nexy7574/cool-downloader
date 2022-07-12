import asyncio
import os
import re
import time
import sys
import warnings
from typing import Tuple, List
from urllib.parse import urlparse

import click
import psutil
from click.exceptions import Exit, Abort
from httpx import AsyncClient, Response, __version__, Timeout, TimeoutException, ReadError, ConnectError
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
WARNING_DISPLAYED = False


async def check_ram(console: Console, event: asyncio.Event, warning_at: int = 1):
    global WARNING_DISPLAYED
    worry_level = 1024**3 * warning_at
    while not event.is_set():
        result = await asyncio.to_thread(psutil.virtual_memory)
        if result.available < worry_level:
            if WARNING_DISPLAYED is False:
                console.log(
                    "[red]Memory warning: Less than %d gigabyte%s of memory is available! (%.2fGB Free)"
                    % (warning_at, "s" if warning_at != 1 else "", result.available / 1024**3)
                )
                WARNING_DISPLAYED = True
        else:
            if WARNING_DISPLAYED:
                console.log("[green]Memory warning resolved.")
                WARNING_DISPLAYED = False
        await asyncio.sleep(5)


async def requires_authentication(client: AsyncClient, url: str, *, method: str = "GET", **kwargs) -> bool:
    if kwargs.pop("try_head_first", False) is True:
        response = await client.head(url, **kwargs)
        if response.status_code == 401 and response.headers.get("WWW-Authenticate", "").startswith("Basic"):
            return True

    async with client.stream(method, url, **kwargs) as response:
        await response.aclose()
        if response.status_code == 401 and response.headers.get("WWW-Authenticate", "").startswith("Basic"):
            # We only support basic auth rn
            return True
        return False


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
    _url = urlparse(url)
    display_domain = _url.hostname
    task = progress.add_task("Check authentication status of  %s" % display_domain, total=0, completed=0, start=False)
    req = client.stream("GET", url, auth=authorisation)
    if await requires_authentication(client, url, try_head_first=True):
        if not authorisation:
            progress.stop()
            progress.console.clear()
            progress.console.log("%s requires authentication. Please input a username and password." % display_domain)
            username = progress.console.input("Username: ")
            password = progress.console.input("Password: ", password=True)
            progress.start()
            authorisation = (username, password)
        progress.console.log("%s requires authentication. Sending basic auth." % display_domain)
        del req
        req = client.stream("GET", url, auth=authorisation)
    else:
        progress.console.log("%s does not require authentication." % display_domain)
    try:
        async with req as response:
            progress.start_task(task)
            response: Response
            if response.status_code not in range(200, 300):
                progress.update(
                    task,
                    total=1,
                    completed=1,
                    description="Get %s - [red]status %d[/]" % (response.url.host, response.status_code),
                )
                return

            display_domain = response.url.host
            if response.history:
                for historical_response in reversed(response.history):
                    historical_response: Response
                    progress.console.log(
                        "{} -> {}".format(
                            escape(str(historical_response.url)),
                            escape(str((historical_response.history or [response])[-1].url)),
                        )
                    )
                    if historical_response.url.host not in display_domain:
                        display_domain += "/" + historical_response.url.host

            # Now we need to perform some black magic fuckery
            display_domain = ">".join(reversed(display_domain.split("/")))
            # ^ This just reverses the order of the domains so that they're in the correct order

            if file is None:
                url = response.url.path
                fp = url.split("/")[-1]
                file = directory / fp
                file = file.with_name(re.sub(r"[^\w\-.]", "-", file.name))
                progress.console.log(
                    f"Saving {escape(repr(url))} to [blue][link={file.absolute().as_uri()}]{file.name}[/]."
                )

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
                progress.update(task, total=1, completed=1, description="Get %s - failed!" % response.url.host)
            else:
                progress.update(
                    task,
                    total=length,
                    completed=0,
                    description="Get %s (%r)"
                    % (
                        display_domain,
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
                                display_domain,
                                file.name,
                            ),
                            pulse=True,
                        )
                except (KeyboardInterrupt, RuntimeError):
                    progress.console.log(f"Cancelling download {file.name!r}")
                    write_file.close()
                    try:
                        await response.aclose()
                    except (Exception, RuntimeError):
                        response.close()
                    return
                except (ReadError, TimeoutException, ConnectError) as e:
                    progress.remove_task(task)
                    progress.console.log(f"[red]Fatal error downloading {file.name}: {repr(e)}")
                    write_file.close()
                    try:
                        await response.aclose()
                    except (Exception, RuntimeError):
                        response.close()
                    return
                write_file.flush()
                if buffer:
                    with file.open("wb+") as foo:
                        foo.write(write_file.read())
                write_file.close()
    except (ReadError, TimeoutException, ConnectError) as e:
        if file is None:
            file = Path(os.urandom(6).hex())
        progress.remove_task(task)
        progress.console.log(f"[red]Fatal error downloading {file.name}: {repr(e)}")
        try:
            write_file.close()
        except UnboundLocalError:
            pass
        try:
            await response.aclose()
        except RuntimeError:
            response.close()
        except UnboundLocalError:
            pass
        return


@click.command()
@click.argument("urls", nargs=-1)
@click.option("--username", default=None)
@click.option("--password", default=None, help="The password to access websites (only sent if requested)")
@click.option("--buffer", default=False, is_flag=True, help="Whether to write data to memory before disk (faster)")
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
@click.option("--ignore-redirects", is_flag=True, default=True)
@click.option("--file-names", help="A list of comma-separated file names. Defaults to automatically generated")
@click.option(
    "--read-timeout", "-R", type=float, default=60.0, help="The maximum time spent waiting for a HTTP chunk to be sent"
)
@click.option(
    "--connect-timeout", "-C", type=float, default=10.0, help="The maximum time spent waiting for HTTP headers"
)
def cli_main(
    urls: List[str],
    username: str = None,
    password: str = None,
    output_directory: Path = Path.cwd(),
    buffer: bool = False,
    user_agent: str = "default",
    ram_warning_at: int = 1,
    from_list: Path = None,
    ignore_redirects: bool = False,
    file_names: str = None,
    read_timeout: float = 60.0,
    connect_timeout: float = 60.0,
):
    timeout = Timeout(
        timeout=(connect_timeout + read_timeout) * 2, connect=connect_timeout, read=read_timeout, write=60, pool=30
    )
    urls = list(urls)
    follow_redirects = not ignore_redirects
    console = Console()
    if from_list is not None:
        with from_list.open("r") as _file:
            urls += list(filter(lambda ln: bool(ln), _file.readlines()))
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
                raise Abort()
        ua = user_agent
    else:
        console.log("Using the user agent %r" % user_agent.lower())
    if buffer:
        console.log(
            "Notice: Data will be written to data before writing to disk. "
            "[u]If the download is interrupted, data will be lost!"
        )
    if type(username) != type(password):
        raise click.BadOptionUsage("username", "Username must be supplied with password or both omitted.")
    elif username is None and password is None:
        auth = None
    else:
        auth = (username, password)

    if not urls:
        raise click.BadArgumentUsage("No urls were provided")

    for _url in urls:
        if urls.count(_url) != 1:
            console.log("Duplicate URL %r - ignoring" % _url)
            urls.pop(urls.index(_url))

    if file_names:
        file_names = list(file_names.split(","))
        for _n, fn in enumerate(file_names):
            if fn == "-":
                file_names[_n] = None
        file_names += [None] * (len(urls) - len(file_names))
    else:
        file_names = [None] * len(urls)

    async def run():
        threads = []
        timer = None
        timer_event = asyncio.Event()
        async with AsyncClient(
            http2=True, headers={"User-Agent": ua}, follow_redirects=follow_redirects, timeout=timeout
        ) as client:
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
                for n, url in enumerate(urls):
                    url = url.strip()
                    if file_names[n] is not None:
                        path = Path(file_names[n])
                    else:
                        path = None
                    task = asyncio.create_task(
                        downloader(
                            client, progress, url, path, authorisation=auth, directory=output_directory, buffer=buffer
                        )
                    )
                    threads.append(task)
                try:
                    await asyncio.wait(threads, timeout=None, return_when=asyncio.ALL_COMPLETED)
                except (KeyboardInterrupt, asyncio.CancelledError, RuntimeError, Exception):
                    console.log("Cancelling...")
                    for task in progress.tasks:
                        if task.completed != task.total:
                            progress.update(
                                task.id,
                                total=task.total,
                                completed=task.total,
                                description=task.description + " (cancelled)",
                            )
                    progress.refresh()
                    progress.stop()
                    for thread in threads:
                        try:
                            if thread.exception():
                                pass
                            else:
                                thread.cancel()
                        except asyncio.InvalidStateError:
                            pass

                    for thread in threads:
                        await thread
                    raise Abort()
                finally:
                    timer_event.set()
                    if timer:
                        timer.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    warnings.simplefilter("ignore", Warning)
    try:
        cli_main()
    except RuntimeError:
        pass
