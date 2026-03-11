"""Alternate screen buffer detection and pyte-based rendering."""

import re

import pyte

# Escape sequences for alternate screen buffer
ENTER_ALT_SCREEN = b"\x1b[?1049h"
LEAVE_ALT_SCREEN = b"\x1b[?1049l"

# Regex to strip ANSI escape sequences from raw output
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[\x20-\x3f]*[0-9;]*[\x20-\x7e]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[>=<]")


class ScreenTracker:
    """Tracks alternate screen state and renders via pyte when active."""

    def __init__(self, rows: int = 24, cols: int = 80):
        self.rows = rows
        self.cols = cols
        self.in_alternate_screen = False
        self._pyte_screen = pyte.Screen(cols, rows)
        self._pyte_stream = pyte.ByteStream(self._pyte_screen)

    def update_state(self, raw_bytes: bytes):
        """Scan raw bytes for enter/leave alternate screen sequences and update flag."""
        # Check for transitions — last one wins
        enter_pos = raw_bytes.rfind(ENTER_ALT_SCREEN)
        leave_pos = raw_bytes.rfind(LEAVE_ALT_SCREEN)

        if enter_pos >= 0 and enter_pos > leave_pos:
            if not self.in_alternate_screen:
                # Entering alt screen — reset pyte
                self._pyte_screen.reset()
            self.in_alternate_screen = True
        elif leave_pos >= 0 and leave_pos > enter_pos:
            self.in_alternate_screen = False

        # Always feed bytes to pyte so it has complete state if screen mode
        # is later requested (e.g. via mode="screen" override)
        if raw_bytes:
            self._pyte_stream.feed(raw_bytes)

    def process_output(self, raw_bytes: bytes, strip_ansi: bool = True,
                        mode: str = "auto") -> dict:
        """Process raw output and return rendered text with mode indicator.

        Args:
            raw_bytes: Raw bytes to process (used for "raw" rendering).
            strip_ansi: Whether to strip ANSI escape sequences in raw mode.
            mode: "auto" (default) uses alternate screen detection,
                  "raw" forces raw decoded output,
                  "screen" forces pyte-rendered output.

        Returns dict with keys:
            - text: the processed output string
            - mode: "screen" if rendered via pyte, "raw" otherwise
        """
        use_screen = (
            self.in_alternate_screen if mode == "auto" else mode == "screen"
        )

        if use_screen:
            lines = self._pyte_screen.display
            while lines and not lines[-1].strip():
                lines = lines[:-1]
            text = "\n".join(lines)
            return {"text": text, "mode": "screen"}
        else:
            text = raw_bytes.decode("utf-8", errors="replace")
            if strip_ansi:
                text = _strip_ansi(text)
            return {"text": text, "mode": "raw"}


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_ESCAPE_RE.sub("", text)
