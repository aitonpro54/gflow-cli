"""Basic local container checks and streaming hash, without full codec decoding."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path
from typing import BinaryIO

from gflow_cli.bridge.service import BridgeError


def _mp4(stream: BinaryIO, total: int) -> bool:
    boxes: set[bytes] = set()
    position = 0
    while position < total:
        raw = stream.read(8)
        if len(raw) != 8:
            return False
        size, kind = struct.unpack(">I4s", raw)
        minimum = 8
        if size == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                return False
            size = struct.unpack(">Q", extended)[0]
            minimum = 16
        elif size == 0:
            size = total - position
        if size < minimum or position + size > total:
            return False
        if kind == b"mdat" and size == minimum:
            return False
        boxes.add(kind)
        position += size
        stream.seek(position)
    return {b"ftyp", b"moov", b"mdat"} <= boxes


def _png(stream: BinaryIO, total: int) -> bool:
    if stream.read(8) != b"\x89PNG\r\n\x1a\n":
        return False
    seen_header = False
    seen_data = False
    while stream.tell() < total:
        raw = stream.read(8)
        if len(raw) != 8:
            return False
        length, kind = struct.unpack(">I4s", raw)
        if stream.tell() + length + 4 > total:
            return False
        if not seen_header:
            if kind != b"IHDR" or length != 13:
                return False
            payload = stream.read(13)
            if not all(struct.unpack(">II", payload[:8])):
                return False
            crc = zlib.crc32(payload, zlib.crc32(kind))
            seen_header = True
        else:
            if kind == b"IHDR":
                return False
            crc = zlib.crc32(kind)
            remaining = length
            while remaining:
                payload = stream.read(min(remaining, 1024 * 1024))
                if not payload:
                    return False
                crc = zlib.crc32(payload, crc)
                remaining -= len(payload)
        if stream.read(4) != struct.pack(">I", crc & 0xFFFFFFFF):
            return False
        seen_data = seen_data or (kind == b"IDAT" and length > 0)
        if kind == b"IEND":
            return length == 0 and seen_data and stream.tell() == total
    return False


def _image(stream: BinaryIO, suffix: str, total: int) -> bool:
    header = stream.read(32)
    stream.seek(0)
    if suffix == ".png":
        return _png(stream, total)
    stream.seek(-2, 2)
    tail = stream.read(2)
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff") and tail == b"\xff\xd9"
    if suffix == ".gif":
        return header[:6] in {b"GIF87a", b"GIF89a"} and tail[-1:] == b";"
    if suffix == ".webp":
        return (
            header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
            and struct.unpack("<I", header[4:8])[0] + 8 == total
            and header[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
        )
    return False


def verify_artifact(path: Path, expected_hash: str, kind: str) -> None:
    """Reject obvious HTML/truncation/hash mismatch; never hold a SQL lock."""
    suffix = path.suffix.lower()
    if (kind == "video" and suffix != ".mp4") or (
        kind == "image" and suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    ):
        raise BridgeError("artifact_kind_mismatch")
    try:
        before = path.stat()
        if not path.is_file() or before.st_size < 16:
            raise BridgeError("artifact_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            stream.seek(0)
            valid = (
                _mp4(stream, before.st_size)
                if suffix == ".mp4"
                else _image(stream, suffix, before.st_size)
            )
        after = path.stat()
        if not valid or digest.hexdigest() != expected_hash:
            raise BridgeError("artifact_invalid_or_hash_mismatch")
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise BridgeError("artifact_changed_during_validation")
    except (OSError, ValueError) as exc:
        raise BridgeError("artifact_unreadable") from exc
