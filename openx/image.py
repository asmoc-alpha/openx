"""Image support for OpenX — loading, display, clipboard, and drag-and-drop.

Provides:
- Image format detection (extension + magic bytes)
- Base64 encoding for multimodal LLM input
- Terminal image rendering (iTerm2 OSC 1337, Kitty TGP, Sixel fallback)
- macOS clipboard image access (osascript)
- Drag-and-drop path extraction from user input
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import base64
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ── Constants ───────────────────────────────────────────────────

IMAGE_EXTENSIONS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".tiff", ".tif", ".webp", ".ico", ".heic", ".heif",
}

# Magic bytes for common image formats
_IMAGE_MAGIC: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"BM": "bmp",
    b"RIFF": "webp",   # RIFF....WEBP — checked separately
}

# Terminal image protocols: detection map
_TERM_PROTOCOLS: dict[str, str] = {
    "iterm2": "iterm2",
    "warp": "iterm2",
    "mintty": "iterm2",
    "kitty": "kitty",
    "ghostty": "kitty",
    "wezterm": "kitty",
    "konsole": "kitty",
    "vscode": "sixel",
    "foot": "sixel",
}


# ── Format detection ────────────────────────────────────────────

def is_image_file(path: str | Path) -> bool:
    """Check if a file is an image (extension + magic bytes verification).

    Args:
        path: The file path to check.

    Returns:
        True if the file exists and is a recognized image format.
    """
    path = Path(path)
    if not path.is_file():
        return False

    # Check extension first (fast path)
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return True

    # If extension unknown, fall back to magic bytes
    return _check_magic_bytes(path)


def _check_magic_bytes(path: Path) -> bool:
    """Check file magic bytes for known image signatures."""
    try:
        with open(path, "rb") as f:
            header = f.read(12)
    except (OSError, PermissionError):
        return False

    if len(header) < 2:
        return False

    # PNG
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # JPEG
    if header[:3] == b"\xff\xd8\xff":
        return True
    # GIF
    if header[:4] in (b"GIF87a", b"GIF89a"):
        return True
    # BMP
    if header[:2] == b"BM":
        return True
    # WebP: RIFF....WEBP
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True
    # HEIC/HEIF: ftyp box
    if header[4:8] == b"ftyp" and header[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"mif1",
    ):
        return True

    return False


# ── Image loading and encoding ──────────────────────────────────

def load_image_bytes(path: str | Path) -> bytes:
    """Read an image file into bytes.

    Raises FileNotFoundError if the file does not exist.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Image file not found: {path}")
    return path.read_bytes()


def image_to_base64_url(source: str | Path | bytes, mime_type: str = "") -> str:
    """Convert an image to an OpenAI-compatible base64 data URL.

    Args:
        source: File path or raw image bytes.
        mime_type: Optional MIME type (e.g. "image/png").
                   Auto-detected from extension if empty.

    Returns:
        A data URL string: "data:image/png;base64,iVBORw0KG..."
    """
    if isinstance(source, (str, Path)):
        data = load_image_bytes(source)
        # Auto-detect MIME from extension
        if not mime_type:
            ext = Path(source).suffix.lower()
            mime_type = _ext_to_mime(ext)
    else:
        data = source

    if not mime_type:
        mime_type = "image/png"  # default fallback

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def _ext_to_mime(ext: str) -> str:
    """Map file extension to MIME type."""
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(ext, "image/png")


def get_image_metadata(source: str | Path | bytes) -> dict:
    """Get basic image metadata without PIL.

    Returns a dict with keys: width, height, format, size_bytes.
    Values are 0 if detection fails.
    """
    result = {"width": 0, "height": 0, "format": "unknown", "size_bytes": 0}

    if isinstance(source, (str, Path)):
        path = Path(source)
        result["format"] = path.suffix.lower().lstrip(".")
        result["size_bytes"] = path.stat().st_size if path.is_file() else 0
        try:
            data = path.read_bytes()
        except (OSError, PermissionError):
            return result
    else:
        data = source
        result["size_bytes"] = len(data)

    # Try to extract dimensions from file headers (no PIL needed)
    try:
        w, h = _parse_image_dimensions(data)
        result["width"] = w
        result["height"] = h
    except Exception:
        pass

    return result


def _parse_image_dimensions(data: bytes) -> tuple[int, int]:
    """Parse width/height from common image headers without PIL.

    Returns (width, height), may raise on unknown formats.
    """
    # PNG: IHDR chunk at offset 16: width(4) height(4)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h

    # JPEG: scan for SOF0/SOF1/SOF2 markers
    if data[:3] == b"\xff\xd8\xff" and len(data) > 10:
        i = 2
        while i < len(data) - 9:
            while data[i] != 0xFF and i < len(data) - 1:
                i += 1
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):  # SOF0, SOF1, SOF2
                if i + 9 < len(data):
                    h, w = struct.unpack(">HH", data[i + 5: i + 9])
                    return w, h
                break
            length = struct.unpack(">H", data[i + 2: i + 4])[0]
            i += 2 + length
        raise ValueError("Could not find JPEG dimensions")

    # GIF: width(2) height(2) at offset 6
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h

    # BMP: width(4) height(4) at offset 18
    if data[:2] == b"BM" and len(data) >= 26:
        w, h = struct.unpack("<II", data[18:26])
        return w, abs(h)  # height can be negative (top-down)

    # WebP: check VP8/VP8L/VP8X
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 26:
            # VP8: dimensions encoded in 16 bits at offset 26
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        elif chunk == b"VP8L" and len(data) >= 25:
            # VP8L: dimensions in 4 bytes at offset 21
            bits = struct.unpack("<I", data[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h
        elif chunk == b"VP8X" and len(data) >= 30:
            w = struct.unpack("<I", b"\x00" + data[24:27])[0] + 1
            h = struct.unpack("<I", b"\x00" + data[27:30])[0] + 1
            return w, h

    raise ValueError(f"Unsupported image format, header: {data[:4].hex()}")


# ── Terminal detection ──────────────────────────────────────────

def detect_terminal() -> str:
    """Detect the current terminal emulator.

    Returns one of: iterm2, kitty, ghostty, wezterm, konsole, warp,
    vscode, foot, terminal_app, tmux, unknown.
    """
    tp = os.environ.get("TERM_PROGRAM", "")
    term = os.environ.get("TERM", "")

    if "iTerm" in tp or "ITERM_SESSION_ID" in os.environ:
        return "iterm2"
    if "kitty" in term or "KITTY_WINDOW_ID" in os.environ:
        return "kitty"
    if "ghostty" in tp.lower() or "GHOSTTY_BIN_DIR" in os.environ:
        return "ghostty"
    if "WezTerm" in tp or "WEZTERM_EXECUTABLE" in os.environ:
        return "wezterm"
    if "KONSOLE_VERSION" in os.environ:
        return "konsole"
    if tp == "WarpTerminal":
        return "warp"
    if tp == "vscode":
        return "vscode"
    if term.startswith("foot"):
        return "foot"
    if tp == "Apple_Terminal":
        return "terminal_app"
    if "TMUX" in os.environ:
        return "tmux"
    return "unknown"


def detect_protocol() -> Optional[str]:
    """Detect the best image display protocol for the current terminal.

    Returns: 'iterm2', 'kitty', 'sixel', or None.
    Respects the IMAGE_PROTOCOL environment variable override.
    """
    override = os.environ.get("IMAGE_PROTOCOL", "").lower()
    if override in ("kitty", "iterm2", "sixel"):
        return override
    if override == "none":
        return None

    term = detect_terminal()
    return _TERM_PROTOCOLS.get(term)


# ── Terminal image display ──────────────────────────────────────

def display_image(path: str | Path, width: str = "auto", height: str = "auto") -> bool:
    """Display an image in the terminal using the best available protocol.

    Args:
        path: Path to the image file.
        width: Display width (px, %, "auto"). iTerm2 only.
        height: Display height (px, %, "auto"). iTerm2 only.

    Returns:
        True if an image protocol was used to render the image.
        False if only metadata was shown (fallback).
    """
    path = Path(path)
    if not path.is_file():
        print(f"Error: File not found: {path}")
        return False

    protocol = detect_protocol()

    if protocol == "iterm2":
        return _iterm2_display(path, width, height)
    elif protocol == "kitty":
        return _kitty_display(path)
    elif protocol == "sixel":
        return _sixel_display(path)

    # No protocol available — show metadata
    meta = get_image_metadata(path)
    _print_image_meta(path, meta)
    return False


def display_image_bytes(data: bytes, width: str = "auto", height: str = "auto") -> bool:
    """Display raw image bytes in the terminal."""
    protocol = detect_protocol()

    if protocol == "iterm2":
        return _iterm2_display_bytes(data, width, height)
    elif protocol == "kitty":
        # Kitty TGP: write to temp file and reference it
        import tempfile
        ext = _guess_ext_from_bytes(data)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
            f.write(data)
            tmp_path = Path(f.name)
        try:
            return _kitty_display(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    # Fallback
    meta = get_image_metadata(data)
    _print_image_meta(Path("(bytes)"), meta)
    return False


def _iterm2_display(path: Path, width: str = "auto", height: str = "auto") -> bool:
    """Display using iTerm2 OSC 1337 inline image protocol."""
    try:
        data = path.read_bytes()
    except (OSError, PermissionError):
        return False
    return _iterm2_display_bytes(data, width, height)


def _iterm2_display_bytes(data: bytes, width: str = "auto", height: str = "auto") -> bool:
    """Display raw bytes using iTerm2 OSC 1337."""
    b64 = base64.b64encode(data).decode("ascii")
    name = base64.b64encode(b"image").decode("ascii")
    args = f"name={name};inline=1;width={width};height={height};preserveAspectRatio=1"
    # Use ST terminator (\033\\) for reliability
    sys.stdout.write(f"\033]1337;File={args}:{b64}\033\\\n")
    sys.stdout.flush()
    return True


def _kitty_display(path: Path) -> bool:
    """Display using Kitty Terminal Graphics Protocol.

    Uses file-based transfer (t=f) — the simplest approach that
    passes the absolute path to Kitty for native loading.
    """
    encoded = base64.b64encode(str(path).encode()).decode("ascii")
    sys.stdout.write(f"\033_Ga=T,f=100,t=f;{encoded}\033\\")
    sys.stdout.flush()
    return True


def _sixel_display(path: Path) -> bool:
    """Display using Sixel (delegates to chafa or img2sixel CLI)."""
    if cmd := shutil.which("chafa"):
        subprocess.run([cmd, "--format", "sixel", str(path)])
        return True
    if cmd := shutil.which("img2sixel"):
        subprocess.run([cmd, str(path)])
        return True
    return False


def _print_image_meta(path: Path, meta: dict) -> None:
    """Print image metadata when no terminal protocol is available."""
    w = meta.get("width", 0)
    h = meta.get("height", 0)
    fmt = meta.get("format", "?")
    size = meta.get("size_bytes", 0)
    size_str = _format_bytes(size)
    print(f"Image: {w}x{h}  {fmt.upper()}  {size_str}")
    print(f"   Path: {path}")


def _guess_ext_from_bytes(data: bytes) -> str:
    """Guess file extension from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:2] == b"BM":
        return ".bmp"
    return ".png"


# ── Clipboard (macOS) ───────────────────────────────────────────

def check_clipboard_for_image() -> Optional[bytes]:
    """Check if the macOS clipboard contains an image.

    Returns PNG bytes if an image is on the clipboard, or None.
    Uses osascript for reliable macOS clipboard access.
    """
    if sys.platform != "darwin":
        return None  # clipboard image only supported on macOS for now

    try:
        result = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, text=True, timeout=5,
        )
        if "«class PNGf»" not in result.stdout and "picture" not in result.stdout.lower():
            return None

        # Extract PNG data
        result = subprocess.run(
            ["osascript", "-e", "get the clipboard as «class PNGf»"],
            capture_output=True, text=True, timeout=5,
        )
        hex_str = result.stdout.strip()
        if not hex_str:
            return None
        # Remove AppleScript wrapper: "«data PNGf...»"
        hex_str = hex_str.replace("«data PNGf", "").replace("»", "").strip()
        import binascii
        return binascii.unhexlify(hex_str)
    except (subprocess.TimeoutExpired, binascii.Error, ValueError, OSError):
        pass

    return None


def save_clipboard_image(
    output_path: str | Path | None = None,
) -> Optional[Path]:
    """Save the clipboard image to a file.

    Args:
        output_path: Where to save. Defaults to a temp file.

    Returns:
        Path to the saved file, or None if no image on clipboard.
    """
    data = check_clipboard_for_image()
    if data is None:
        return None

    if output_path is None:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".png", prefix="openx_clipboard_")
        os.close(fd)
        output_path = Path(path)
    else:
        output_path = Path(output_path)

    output_path.write_bytes(data)
    return output_path


# ── Drag-and-drop / path extraction ─────────────────────────────

def extract_image_paths(text: str) -> list[Path]:
    """Extract image file paths from user input.

    Handles drag-and-drop from Finder (space-delimited, backslash-escaped
    paths) as well as manually typed paths.

    Args:
        text: The raw user input string.

    Returns:
        List of resolved Path objects that exist and are image files.
    """
    # Clean up: strip surrounding quotes
    cleaned = text.strip().strip("'\"")

    # Split on whitespace, but respect backslash-escaped spaces.
    # "path/to/My\\ Dir/file.png other.png" → ["path/to/My Dir/file.png", "other.png"]
    candidates = _split_respecting_escapes(cleaned)

    results: list[Path] = []

    for candidate in candidates:
        try:
            path = Path(candidate).expanduser().resolve()
        except (OSError, RuntimeError):
            continue

        if path.is_file() and is_image_file(path):
            if path not in results:
                results.append(path)

    return results


# ── Internal helpers ────────────────────────────────────────────

def _split_respecting_escapes(text: str) -> list[str]:
    """Split on whitespace, respecting backslash-escaped spaces.

    "a\\ b c" → ["a b", "c"]
    """
    tokens: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == " ":
            current.append(" ")
            i += 2
        elif text[i].isspace():
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
        else:
            current.append(text[i])
            i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _format_bytes(size: int) -> str:
    """Format byte count to human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


if __name__ == "__main__":
    import tempfile, zlib
    from pathlib import Path as _P

    # 在内存中拼一个极小的合法 PNG（1x1 红色像素，含正确 CRC 与 zlib 压缩 IDAT）
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    _ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    _png = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", _ihdr)
            + _chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + _chunk(b"IEND", b""))

    with tempfile.TemporaryDirectory() as _td:
        _fp = _P(_td) / "pixel.png"
        _fp.write_bytes(_png)
        assert is_image_file(_fp)
        _meta = get_image_metadata(_fp)
        assert (_meta["width"], _meta["height"], _meta["format"]) == (1, 1, "png"), _meta
        _url = image_to_base64_url(_fp)
        assert _url.startswith("data:image/png;base64,")
        assert image_to_base64_url(_png) == _url  # 字节输入与文件输入结果一致
        assert extract_image_paths(str(_fp)) == [_fp.resolve()]
        print(f"metadata={_meta}  data-url={_url[:38]}...")

    print("openx/image.py OK ✓")
