from setuptools import setup
from subprocess import run
from sys import stdout


with open("./requirements.txt") as requirements_txt:
    requirements = requirements_txt.read().splitlines()

setup(
    name="cool-downloader",
    version=run(
        ("git", "rev-parse", "--short", "HEAD"), capture_output=True, encoding="utf-8", check=True
    ).stdout.strip(),
    packages=["downloader"],
    url="https://github.com/EEKIM10/cool-downloader",
    license="MIT",
    author="nexy7574",
    author_email="",
    description="I got bored and decided I wanted a cool looking downloader.",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "cdl = downloader.main:cli_main",
            "cdownload = downloader.main:cli_main",
            "cdownloader = downloader.main:cli_main",
        ]
    },
)
