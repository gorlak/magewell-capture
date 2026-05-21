"""Tests for the magewell ctypes binding.

Three layers:
  * layout tests  — no hardware; tripwire for struct/type mirroring vs the SDK.
  * setup test    — verifies the one-time privileged setup (udev rule).
  * device tests  — talk to the real Magewell; auto-skip if none present.
"""
from __future__ import annotations

import ctypes
import os

import pytest

import magewell
from magewell import _sdk
from magewell._sdk import (
    MWCAP_AUDIO_SIGNAL_STATUS,
    MWCAP_CHANNEL_INFO,
    MWCAP_VIDEO_SIGNAL_STATUS,
    SignalState,
)


# --------------------------------------------------------------------------- #
# Layout tests — struct size / offset tripwires (no hardware)
# --------------------------------------------------------------------------- #

def test_video_signal_is_byte_packed():
    assert MWCAP_VIDEO_SIGNAL_STATUS._pack_ == 1


def test_video_signal_size():
    assert ctypes.sizeof(MWCAP_VIDEO_SIGNAL_STATUS) == _sdk.SIZEOF_VIDEO_SIGNAL_STATUS == 58


@pytest.mark.parametrize("field,offset", [
    ("state", 0), ("cx", 12), ("cy", 16),
    ("bInterlaced", 28), ("dwFrameDuration", 29),
    ("bSegmentedFrame", 41), ("satRange", 54),
])
def test_video_signal_field_offsets(field, offset):
    assert getattr(MWCAP_VIDEO_SIGNAL_STATUS, field).offset == offset


def test_channel_info_size():
    assert MWCAP_CHANNEL_INFO._pack_ == 1
    assert ctypes.sizeof(MWCAP_CHANNEL_INFO) == _sdk.SIZEOF_CHANNEL_INFO == 224


def test_audio_signal_size():
    assert MWCAP_AUDIO_SIGNAL_STATUS._pack_ == 1
    assert ctypes.sizeof(MWCAP_AUDIO_SIGNAL_STATUS) == _sdk.SIZEOF_AUDIO_SIGNAL_STATUS == 33


def test_library_loads_and_binds():
    lib = _sdk._get_lib()
    assert lib.MWGetVideoSignalStatus.restype is ctypes.c_int
    assert lib.MWGetAudioSignalStatus.restype is ctypes.c_int
    assert lib.MWGetChannelInfo.restype is ctypes.c_int


# --------------------------------------------------------------------------- #
# Setup verification
# --------------------------------------------------------------------------- #

def test_usb_setup_has_been_applied():
    node = _sdk.find_usb_node()
    if node is None:
        pytest.skip("no Magewell USB device present")
    assert os.access(node, os.W_OK), (
        f"{node} is not writable: the one-time privileged setup has not been run. "
        f"Run 'sudo ./setup.sh' to install the udev rule (see README)."
    )


# --------------------------------------------------------------------------- #
# Device tests
# --------------------------------------------------------------------------- #

def _device_present() -> bool:
    try:
        return magewell.channel_count() >= 1
    except Exception:
        return False


requires_device = pytest.mark.skipif(
    not _device_present(), reason="no Magewell capture device present"
)


@requires_device
def test_channel_count_positive():
    assert magewell.channel_count() >= 1


@requires_device
def test_read_signal_returns_valid_state():
    sig = magewell.read_signal()
    assert isinstance(sig, magewell.Signal)
    assert sig.state in set(SignalState)


@requires_device
def test_read_signal_index_out_of_range_raises():
    n = magewell.channel_count()
    with pytest.raises(magewell.MWError):
        magewell.read_signal(index=n + 5)


@requires_device
def test_locked_signal_is_sane():
    sig = magewell.read_signal()
    if not sig.locked:
        pytest.skip(f"no locked signal (state={sig.state.name})")
    assert 0 < sig.width <= 2048
    assert 0 < sig.height <= 2160
    assert sig.fps > 0
    assert sig.frame_duration_100ns > 0
    assert sig.fps == pytest.approx(10_000_000 / sig.frame_duration_100ns, rel=1e-3)
    assert isinstance(sig.interlaced, bool)
    assert isinstance(sig.segmented_frame, bool)
    assert isinstance(sig.color_format, magewell.VideoColorFormat)
    assert isinstance(sig.quant_range, magewell.VideoQuantizationRange)
    assert isinstance(sig.sat_range, magewell.VideoSaturationRange)
    assert isinstance(sig.frame_type, magewell.VideoFrameType)


@requires_device
def test_read_channel_info():
    info = magewell.read_channel_info()
    assert isinstance(info, magewell.ChannelInfo)
    assert isinstance(info.family, magewell.FamilyID)
    assert len(info.serial_no) > 0
    assert len(info.product_name) > 0


@requires_device
def test_read_audio_signal():
    audio = magewell.read_audio_signal()
    assert isinstance(audio, magewell.AudioSignal)
    assert isinstance(audio.lpcm, bool)
    assert isinstance(audio.channel_status_raw, bytes)
    assert len(audio.channel_status_raw) == 24
    if audio.sample_rate:
        assert audio.bits_per_sample > 0
