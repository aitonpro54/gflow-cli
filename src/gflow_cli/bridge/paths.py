"""Conservative local paths: refuse network/device paths and link traversal."""

from __future__ import annotations

import ctypes
import os
import stat
from pathlib import Path


def local_path(path: Path) -> Path:
    """Inspect local ancestors before resolving; never probe a UNC/device target."""
    if ".." in path.parts:
        raise ValueError("parent traversal refused")
    raw = str(path)
    if raw.startswith(("\\\\", "//", "\\?\\", "\\.\\")) or not path.is_absolute():
        raise ValueError("local absolute path required")
    if os.name == "nt":
        # GetDriveType is queried only for a drive-letter root. Mapped shares are refused.
        if len(path.drive) != 2 or path.drive[1] != ":":
            raise ValueError("local drive required")
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(path.anchor)
        if drive_type not in (2, 3):
            raise ValueError("local drive required")
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked paths refused")
    resolved = path.resolve()
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("network paths refused")
    return resolved
