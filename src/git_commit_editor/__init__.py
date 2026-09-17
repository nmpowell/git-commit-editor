"""Git Commit Message Editor: a local web app for rewording the commits on a branch."""

from .app import _dist_version, main

__version__ = _dist_version()

__all__ = ["__version__", "main"]
