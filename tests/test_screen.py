"""Tests for screen.py — alternate screen detection and pyte rendering."""

import pytest

from pty_tools.screen import (
    ENTER_ALT_SCREEN,
    LEAVE_ALT_SCREEN,
    ScreenTracker,
    _strip_ansi,
)


class TestStripAnsi:
    def test_plain_text(self):
        assert _strip_ansi("hello world") == "hello world"

    def test_color_codes(self):
        assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_cursor_movement(self):
        assert _strip_ansi("\x1b[2J\x1b[Hhello") == "hello"

    def test_osc_sequences(self):
        assert _strip_ansi("\x1b]0;title\x07text") == "text"


class TestScreenTracker:
    def test_initial_state(self):
        tracker = ScreenTracker()
        assert not tracker.in_alternate_screen

    def test_enter_alternate_screen(self):
        tracker = ScreenTracker()
        tracker.update_state(ENTER_ALT_SCREEN)
        assert tracker.in_alternate_screen

    def test_leave_alternate_screen(self):
        tracker = ScreenTracker()
        tracker.update_state(ENTER_ALT_SCREEN)
        tracker.update_state(LEAVE_ALT_SCREEN)
        assert not tracker.in_alternate_screen

    def test_multiple_transitions(self):
        tracker = ScreenTracker()
        tracker.update_state(ENTER_ALT_SCREEN)
        assert tracker.in_alternate_screen
        tracker.update_state(LEAVE_ALT_SCREEN)
        assert not tracker.in_alternate_screen
        tracker.update_state(ENTER_ALT_SCREEN)
        assert tracker.in_alternate_screen

    def test_enter_and_leave_in_same_chunk(self):
        tracker = ScreenTracker()
        # Last one wins
        tracker.update_state(ENTER_ALT_SCREEN + b"some data" + LEAVE_ALT_SCREEN)
        assert not tracker.in_alternate_screen

    def test_raw_mode_strips_ansi_by_default(self):
        tracker = ScreenTracker()
        result = tracker.process_output(b"\x1b[31mred\x1b[0m text")
        assert result["mode"] == "raw"
        assert result["text"] == "red text"

    def test_raw_mode_preserves_ansi_when_requested(self):
        tracker = ScreenTracker()
        result = tracker.process_output(b"\x1b[31mred\x1b[0m text", strip_ansi=False)
        assert result["mode"] == "raw"
        assert "\x1b[31m" in result["text"]
        assert "red" in result["text"]

    def test_raw_mode_output(self):
        tracker = ScreenTracker()
        result = tracker.process_output(b"hello world")
        assert result["mode"] == "raw"
        assert result["text"] == "hello world"

    def test_screen_mode_output(self):
        tracker = ScreenTracker(rows=5, cols=20)
        tracker.update_state(ENTER_ALT_SCREEN + b"\x1b[1;1HHello Screen")
        result = tracker.process_output(b"")
        assert result["mode"] == "screen"
        assert "Hello Screen" in result["text"]

    def test_screen_mode_strips_trailing_blank_lines(self):
        tracker = ScreenTracker(rows=10, cols=20)
        tracker.update_state(ENTER_ALT_SCREEN + b"\x1b[1;1HLine1")
        result = tracker.process_output(b"")
        lines = result["text"].split("\n")
        assert len(lines) < 10

    def test_mode_raw_overrides_alternate_screen(self):
        """mode='raw' should return raw bytes even when in alternate screen."""
        tracker = ScreenTracker(rows=5, cols=20)
        tracker.update_state(ENTER_ALT_SCREEN + b"\x1b[1;1HScreen Text")
        assert tracker.in_alternate_screen
        result = tracker.process_output(b"raw bytes here", mode="raw")
        assert result["mode"] == "raw"
        assert result["text"] == "raw bytes here"

    def test_mode_screen_without_alternate_screen(self):
        """mode='screen' should return pyte-rendered output even outside alternate screen."""
        tracker = ScreenTracker(rows=5, cols=20)
        tracker.update_state(b"Hello World")
        assert not tracker.in_alternate_screen
        result = tracker.process_output(b"ignored raw", mode="screen")
        assert result["mode"] == "screen"
        assert "Hello World" in result["text"]

    def test_mode_auto_uses_alternate_screen_flag(self):
        """mode='auto' should behave like the default (use in_alternate_screen)."""
        tracker = ScreenTracker(rows=5, cols=20)
        result = tracker.process_output(b"plain text", mode="auto")
        assert result["mode"] == "raw"
        assert result["text"] == "plain text"

        tracker.update_state(ENTER_ALT_SCREEN + b"\x1b[1;1HScreen")
        result = tracker.process_output(b"", mode="auto")
        assert result["mode"] == "screen"

    def test_pyte_reset_on_reenter(self):
        tracker = ScreenTracker(rows=5, cols=20)
        tracker.update_state(ENTER_ALT_SCREEN + b"\x1b[1;1HOld Content")
        tracker.update_state(LEAVE_ALT_SCREEN)
        tracker.update_state(ENTER_ALT_SCREEN + b"\x1b[1;1HNew Content")
        result = tracker.process_output(b"")
        assert "New Content" in result["text"]
        assert "Old Content" not in result["text"]
