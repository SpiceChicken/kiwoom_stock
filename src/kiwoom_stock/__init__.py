"""Kiwoom stock monitoring package."""

from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("kiwoom_stock")
except PackageNotFoundError:
    __version__ = "0+unknown"


__all__ = ["__version__"]
