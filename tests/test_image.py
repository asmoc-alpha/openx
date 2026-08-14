"""Tests for the image support module."""

import base64
import struct
from pathlib import Path

import pytest

from openx.image import (
    is_image_file,
    image_to_base64_url,
    get_image_metadata,
    detect_terminal,
    detect_protocol,
    extract_image_paths,
    _parse_image_dimensions,
    _check_magic_bytes,
    IMAGE_EXTENSIONS,
)


# ── Helper to create test images ────────────────────────────────

def _make_png(path: Path, width: int = 10, height: int = 8) -> bytes:
    """Create a minimal valid PNG file. Returns the bytes written."""
    import zlib
    import struct

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw_data = b""
    for y in range(height):
        raw_data += b"\x00"  # filter none
        raw_data += b"\xff\x00\x00" * width  # red row
    compressed = zlib.compress(raw_data)
    data = signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    path.write_bytes(data)
    return data


def _make_jpeg(path: Path) -> bytes:
    """Create a minimal valid JPEG. Very small 1x1 gray."""
    # Minimal JPEG: SOI + APP0(JFIF) + SOF0 + DQT + SOS + EOI
    data = bytes([
        0xFF, 0xD8,                     # SOI
        0xFF, 0xE0, 0x00, 0x10,        # APP0
        0x4A, 0x46, 0x49, 0x46, 0x00,  # "JFIF\0"
        0x01, 0x01, 0x01, 0x00,        # version 1.1
        0x00, 0x01, 0x00, 0x01,        # density
        0x00, 0x00,                     # thumbnail
        0xFF, 0xDB, 0x00, 0x43, 0x00,  # DQT
        0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07,
        0x07, 0x07, 0x09, 0x09, 0x08, 0x0A, 0x0C, 0x14,
        0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12, 0x13,
        0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A,
        0x1C, 0x1C, 0x20, 0x24, 0x2E, 0x27, 0x20, 0x22,
        0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29, 0x2C,
        0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39,
        0x3D, 0x38, 0x32, 0x3C, 0x2E, 0x33, 0x34, 0x32,
        0xFF, 0xC0, 0x00, 0x0B, 0x08,  # SOF0
        0x00, 0x01, 0x00, 0x01,        # 1x1
        0x01, 0x01, 0x00,              # components
        0xFF, 0xC4, 0x00, 0x1F, 0x00,  # DHT
        0x00, 0x01, 0x05, 0x01, 0x01,
        0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04,
        0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B,
        0xFF, 0xDA, 0x00, 0x08, 0x01,  # SOS
        0x01, 0x00, 0x00, 0x3F, 0x00,  # component + spectral
        0x7F, 0x00,                     # dummy entropy-coded data
        0xFF, 0xD9,                     # EOI
    ])
    path.write_bytes(data)
    return data


def _make_gif(path: Path) -> bytes:
    """Create a minimal valid GIF (1x1 pixel)."""
    data = (
        b"GIF89a"
        b"\x01\x00\x01\x00"  # width=1, height=1
        b"\xf0\x00\x00"      # color table
        b"\xff\x00\x00"      # red
        b"\x00\x00\x00"      # black
        b"\x00\x00\x00"      # black
        b",\x00\x00\x00\x00"  # image descriptor
        b"\x01\x00\x01\x00"   # image w/h
        b"\x00"               # no LZW
        b"\x02\x02\x4C\x01\x00;"  # image data
    )
    path.write_bytes(data)
    return data


def _make_bmp(path: Path) -> bytes:
    """Create a minimal valid BMP (2x2, 24-bit)."""
    pixel_data = b"\xff\x00\x00" * 4  # 4 red pixels
    row_size = (2 * 3 + 3) & ~3
    pixel_offset = 54
    file_size = pixel_offset + row_size * 2
    data = (
        b"BM"
        + struct.pack("<I", file_size)
        + b"\x00\x00\x00\x00"
        + struct.pack("<I", pixel_offset)
        + struct.pack("<I", 40)  # DIB header size
        + struct.pack("<I", 2)   # width
        + struct.pack("<I", 2)   # height
        + struct.pack("<H", 1)   # planes
        + struct.pack("<H", 24)  # bpp
        + b"\x00" * 20           # rest of DIB
        + pixel_data + b"\x00" * (row_size - 6)  # pad first row
        + pixel_data + b"\x00" * (row_size - 6)  # second row
    )
    path.write_bytes(data)
    return data


# ── Tests ───────────────────────────────────────────────────────


class TestIsImageFile:
    """Image detection tests."""

    def test_png_detected(self, tmp_path):
        p = tmp_path / "test.png"
        _make_png(p)
        assert is_image_file(p)

    def test_jpg_detected(self, tmp_path):
        p = tmp_path / "test.jpg"
        _make_jpeg(p)
        assert is_image_file(p)

    def test_gif_detected(self, tmp_path):
        p = tmp_path / "test.gif"
        _make_gif(p)
        assert is_image_file(p)

    def test_bmp_detected(self, tmp_path):
        p = tmp_path / "test.bmp"
        _make_bmp(p)
        assert is_image_file(p)

    def test_text_not_detected(self, tmp_path):
        p = tmp_path / "test.txt"
        p.write_text("hello world")
        assert not is_image_file(p)

    def test_nonexistent_file(self, tmp_path):
        assert not is_image_file(tmp_path / "nope.png")

    def test_magic_bytes_fallback(self, tmp_path):
        """A file with no extension but PNG magic bytes should be detected."""
        p = tmp_path / "noext"
        _make_png(p)
        assert is_image_file(p)


class TestImageToBase64Url:
    """Base64 URL encoding tests."""

    def test_png_data_url(self, tmp_path):
        p = tmp_path / "test.png"
        _make_png(p)
        url = image_to_base64_url(p)
        assert url.startswith("data:image/png;base64,")
        assert len(url) > 30

    def test_jpeg_data_url(self, tmp_path):
        p = tmp_path / "test.jpg"
        _make_jpeg(p)
        url = image_to_base64_url(p)
        assert url.startswith("data:image/jpeg;base64,")

    def test_from_bytes(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        url = image_to_base64_url(data, mime_type="image/png")
        assert url.startswith("data:image/png;base64,")
        b64_part = url.split(",")[1]
        assert base64.b64decode(b64_part) == data


class TestGetImageMetadata:
    """Metadata extraction tests."""

    def test_png_dimensions(self, tmp_path):
        p = tmp_path / "test.png"
        _make_png(p, width=16, height=9)
        meta = get_image_metadata(p)
        assert meta["width"] == 16
        assert meta["height"] == 9
        assert meta["format"] == "png"

    def test_gif_dimensions(self, tmp_path):
        p = tmp_path / "test.gif"
        _make_gif(p)
        meta = get_image_metadata(p)
        assert meta["width"] == 1
        assert meta["height"] == 1
        assert meta["format"] == "gif"

    def test_bmp_dimensions(self, tmp_path):
        p = tmp_path / "test.bmp"
        _make_bmp(p)
        meta = get_image_metadata(p)
        assert meta["width"] == 2
        assert meta["height"] == 2

    def test_from_bytes(self):
        data = _make_png(Path("/dev/null")) if False else None
        # Create PNG in memory
        import zlib
        def chunk(ct, d):
            c = ct + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = struct.pack(">IIBBBBB", 32, 18, 8, 2, 0, 0, 0)
        raw = b"\x00" + b"\xff\x00\x00" * 32
        raw = (raw + b"\x00" + b"\x00\xff\x00" * 32) * 9
        compressed = zlib.compress(raw)
        data = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
        meta = get_image_metadata(data)
        assert meta["width"] == 32
        assert meta["height"] == 18


class TestTerminalDetection:
    """Terminal detection tests."""

    def test_detect_terminal_returns_string(self):
        term = detect_terminal()
        assert isinstance(term, str)
        assert len(term) > 0

    def test_detect_protocol_returns_string_or_none(self):
        proto = detect_protocol()
        assert proto is None or isinstance(proto, str)

    def test_term_is_in_known_set(self):
        term = detect_terminal()
        known = {
            "iterm2", "kitty", "ghostty", "wezterm", "konsole",
            "warp", "vscode", "foot", "terminal_app", "tmux", "unknown",
        }
        assert term in known


class TestExtractImagePaths:
    """Drag-drop path extraction tests."""

    def test_single_image_path(self, tmp_path):
        p = tmp_path / "screen.png"
        _make_png(p)
        paths = extract_image_paths(str(p))
        assert len(paths) == 1
        assert paths[0] == p

    def test_non_image_ignored(self, tmp_path):
        p = tmp_path / "readme.txt"
        p.write_text("hello")
        paths = extract_image_paths(str(p))
        assert len(paths) == 0

    def test_escaped_spaces(self, tmp_path):
        """Simulate drag-drop with backslash-escaped spaces."""
        subdir = tmp_path / "My Screenshots"
        subdir.mkdir()
        p = subdir / "shot.png"
        _make_png(p)
        # Simulate the escaped input that iTerm2 would send
        raw_input = str(p).replace(" ", "\\ ")
        paths = extract_image_paths(raw_input)
        assert len(paths) == 1
        assert paths[0] == p

    def test_mixed_text_and_image(self, tmp_path):
        p = tmp_path / "img.png"
        _make_png(p)
        text = f"Look at this {p}"
        paths = extract_image_paths(text)
        assert len(paths) == 1

    def test_multiple_images(self, tmp_path):
        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.jpg"
        _make_png(p1)
        _make_jpeg(p2)
        text = f"{p1} {p2}"
        paths = extract_image_paths(text)
        assert len(paths) == 2

    def test_nonexistent_path_ignored(self, tmp_path):
        paths = extract_image_paths(str(tmp_path / "ghost.png"))
        assert len(paths) == 0


class TestImageExtensions:
    """Extension table tests."""

    def test_known_extensions(self):
        for ext in [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"]:
            assert ext in IMAGE_EXTENSIONS
