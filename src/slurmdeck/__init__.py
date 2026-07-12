"""SlurmDeck: end-to-end workflow manager for Slurm clusters."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("slurmdeck")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0+unknown"
