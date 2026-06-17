"""Tier 1 tests for capture_shared — ffmpeg command builders + helpers.

No hardware required. Runs in CI.
"""
from __future__ import annotations

from pathlib import Path

from capture_shared import (
    build_capture_cmd,
    build_extract_cmd,
    build_monitor_cmd,
    fps_to_ffmpeg,
    make_output_path,
    make_recording_path,
)


# ---------------------------------------------------------------------------
# fps_to_ffmpeg
# ---------------------------------------------------------------------------

class TestFpsToFfmpeg:
    def test_ntsc_5994(self):
        assert fps_to_ffmpeg(59.94) == "60000/1001"

    def test_ntsc_2997(self):
        assert fps_to_ffmpeg(29.97) == "30000/1001"

    def test_ntsc_23976(self):
        assert fps_to_ffmpeg(23.976) == "24000/1001"

    def test_ntsc_tolerance(self):
        assert fps_to_ffmpeg(59.946) == "60000/1001"

    def test_integer_60(self):
        assert fps_to_ffmpeg(60.0) == "60"

    def test_integer_50(self):
        assert fps_to_ffmpeg(50.0) == "50"

    def test_integer_25(self):
        assert fps_to_ffmpeg(25.0) == "25"

    def test_non_standard(self):
        assert fps_to_ffmpeg(48.5) == "48.500"


# ---------------------------------------------------------------------------
# build_capture_cmd
# ---------------------------------------------------------------------------

class TestBuildCaptureCmd:
    def _build(self, **kwargs):
        defaults = dict(
            width=1920, height=1080, fps=59.94, interlaced=False,
            output=Path("/tmp/test.mp4"),
        )
        defaults.update(kwargs)
        return build_capture_cmd(**defaults)

    def test_uses_hevc_nvenc(self):
        cmd = self._build()
        assert "hevc_nvenc" in cmd

    def test_uses_main10_profile(self):
        cmd = self._build()
        idx = cmd.index("-profile:v")
        assert cmd[idx + 1] == "main10"

    def test_no_temporal_aq(self):
        cmd = self._build()
        assert "-temporal-aq" not in cmd

    def test_has_spatial_aq(self):
        cmd = self._build()
        assert "-spatial-aq" in cmd

    def test_no_wallclock_on_audio(self):
        cmd = self._build()
        # find the two -i flags (video, audio)
        i_indices = [i for i, v in enumerate(cmd) if v == "-i"]
        assert len(i_indices) == 2
        # between the two -i flags is the audio input section
        audio_section = cmd[i_indices[0] + 1 : i_indices[1]]
        assert "-use_wallclock_as_timestamps" not in audio_section

    def test_wallclock_on_video(self):
        cmd = self._build()
        i_indices = [i for i, v in enumerate(cmd) if v == "-i"]
        video_section = cmd[: i_indices[0]]
        assert "-use_wallclock_as_timestamps" in video_section

    def test_duration_flag(self):
        cmd = self._build(duration=10.0)
        idx = cmd.index("-t")
        assert cmd[idx + 1] == "10.0"

    def test_no_duration_by_default(self):
        cmd = self._build()
        assert "-t" not in cmd

    def test_framerate_ntsc(self):
        cmd = self._build(fps=59.94)
        idx = cmd.index("-framerate")
        assert cmd[idx + 1] == "60000/1001"

    def test_gop_is_one_second(self):
        cmd = self._build(fps=59.94)
        idx = cmd.index("-g")
        assert cmd[idx + 1] == "60"

    def test_output_is_last(self):
        cmd = self._build()
        assert cmd[-1] == "/tmp/test.mp4"

    def test_movflags_faststart(self):
        cmd = self._build()
        assert "+faststart" in cmd

    def test_custom_devices(self):
        cmd = self._build(
            video_device="/dev/video10",
            audio_device="hw:Loopback,1",
        )
        assert "/dev/video10" in cmd
        assert "hw:Loopback,1" in cmd


# ---------------------------------------------------------------------------
# build_monitor_cmd
# ---------------------------------------------------------------------------

class TestBuildMonitorCmd:
    def _build(self, **kwargs):
        defaults = dict(
            width=1920, height=1080, fps=59.94, interlaced=False,
            output=Path("/tmp/session.mp4"),
        )
        defaults.update(kwargs)
        return build_monitor_cmd(**defaults)

    def test_no_tee_muxer(self):
        """tee muxer doesn't forward codec extradata (empty hvcC)."""
        cmd = self._build()
        assert "tee" not in cmd

    def test_has_file_output(self):
        cmd = self._build()
        assert "/tmp/session.mp4" in cmd

    def test_has_fmp4_pipe_output(self):
        cmd = self._build()
        cmd_str = " ".join(cmd)
        assert "frag_keyframe" in cmd_str
        assert "empty_moov" in cmd_str
        assert "default_base_moof" in cmd_str
        assert "pipe:1" in cmd_str

    def test_file_has_no_faststart(self):
        # The session file must NOT use faststart: moov is written at the end
        # (duration unknown up front); faststart would require a full file rewrite
        # on stop, making shutdown proportional to session file size.
        cmd = self._build()
        file_idx = cmd.index("/tmp/session.mp4")
        # Check only the flags that precede the file output (not the pipe flags)
        flags_before_file = " ".join(cmd[:file_idx])
        assert "+faststart" not in flags_before_file

    def test_custom_pipe_fd(self):
        cmd = self._build(pipe_fd="pipe:3")
        assert "pipe:3" in cmd

    def test_dual_encode_args(self):
        """Both outputs should have their own encode args."""
        cmd = self._build()
        assert cmd.count("hevc_nvenc") == 2


# ---------------------------------------------------------------------------
# build_extract_cmd
# ---------------------------------------------------------------------------

class TestBuildExtractCmd:
    def test_basic_extraction(self):
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 10.5, 25.0, Path("/tmp/out.mp4")
        )
        assert cmd[0] == "ffmpeg"
        idx_ss = cmd.index("-ss")
        assert cmd[idx_ss + 1] == "10.500"
        idx_to = cmd.index("-to")
        assert cmd[idx_to + 1] == "25.150"

    def test_seek_is_input_mode(self):
        # -ss must come BEFORE -i (input-mode seek): ffmpeg seeks the whole
        # muxed file to the keyframe before start, so both audio and video
        # depart from the same point and play in sync (brief pre-roll ≤ 1 GOP).
        # Output-mode (-ss after -i) starts audio at mark-in but video at the
        # next keyframe after it, producing a persistent audio-ahead offset.
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 10.5, 25.0, Path("/tmp/out.mp4")
        )
        idx_i  = cmd.index("-i")
        idx_ss = cmd.index("-ss")
        idx_to = cmd.index("-to")
        assert idx_ss < idx_i, "-ss must be before -i (input-mode seek)"
        assert idx_to > idx_i, "-to must be after -i"

    def test_uses_stream_copy(self):
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 0, 5, Path("/tmp/out.mp4")
        )
        assert "-c" in cmd
        idx = cmd.index("-c")
        assert cmd[idx + 1] == "copy"

    def test_output_is_last(self):
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 0, 5, Path("/tmp/out.mp4")
        )
        assert cmd[-1] == "/tmp/out.mp4"

    def test_faststart(self):
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 0, 5, Path("/tmp/out.mp4")
        )
        assert "+faststart" in cmd

    def test_no_reset_timestamps(self):
        # -reset_timestamps 1 resets each stream independently to PTS=0,
        # causing a sustained audio-ahead-of-video offset equal to the keyframe
        # gap. avoid_negative_ts normalises cleanly instead.
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 0, 5, Path("/tmp/out.mp4")
        )
        assert "-reset_timestamps" not in cmd

    def test_avoid_negative_ts(self):
        # Shifts timestamps to start at 0 so large absolute PTSes don't trip
        # up VLC / QuickTime edit list handling.
        cmd = build_extract_cmd(
            Path("/tmp/session.mp4"), 10.5, 25.0, Path("/tmp/out.mp4")
        )
        assert "-avoid_negative_ts" in cmd
        idx = cmd.index("-avoid_negative_ts")
        assert cmd[idx + 1] == "make_zero"


# ---------------------------------------------------------------------------
# make_output_path
# ---------------------------------------------------------------------------

class TestMakeOutputPath:
    def test_mp4_extension(self):
        assert make_output_path(Path("/tmp")).suffix == ".mp4"

    def test_default_prefix(self):
        assert make_output_path(Path("/tmp")).name.startswith("capture_")

    def test_custom_prefix(self):
        assert make_output_path(Path("/tmp"), prefix="session").name.startswith("session_")

    def test_output_in_correct_dir(self):
        assert make_output_path(Path("/tmp/captures")).parent == Path("/tmp/captures")

    def test_no_resolution_in_name(self):
        p = make_output_path(Path("/tmp"), prefix="session")
        assert "x" not in p.stem  # no WxH
        assert "p" not in p.stem.split("_")[-1]  # no scan tag


class TestMakeRecordingPath:
    def _session(self) -> Path:
        return Path("/tmp/sessions/session_20260617_104429.mp4")

    def test_derives_from_session_stem(self):
        p = make_recording_path(self._session(), 1, 0.0)
        assert p.stem.startswith("session_20260617_104429_")

    def test_recording_index(self):
        p = make_recording_path(self._session(), 3, 0.0)
        assert "_3_starting_" in p.name

    def test_time_notation_full(self):
        p = make_recording_path(self._session(), 1, 3723.0)  # 1h2m3s
        assert "_starting_1h2m3s" in p.name

    def test_time_notation_omits_zero_hours(self):
        p = make_recording_path(self._session(), 1, 150.0)  # 2m30s
        assert "_starting_2m30s" in p.name

    def test_time_notation_omits_zero_hours_and_minutes(self):
        p = make_recording_path(self._session(), 1, 45.0)  # 45s
        assert "_starting_45s" in p.name

    def test_time_notation_omits_zero_minutes_when_hours_present(self):
        p = make_recording_path(self._session(), 1, 3630.0)  # 1h0m30s → 1h30s
        assert "_starting_1h30s" in p.name

    def test_zero_time(self):
        p = make_recording_path(self._session(), 1, 0.0)
        assert "_starting_0s" in p.name

    def test_same_dir_as_session(self):
        p = make_recording_path(self._session(), 1, 60.0)
        assert p.parent == Path("/tmp/sessions")

    def test_mp4_extension(self):
        assert make_recording_path(self._session(), 1, 0.0).suffix == ".mp4"
