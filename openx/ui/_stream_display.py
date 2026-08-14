"""Legacy raw-ANSI streaming display.

Kept for backward compatibility.  Prefer :class:`openx.services.streaming.StreamingService`
for new code — it uses Rich Live + Markdown rendering.
"""

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

import sys
import time


class StreamDisplay:
    """Manages a live-updating streaming response display with a spinner.

    Uses ``\\r`` (carriage return) + ``\\033[2K`` (clear line) to keep the
    spinner on the current line, inserting completed text lines above it
    as they arrive.
    """

    SPIN = ["●", "○", "◌", "○"]

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._token_count = 0
        self._buf = ""

    # ── public API ──────────────────────────────────────────────

    def start(self) -> None:
        """Print the initial spinner (no leading blank line)."""
        sys.stdout.write("\033[2m● Thinking…\033[0m\033[K")
        sys.stdout.flush()

    def feed(self, chunk: str) -> None:
        """Feed one incoming text chunk."""
        if not chunk:
            return
        self._buf += chunk
        self._token_count += 1
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit_line(line)
        sys.stdout.flush()

    def done(self) -> float:
        """Flush remaining buffer and print the *Done* summary."""
        if self._buf:
            sys.stdout.write("\r\033[2K")
            sys.stdout.write(self._buf + "\n")
            sys.stdout.flush()
        elapsed = time.monotonic() - self._t0
        sys.stdout.write("\r\033[2K")
        tok_s = (
            f"{self._token_count / 1000:.1f}k"
            if self._token_count >= 1000
            else str(self._token_count)
        )
        sys.stdout.write(
            f"\033[2m● Done  ·  {elapsed:.1f}s  ·  {tok_s} tokens\033[0m\n"
        )
        sys.stdout.flush()
        return elapsed

    # ── internals ───────────────────────────────────────────────

    def _emit_line(self, line: str) -> None:
        """Clear spinner, print text line, re-draw spinner below."""
        elapsed = time.monotonic() - self._t0
        tok_s = (
            f"{self._token_count / 1000:.1f}k"
            if self._token_count >= 1000
            else str(self._token_count)
        )
        frame = self.SPIN[int(elapsed * 1000 / 120) % 4]
        spinner = (
            f"\033[2m{frame} Thinking…\033[0m"
            f"  ({elapsed:.1f}s)"
            f"  ·  \033[2m{tok_s} tokens\033[0m"
            f"\033[K"
        )
        sys.stdout.write("\r\033[2K")
        sys.stdout.write(line + "\n")
        sys.stdout.write(spinner)


if __name__ == "__main__":
    import io
    _buf, _real = io.StringIO(), sys.stdout
    sys.stdout = _buf  # 重定向：spinner/ANSI 一律写进缓冲区，不写真实终端
    try:
        sd = StreamDisplay()
        sd.start()
        sd.feed("line one\npartial chunk")
        elapsed = sd.done()
    finally:
        sys.stdout = _real
    print(f"captured {len(_buf.getvalue())} chars, done() elapsed={elapsed:.4f}s")
    print("openx/ui/_stream_display.py OK ✓")
