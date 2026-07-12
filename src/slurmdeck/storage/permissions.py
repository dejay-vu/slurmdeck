"""Private local-state permissions shared by YAML and SQLite storage."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> None:
    """Create a local state directory and restrict an existing one."""
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(PRIVATE_DIRECTORY_MODE)


def ensure_private_file(path: Path) -> None:
    """Create an empty private file or restrict an existing file."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    except FileExistsError:
        path.chmod(PRIVATE_FILE_MODE)
    else:
        os.close(descriptor)


def restrict_file_if_present(path: Path) -> None:
    """Restrict an optional sidecar without creating it."""
    with suppress(FileNotFoundError):
        path.chmod(PRIVATE_FILE_MODE)
