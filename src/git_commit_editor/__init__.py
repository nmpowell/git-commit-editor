"""Git Commit Message Editor: a local web app for rewording the commits on a branch."""

from importlib.metadata import PackageNotFoundError, version

from .app import main

try:
    __version__ = version("git-commit-editor")
except PackageNotFoundError:  # a source tree that has not been installed
    __version__ = "0+unknown"

__all__ = ["__version__", "main"]
