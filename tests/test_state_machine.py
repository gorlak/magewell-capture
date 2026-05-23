"""Tier 1 tests for the monitor state machine.

No hardware required. Runs in CI.
"""
from __future__ import annotations

import time

from monitor import SessionState, State


def _make_session(elapsed: float = 0.0) -> SessionState:
    """Create a session with a start time offset so elapsed ≈ `elapsed`."""
    return SessionState(time.monotonic() - elapsed)


class TestSessionState:
    def test_initial_state_is_standby(self):
        s = _make_session()
        assert s.state is State.STANDBY

    def test_initial_no_segments(self):
        s = _make_session()
        assert s.segments == []

    def test_elapsed_increases(self):
        s = _make_session(elapsed=5.0)
        assert s.elapsed >= 5.0

    def test_recording_elapsed_none_when_standby(self):
        s = _make_session()
        assert s.recording_elapsed is None


class TestMarkIn:
    def test_mark_in_transitions_to_recording(self):
        s = _make_session()
        assert s.mark_in(10.0) is True
        assert s.state is State.RECORDING

    def test_double_mark_in_returns_false(self):
        s = _make_session()
        s.mark_in(10.0)
        assert s.mark_in(12.0) is False
        assert s.state is State.RECORDING

    def test_recording_elapsed_available_after_mark_in(self):
        s = _make_session()
        s.mark_in(10.0)
        assert s.recording_elapsed is not None
        assert s.recording_elapsed >= 0.0


class TestMarkOut:
    def test_mark_out_without_mark_in_returns_false(self):
        s = _make_session()
        assert s.mark_out(5.0) is False
        assert s.state is State.STANDBY

    def test_mark_out_after_mark_in(self):
        s = _make_session()
        s.mark_in(10.0)
        assert s.mark_out(20.0) is True
        assert s.state is State.STANDBY

    def test_mark_out_creates_segment(self):
        s = _make_session()
        s.mark_in(10.0)
        s.mark_out(20.0)
        assert len(s.segments) == 1
        start, end = s.segments[0]
        assert start == 10.0
        assert end == 20.0

    def test_double_mark_out_returns_false(self):
        s = _make_session()
        s.mark_in(10.0)
        s.mark_out(20.0)
        assert s.mark_out(25.0) is False

    def test_recording_elapsed_none_after_mark_out(self):
        s = _make_session()
        s.mark_in(10.0)
        s.mark_out(20.0)
        assert s.recording_elapsed is None


class TestStreamTimeAccuracy:
    def test_segment_uses_exact_stream_times(self):
        s = _make_session()
        s.mark_in(42.567)
        s.mark_out(98.123)
        assert s.segments[0] == (42.567, 98.123)

    def test_multiple_segments_preserve_stream_times(self):
        s = _make_session()
        s.mark_in(10.0)
        s.mark_out(20.0)
        s.mark_in(50.5)
        s.mark_out(75.25)
        assert s.segments[0] == (10.0, 20.0)
        assert s.segments[1] == (50.5, 75.25)


class TestMultipleSegments:
    def test_multiple_in_out_cycles(self):
        s = _make_session()
        for i in range(3):
            s.mark_in(float(i * 20))
            s.mark_out(float(i * 20 + 10))
        assert len(s.segments) == 3

    def test_segments_are_monotonic(self):
        s = _make_session()
        for i in range(3):
            s.mark_in(float(i * 20))
            s.mark_out(float(i * 20 + 10))
        for i in range(1, len(s.segments)):
            prev_end = s.segments[i - 1][1]
            curr_start = s.segments[i][0]
            assert curr_start >= prev_end


class TestFinalize:
    def test_finalize_closes_open_recording(self):
        s = _make_session()
        s.mark_in(10.0)
        assert s.state is State.RECORDING
        s.finalize(25.0)
        assert s.state is State.STANDBY
        assert len(s.segments) == 1
        assert s.segments[0] == (10.0, 25.0)

    def test_finalize_noop_when_standby(self):
        s = _make_session()
        s.finalize(5.0)
        assert s.state is State.STANDBY
        assert len(s.segments) == 0

    def test_finalize_preserves_existing_segments(self):
        s = _make_session()
        s.mark_in(10.0)
        s.mark_out(20.0)
        s.mark_in(30.0)
        s.finalize(40.0)
        assert len(s.segments) == 2
        assert s.segments[1] == (30.0, 40.0)


class TestToDict:
    def test_standby_dict(self):
        s = _make_session()
        d = s.to_dict()
        assert d["state"] == "STANDBY"
        assert d["recording_elapsed"] is None
        assert d["segments"] == []
        assert isinstance(d["elapsed"], float)

    def test_recording_dict(self):
        s = _make_session()
        s.mark_in(10.0)
        d = s.to_dict()
        assert d["state"] == "RECORDING"
        assert d["recording_elapsed"] is not None

    def test_segments_in_dict(self):
        s = _make_session()
        s.mark_in(10.0)
        s.mark_out(20.0)
        d = s.to_dict()
        assert len(d["segments"]) == 1
        seg = d["segments"][0]
        assert seg["in"] == 10.0
        assert seg["out"] == 20.0
