"""Interactive CLI test suite for Feetech STS3215 servos."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sts-suite")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
