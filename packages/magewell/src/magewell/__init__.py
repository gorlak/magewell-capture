"""magewell — ctypes wrapper over the Magewell MWCapture SDK.

Public API::

    from magewell import read_signal, read_channel_info, read_audio_signal
    sig = read_signal()
    info = read_channel_info()
    audio = read_audio_signal()

Importing this package does not load the library or touch hardware; that happens
lazily on first call. See ``DECISIONS.md`` at the repo root.
"""

from ._sdk import (
    AudioSignal,
    ChannelInfo,
    FamilyID,
    MWAccessError,
    MWError,
    Signal,
    SignalState,
    VideoColorFormat,
    VideoFrameType,
    VideoQuantizationRange,
    VideoSaturationRange,
    access_ok,
    channel_count,
    find_usb_node,
    read_audio_signal,
    read_channel_info,
    read_signal,
)

__all__ = [
    "read_signal",
    "read_channel_info",
    "read_audio_signal",
    "channel_count",
    "access_ok",
    "find_usb_node",
    "Signal",
    "ChannelInfo",
    "AudioSignal",
    "SignalState",
    "FamilyID",
    "VideoColorFormat",
    "VideoFrameType",
    "VideoQuantizationRange",
    "VideoSaturationRange",
    "MWError",
    "MWAccessError",
]

__version__ = "0.1.0"
