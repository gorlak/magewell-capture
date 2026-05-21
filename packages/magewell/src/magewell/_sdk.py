"""ctypes binding to the Magewell MWCapture SDK (``libMWCapture.so``).

Generic MWCapture API — works for any Magewell capture device. We bind the
subset needed to probe a device before capture starts:

    MWCaptureInitInstance / MWCaptureExitInstance   (global SDK lifecycle)
    MWRefreshDevice / MWGetChannelCount             (enumerate)
    MWGetDevicePath / MWOpenChannelByPath / MWCloseChannel
    MWGetChannelInfo                                (device identification)
    MWGetVideoSignalStatus                          (video input signal)
    MWGetAudioSignalStatus                          (audio input signal)

All type widths, struct layouts, and prototypes are mirrored verbatim from the
vendored SDK 3.3.1.1515 headers (see DECISIONS.md):
  - WinTypes.h:  BOOLEAN = char (1 byte), DWORD = unsigned int (4)
  - MWCaptureExtension.h is wrapped in ``#pragma pack(1)`` → all structs here
    set ``_pack_ = 1`` (byte-packed, no padding).
  - MWCapture.h:  ``#define HCHANNEL void *`` ; MW_RESULT enum, MW_SUCCEEDED = 0.

Importing this module does not load the native library or touch hardware; the
``.so`` is loaded lazily on first use.
"""
from __future__ import annotations

import ctypes
import glob
import os
from dataclasses import dataclass
from enum import IntEnum
from importlib.resources import files

# ---------------------------------------------------------------------------
# Path to the per-box native build (built by build_lib.sh; has proper DT_NEEDED).
# ---------------------------------------------------------------------------
_LIB_PATH = files(__package__).joinpath("_lib/libMWCapture.so")

MW_SUCCEEDED = 0


# ---------------------------------------------------------------------------
# Enums (mirrored from MWCaptureExtension.h / MWCommon.h)
# ---------------------------------------------------------------------------

class SignalState(IntEnum):
    """MWCAP_VIDEO_SIGNAL_STATE."""
    NONE = 0
    UNSUPPORTED = 1
    LOCKING = 2
    LOCKED = 3


class FamilyID(IntEnum):
    """MW_FAMILY_ID — device family."""
    PRO_CAPTURE = 0
    ECO_CAPTURE = 1
    USB_CAPTURE = 2


class VideoColorFormat(IntEnum):
    """MWCAP_VIDEO_COLOR_FORMAT."""
    UNKNOWN = 0x00
    RGB = 0x01
    YUV601 = 0x02
    YUV709 = 0x03
    YUV2020 = 0x04
    YUV2020C = 0x05


class VideoQuantizationRange(IntEnum):
    """MWCAP_VIDEO_QUANTIZATION_RANGE."""
    UNKNOWN = 0x00
    FULL = 0x01
    LIMITED = 0x02


class VideoSaturationRange(IntEnum):
    """MWCAP_VIDEO_SATURATION_RANGE."""
    UNKNOWN = 0x00
    FULL = 0x01
    LIMITED = 0x02
    EXTENDED_GAMUT = 0x03


class VideoFrameType(IntEnum):
    """MWCAP_VIDEO_FRAME_TYPE."""
    FRAME_2D = 0x00
    FRAME_3D_TOP_AND_BOTTOM_FULL = 0x01
    FRAME_3D_TOP_AND_BOTTOM_HALF = 0x02
    FRAME_3D_SIDE_BY_SIDE_FULL = 0x03
    FRAME_3D_SIDE_BY_SIDE_HALF = 0x04


# ---------------------------------------------------------------------------
# Structs (byte-packed, mirrored from headers — #pragma pack(1))
# ---------------------------------------------------------------------------

class MWCAP_VIDEO_SIGNAL_STATUS(ctypes.Structure):
    """Video input signal status (MWCaptureExtension.h)."""
    _pack_ = 1
    _fields_ = [
        ("state", ctypes.c_int),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("cy", ctypes.c_int),
        ("cxTotal", ctypes.c_int),
        ("cyTotal", ctypes.c_int),
        ("bInterlaced", ctypes.c_bool),
        ("dwFrameDuration", ctypes.c_uint),
        ("nAspectX", ctypes.c_int),
        ("nAspectY", ctypes.c_int),
        ("bSegmentedFrame", ctypes.c_bool),
        ("frameType", ctypes.c_int),
        ("colorFormat", ctypes.c_int),
        ("quantRange", ctypes.c_int),
        ("satRange", ctypes.c_int),
    ]


SIZEOF_VIDEO_SIGNAL_STATUS = 58

_MW_FAMILY_NAME_LEN = 64
_MW_PRODUCT_NAME_LEN = 64
_MW_FIRMWARE_NAME_LEN = 64
_MW_SERIAL_NO_LEN = 16


class MWCAP_CHANNEL_INFO(ctypes.Structure):
    """Device/channel identification (MWCaptureExtension.h)."""
    _pack_ = 1
    _fields_ = [
        ("wFamilyID", ctypes.c_ushort),
        ("wProductID", ctypes.c_ushort),
        ("chHardwareVersion", ctypes.c_char),
        ("byFirmwareID", ctypes.c_ubyte),
        ("dwFirmwareVersion", ctypes.c_uint),
        ("dwDriverVersion", ctypes.c_uint),
        ("szFamilyName", ctypes.c_char * _MW_FAMILY_NAME_LEN),
        ("szProductName", ctypes.c_char * _MW_PRODUCT_NAME_LEN),
        ("szFirmwareName", ctypes.c_char * _MW_FIRMWARE_NAME_LEN),
        ("szBoardSerialNo", ctypes.c_char * _MW_SERIAL_NO_LEN),
        ("byBoardIndex", ctypes.c_ubyte),
        ("byChannelIndex", ctypes.c_ubyte),
    ]


SIZEOF_CHANNEL_INFO = 224


class MWCAP_AUDIO_SIGNAL_STATUS(ctypes.Structure):
    """Audio input signal status (MWCaptureExtension.h).

    ``channelStatus`` is the raw 24-byte IEC 60958 channel-status block
    (union in the SDK; we expose as bytes for simplicity).
    """
    _pack_ = 1
    _fields_ = [
        ("wChannelValid", ctypes.c_ushort),
        ("bLPCM", ctypes.c_bool),
        ("cBitsPerSample", ctypes.c_ubyte),
        ("dwSampleRate", ctypes.c_uint),
        ("bChannelStatusValid", ctypes.c_bool),
        ("channelStatus", ctypes.c_ubyte * 24),
    ]


SIZEOF_AUDIO_SIGNAL_STATUS = 33


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MWError(RuntimeError):
    """An MWCapture SDK call failed."""


class MWAccessError(MWError):
    """A Magewell USB device is present but not accessible to this user.

    Almost always means the one-time privileged setup (the udev rule) has not
    been applied yet — see :func:`access_ok` and ``./setup.sh``.
    """


# ---------------------------------------------------------------------------
# One-time-setup runtime check (udev rule from ./setup.sh)
# ---------------------------------------------------------------------------

_MAGEWELL_USB_VENDOR_ID = "2935"
_SETUP_HINT = (
    "run the one-time setup: 'sudo ./setup.sh' "
    "(installs the udev rule; see README)"
)


def find_usb_node() -> str | None:
    """Return ``/dev/bus/usb/BBB/DDD`` of the first Magewell USB device, or
    ``None``. Pure sysfs lookup — no SDK, no privileges."""
    for dev_dir in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(dev_dir, "idVendor")) as f:
                if f.read().strip().lower() != _MAGEWELL_USB_VENDOR_ID:
                    continue
            with open(os.path.join(dev_dir, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(dev_dir, "devnum")) as f:
                num = int(f.read())
        except (OSError, ValueError):
            continue
        return f"/dev/bus/usb/{bus:03d}/{num:03d}"
    return None


def access_ok() -> bool:
    """``True`` iff a Magewell USB device is present and its raw USB node is
    rw for the current user (i.e. the udev rule from ``./setup.sh`` is in
    effect)."""
    node = find_usb_node()
    return node is not None and os.access(node, os.R_OK | os.W_OK)


def _require_usb_access() -> None:
    node = find_usb_node()
    if node is None:
        raise MWAccessError("no Magewell USB device found (is it plugged in?)")
    if not os.access(node, os.R_OK | os.W_OK):
        raise MWAccessError(
            f"{node} is not read-writable by uid {os.getuid()}; the MWCapture "
            f"SDK needs rw on the raw USB node to read signal status — "
            f"{_SETUP_HINT}"
        )


# ---------------------------------------------------------------------------
# Dataclasses — decoded, Pythonic results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    """Decoded video input-signal status."""
    state: SignalState
    width: int
    height: int
    fps: float
    interlaced: bool
    aspect_x: int
    aspect_y: int
    frame_duration_100ns: int
    color_format: VideoColorFormat
    quant_range: VideoQuantizationRange
    sat_range: VideoSaturationRange
    frame_type: VideoFrameType
    segmented_frame: bool

    @property
    def locked(self) -> bool:
        return self.state is SignalState.LOCKED

    def __str__(self) -> str:
        if not self.locked:
            return f"<{self.state.name}>"
        scan = "i" if self.interlaced else "p"
        return f"{self.width}x{self.height}{scan}{self.fps:g}"


@dataclass(frozen=True)
class ChannelInfo:
    """Device/channel identification."""
    family: FamilyID
    product_id: int
    hardware_version: str
    firmware_id: int
    firmware_version: int
    driver_version: int
    family_name: str
    product_name: str
    firmware_name: str
    serial_no: str
    board_index: int
    channel_index: int

    def __str__(self) -> str:
        return f"{self.product_name} ({self.family.name}, serial={self.serial_no})"


@dataclass(frozen=True)
class AudioSignal:
    """Decoded audio input-signal status."""
    channels_valid: int
    lpcm: bool
    bits_per_sample: int
    sample_rate: int
    channel_status_valid: bool
    channel_status_raw: bytes

    @property
    def num_channels(self) -> int:
        """Count of valid channel *pairs* (each bit = one stereo pair)."""
        return bin(self.channels_valid & 0xF).count("1") * 2

    def __str__(self) -> str:
        if not self.sample_rate:
            return "<no audio>"
        return f"{self.sample_rate}Hz/{self.bits_per_sample}bit {'LPCM' if self.lpcm else 'compressed'}"


# ---------------------------------------------------------------------------
# Library loading + function binding
# ---------------------------------------------------------------------------

_lib: ctypes.CDLL | None = None


def _bind() -> ctypes.CDLL:
    try:
        lib = ctypes.CDLL(str(_LIB_PATH))
    except OSError as e:
        raise MWError(
            f"could not load {_LIB_PATH}; run packages/magewell/build_lib.sh "
            f"to build the native library ({e})"
        ) from e

    # lifecycle
    lib.MWCaptureInitInstance.restype = ctypes.c_byte
    lib.MWCaptureInitInstance.argtypes = []
    lib.MWCaptureExitInstance.restype = None
    lib.MWCaptureExitInstance.argtypes = []
    # enumerate
    lib.MWRefreshDevice.restype = ctypes.c_int
    lib.MWRefreshDevice.argtypes = []
    lib.MWGetChannelCount.restype = ctypes.c_int
    lib.MWGetChannelCount.argtypes = []
    # open / close
    lib.MWGetDevicePath.restype = ctypes.c_int
    lib.MWGetDevicePath.argtypes = [ctypes.c_int, ctypes.c_char_p]
    lib.MWOpenChannelByPath.restype = ctypes.c_void_p
    lib.MWOpenChannelByPath.argtypes = [ctypes.c_char_p]
    lib.MWCloseChannel.restype = None
    lib.MWCloseChannel.argtypes = [ctypes.c_void_p]
    # channel info
    lib.MWGetChannelInfo.restype = ctypes.c_int
    lib.MWGetChannelInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(MWCAP_CHANNEL_INFO)
    ]
    # video signal
    lib.MWGetVideoSignalStatus.restype = ctypes.c_int
    lib.MWGetVideoSignalStatus.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(MWCAP_VIDEO_SIGNAL_STATUS)
    ]
    # audio signal
    lib.MWGetAudioSignalStatus.restype = ctypes.c_int
    lib.MWGetAudioSignalStatus.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(MWCAP_AUDIO_SIGNAL_STATUS)
    ]
    return lib


def _get_lib() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _bind()
    return _lib


class _Instance:
    """Context manager for the global SDK instance (Init/Exit + RefreshDevice)."""
    def __enter__(self) -> ctypes.CDLL:
        self._lib = _get_lib()
        if not self._lib.MWCaptureInitInstance():
            raise MWError("MWCaptureInitInstance failed")
        if self._lib.MWRefreshDevice() != MW_SUCCEEDED:
            self._lib.MWCaptureExitInstance()
            raise MWError("MWRefreshDevice failed")
        return self._lib

    def __exit__(self, *exc) -> None:
        self._lib.MWCaptureExitInstance()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open_channel(lib: ctypes.CDLL, index: int):
    """Validate index, get path, require USB access, open the channel.
    Returns (handle, path_bytes). Caller must MWCloseChannel."""
    n = int(lib.MWGetChannelCount())
    if n <= 0:
        raise MWError("no Magewell capture channels found")
    if not 0 <= index < n:
        raise MWError(f"channel index {index} out of range (0..{n - 1})")
    path = ctypes.create_string_buffer(256)
    if lib.MWGetDevicePath(index, path) != MW_SUCCEEDED:
        raise MWError(f"MWGetDevicePath({index}) failed")
    _require_usb_access()
    handle = lib.MWOpenChannelByPath(path)
    if not handle:
        raise MWError(f"MWOpenChannelByPath failed for {path.value!r}")
    return handle


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def channel_count() -> int:
    """Number of Magewell capture channels currently present."""
    with _Instance() as lib:
        return int(lib.MWGetChannelCount())


def read_signal(index: int = 0) -> Signal:
    """Read the live video input-signal status for channel *index*."""
    with _Instance() as lib:
        handle = _open_channel(lib, index)
        try:
            vss = MWCAP_VIDEO_SIGNAL_STATUS()
            rc = lib.MWGetVideoSignalStatus(handle, ctypes.byref(vss))
            if rc != MW_SUCCEEDED:
                raise MWError(
                    f"MWGetVideoSignalStatus failed (MW_RESULT={rc})"
                )
        finally:
            lib.MWCloseChannel(handle)

    dur = int(vss.dwFrameDuration)
    fps = round(10_000_000 / dur, 3) if dur else 0.0
    return Signal(
        state=SignalState(vss.state),
        width=int(vss.cx),
        height=int(vss.cy),
        fps=fps,
        interlaced=vss.bInterlaced,
        aspect_x=int(vss.nAspectX),
        aspect_y=int(vss.nAspectY),
        frame_duration_100ns=dur,
        color_format=VideoColorFormat(vss.colorFormat),
        quant_range=VideoQuantizationRange(vss.quantRange),
        sat_range=VideoSaturationRange(vss.satRange),
        frame_type=VideoFrameType(vss.frameType),
        segmented_frame=vss.bSegmentedFrame,
    )


def read_channel_info(index: int = 0) -> ChannelInfo:
    """Read device/channel identification for channel *index*."""
    with _Instance() as lib:
        handle = _open_channel(lib, index)
        try:
            ci = MWCAP_CHANNEL_INFO()
            rc = lib.MWGetChannelInfo(handle, ctypes.byref(ci))
            if rc != MW_SUCCEEDED:
                raise MWError(f"MWGetChannelInfo failed (MW_RESULT={rc})")
        finally:
            lib.MWCloseChannel(handle)

    return ChannelInfo(
        family=FamilyID(ci.wFamilyID),
        product_id=int(ci.wProductID),
        hardware_version=ci.chHardwareVersion.decode(errors="replace"),
        firmware_id=int(ci.byFirmwareID),
        firmware_version=int(ci.dwFirmwareVersion),
        driver_version=int(ci.dwDriverVersion),
        family_name=ci.szFamilyName.decode(errors="replace").rstrip("\x00"),
        product_name=ci.szProductName.decode(errors="replace").rstrip("\x00"),
        firmware_name=ci.szFirmwareName.decode(errors="replace").rstrip("\x00"),
        serial_no=ci.szBoardSerialNo.decode(errors="replace").rstrip("\x00"),
        board_index=int(ci.byBoardIndex),
        channel_index=int(ci.byChannelIndex),
    )


def read_audio_signal(index: int = 0) -> AudioSignal:
    """Read the live audio input-signal status for channel *index*."""
    with _Instance() as lib:
        handle = _open_channel(lib, index)
        try:
            ass = MWCAP_AUDIO_SIGNAL_STATUS()
            rc = lib.MWGetAudioSignalStatus(handle, ctypes.byref(ass))
            if rc != MW_SUCCEEDED:
                raise MWError(
                    f"MWGetAudioSignalStatus failed (MW_RESULT={rc})"
                )
        finally:
            lib.MWCloseChannel(handle)

    return AudioSignal(
        channels_valid=int(ass.wChannelValid),
        lpcm=ass.bLPCM,
        bits_per_sample=int(ass.cBitsPerSample),
        sample_rate=int(ass.dwSampleRate),
        channel_status_valid=ass.bChannelStatusValid,
        channel_status_raw=bytes(ass.channelStatus),
    )
