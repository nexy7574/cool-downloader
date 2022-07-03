from setuptools import setup


with open("./requirements.txt") as requirements_txt:
    requirements = requirements_txt.readlines()

setup(
    name='cool-downloader',
    version='1.0.0',
    packages=['downloader'],
    url='https://github.com/EEKIM10/cool-downloader',
    license='MIT',
    author='nexy7574',
    author_email='',
    description='I got bored and decided I wanted a cool looking downloader.',
    requires=requirements,
    entry_points={
        "console_scripts": [
            "cdl = downloader.main:download_file",
            "cdownload = downloader.main:download_file",
            "cdownloader = downloader.main:download_file"
        ]
    }

)
