"""Container/path safety tests do not contact remote hosts."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from gflow_cli.bridge.artifact import verify_artifact
from gflow_cli.bridge.paths import local_path
from gflow_cli.bridge.service import BridgeError, Settings


@pytest.mark.parametrize(
    "path",
    [
        r"\\unreachable.invalid\share\state.db",
        r"\\?\UNC\unreachable.invalid\share\state.db",
        r"\\.\pipe\device",
        "//unreachable.invalid/share/state.db",
    ],
)
def test_remote_and_device_paths_rejected_before_io(path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not inspect a network/device target")

    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    with pytest.raises(BridgeError, match="local_state_db"):
        Settings(Path(path), "Test Profile")
    with pytest.raises(ValueError):
        local_path(Path(path))


def test_reparse_point_rejected_before_resolve(tmp_path, monkeypatch):
    original = Path.lstat

    def reparse(path):
        if path == tmp_path:
            return SimpleNamespace(st_mode=0, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError, match="linked"):
        local_path(tmp_path / "artifact.mp4")


def test_mapped_network_drive_rejected_before_io(tmp_path, monkeypatch):
    import ctypes
    import os

    if os.name != "nt":
        pytest.skip("Windows drive type check")
    monkeypatch.setattr(ctypes.windll.kernel32, "GetDriveTypeW", lambda root: 4)
    monkeypatch.setattr(Path, "lstat", lambda *args: pytest.fail("must not stat remote drive"))
    with pytest.raises(ValueError, match="local drive"):
        local_path(tmp_path / "state.db")


def test_png_crc_and_truncation(tmp_path, valid_png):
    path = tmp_path / "result.png"
    path.write_bytes(valid_png)
    verify_artifact(path, hashlib.sha256(valid_png).hexdigest(), "image")
    for content in [
        valid_png[:-1],
        valid_png.replace(b"IDAT", b"JDAT"),
        b"\x89PNG\r\n\x1a\n" + b"0" * 24,
    ]:
        path.write_bytes(content)
        with pytest.raises(BridgeError, match="artifact"):
            verify_artifact(path, hashlib.sha256(content).hexdigest(), "image")


@pytest.mark.parametrize(
    "suffix,content",
    [
        (".jpg", b"\xff\xd8\xff" + b"0" * 30),
        (".gif", b"GIF89a" + b"0" * 30),
        (".webp", b"RIFF\xff\xff\xff\xffWEBPVP8 " + b"0" * 30),
        (".mp4", b"\x00\x00\xff\xffftyp" + b"0" * 30),
    ],
)
def test_truncated_container_cannot_finish(tmp_path, suffix, content):
    path = tmp_path / ("bad" + suffix)
    path.write_bytes(content)
    with pytest.raises(BridgeError, match="artifact"):
        verify_artifact(path, hashlib.sha256(content).hexdigest(), "upscale")


def test_parent_traversal_rejected_before_io(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "lstat", lambda *args: pytest.fail("no path probe"))
    monkeypatch.setattr(Path, "resolve", lambda *args: pytest.fail("no path resolve"))
    with pytest.raises(ValueError, match="parent traversal"):
        local_path(tmp_path / "missing" / ".." / "junction" / "file.mp4")
