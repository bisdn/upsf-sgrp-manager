# BSD 3-Clause License
#
# Copyright (c) 2023, BISDN GmbH
# All rights reserved.

""" setup.py for package upsf_shard_manager """

#
# pylint: disable=R0201
# pylint: disable=W1514
#

import os
import pathlib
import setuptools

# To generate the egg file you need to type:
# sudo python setup.py bdist_egg --dist-dir build

# To install the egg file use easy_install, e.g.:
# sudo easy_install arpdaemon-0.1-py2.7.egg

# To remove the installed packet use pip, e.g.:
# sudo pip uninstall arpdaemon

# To clean the files created during building run:
# sudo python setup.py clean


def walk(path, match=None):
    """walk through a directory and its subdirectories yielding all files"""
    for _path in pathlib.Path(path).iterdir():
        if _path.is_dir():
            yield from walk(_path, match=match)
            continue
        if match is not None and not _path.match(match):
            continue
        yield _path.resolve()


def walkdir(path):
    """walk through a directory and its subdirectories yielding all directories"""
    for _path in pathlib.Path(path).iterdir():
        if not _path.is_dir():
            continue
        yield _path.resolve()


class CleanCommand(setuptools.Command):

    """Custom clean command to tidy up the project root."""

    user_options = []

    def initialize_options(self):
        """initialize_options"""

    def finalize_options(self):
        """finalize_options"""

    def run(self):
        """run"""
        os.system(  # nosec B605
            "/bin/rm -vrf ./build ./dist ./upsf_shard_manager.egg-info"
        )
        for filename in walk(pathlib.Path("."), match="*.pyc"):
            print(f"removing file {filename}")
            filename.unlink()


with open("README.md", "r") as file:
    long_description = file.read()

setuptools.setup(
    name="upsf_shard_manager",
    version="1.0.0",
    author="BISDN GmbH",
    author_email="info@bisdn.de",
    description="UPSF shard manager",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/bisdn/upsf-shard-manager.git",
    packages=setuptools.find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Programming Language :: Python :: 3.10",
        "License :: Other/Proprietary License",
        "Operating System :: OS Independent",
    ],
    include_package_data=True,
    package_data={},
    entry_points={
        "console_scripts": [
            "upsf-shard-manager=upsf_shard_manager.app:main",
        ]
    },
    zip_safe=False,
    cmdclass={"clean": CleanCommand},
)
