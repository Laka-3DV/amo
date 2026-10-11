"""Zero-model tests for attachment validation limits."""
from __future__ import annotations

import base64
import asyncio
import binascii
import os
import re
import struct
import time
import zlib
from pathlib import Path

from cc_remote.attachments import (
    MAX_ATTACHMENT_COUNT,
    MAX_SINGLE_ATTACHMENT_BYTES,
    validate_attachments,
)
from cc_remote.protocol import ERR_BAD_PROMPT, Query, Steer, TurnSteered
from tests.test_multisession import _mk_ctx, _mk_machine


def _b64(size: int) -> str:
    return base64.b64encode(b"x" * size).decode()


def _png(width: int = 1, height: int = 1, extra: bytes = b"") -> str:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(
        ">II", width, height)
    return base64.b64encode(header + extra).decode()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", checksum))


def _complete_png(*extra_chunks: tuple[bytes, bytes]) -> str:
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + b"".join(_png_chunk(kind, payload) for kind, payload in extra_chunks)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(data).decode()


def test_valid_small_image_and_file():
    assert validate_attachments(
        [{"media_type": "image/png", "data": _png()}],
        [{"filename": "note.txt", "data": _b64(64)}],
    ) is None


def test_rejects_invalid_base64_type_count_and_size():
    assert "base64" in validate_attachments(
        [], [{"filename": "x", "data": "%%%"}])
    assert "unsupported" in validate_attachments(
        [{"media_type": "image/svg+xml", "data": _b64(2)}], [])
    assert "too many" in validate_attachments(
        [], [{"filename": str(i), "data": _b64(1)}
             for i in range(MAX_ATTACHMENT_COUNT + 1)])
    assert "6 MiB" in validate_attachments(
        [], [{"filename": "big", "data": _b64(MAX_SINGLE_ATTACHMENT_BYTES + 1)}])


def test_rejects_total_size_and_bad_filename():
    four_mib = 4 * 1024 * 1024
    assert "8 MiB" in validate_attachments([], [
        {"filename": "a", "data": _b64(four_mib)},
        {"filename": "b", "data": _b64(four_mib)},
        {"filename": "c", "data": _b64(1)},
    ])
    assert "filename" in validate_attachments(
        [], [{"filename": "", "data": _b64(1)}])


def test_rejects_mime_mismatch_pixel_bomb_and_utf8_filename_overflow():
    assert "declared type" in validate_attachments(
        [{"media_type": "image/png", "data": _b64(32)}], [])
    assert "dimensions" in validate_attachments(
        [{"media_type": "image/png", "data": _png(8192, 8192)}], [])
    assert "filename" in validate_attachments(
        [], [{"filename": "界" * 100, "data": _b64(1)}])


def test_png_animation_detection_uses_chunk_boundaries():
    static_with_text = _complete_png(
        (b"tEXt", b"Comment\x00a static image may mention acTL"),
    )
    animated = _complete_png((b"acTL", struct.pack(">II", 1, 0)))

    assert validate_attachments(
        [{"media_type": "image/png", "data": static_with_text}], []) is None
    assert "declared type" in validate_attachments(
        [{"media_type": "image/png", "data": animated}], [])
