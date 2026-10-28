"""Local Clearlight LAN protocol: framing, field conversion, payload builders.

PROVISIONAL. This mapping is transcribed from the handoff's description of a
working reference implementation. It is not a manufacturer-supported API
contract. Every byte offset here must be confirmed against the shop unit with
non-controlling reads before any control frame is ever sent.

Framing (handoff section 5):
    prefix  00 00 00 03
    varint  unsigned base-128 little-endian length of `inner`
    inner   00 | cmd (u16 BE) | [seq (u32 BE) for control frames] | payload
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

FRAME_PREFIX = b"\x00\x00\x00\x03"
RESERVED_BYTE = 0x00
MAX_INNER_LENGTH = 4096          # bound on a single frame's inner length
MAX_BUFFER_LENGTH = 64 * 1024    # bound on the receive buffer
MAX_VARINT_BYTES = 5

STATE_PAYLOAD_MIN_LEN = 13
CONTROL_PAYLOAD_LEN = 13
STATE_REQUEST_PAYLOAD = b"\x02"

TEMP_MIN_C = 20
TEMP_MAX_C = 80
TIMER_MIN_MINUTES = 1
TIMER_MAX_MINUTES = 60


class Cmd(IntEnum):
    PASSCODE_REQ = 0x0006
    PASSCODE_RSP = 0x0007
    LOGIN_REQ = 0x0008
    LOGIN_RSP = 0x0009
    PING = 0x0015
    PONG = 0x0016
    STATE_REQ = 0x0090
    STATE_RSP = 0x0091
    CONTROL_REQ = 0x0093
    CONTROL_ACK = 0x0094


# Commands whose inner message carries a 4-byte sequence number before the payload.
SEQUENCED_COMMANDS = frozenset({Cmd.CONTROL_REQ, Cmd.CONTROL_ACK})


class Flag(IntEnum):
    EXTERNAL_LIGHT = 0x01
    INTERNAL_LIGHT = 0x02
    PRESET_ENABLED = 0x04
    POWER = 0x08
    CELSIUS_DISPLAY = 0x10
    UVB = 0x20


class ProtocolError(ValueError):
    """Malformed or impossible data on the wire."""


class FrameTooLarge(ProtocolError):
    pass


class BufferOverflow(ProtocolError):
    pass


# --------------------------------------------------------------------------- #
# Unit conversion
# --------------------------------------------------------------------------- #

def f_to_c(f: int) -> int:
    return round((f - 32) * 5 / 9)


def c_to_f(c: int) -> int:
    return round(c * 9 / 5 + 32)


# --------------------------------------------------------------------------- #
# Varint
# --------------------------------------------------------------------------- #

def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(buf: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int] | None:
    """Return (value, bytes_consumed) or None if more bytes are needed.

    Raises ProtocolError on an over-long varint.
    """
    result = 0
    shift = 0
    pos = offset
    while True:
        if pos >= len(buf):
            return None
        if pos - offset >= MAX_VARINT_BYTES:
            raise ProtocolError("varint too long")
        byte = buf[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not byte & 0x80:
            return result, pos - offset
        shift += 7


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Frame:
    cmd: int
    seq: int | None
    payload: bytes

    @property
    def cmd_name(self) -> str:
        try:
            return Cmd(self.cmd).name
        except ValueError:
            return f"0x{self.cmd:04x}"


def encode_frame(cmd: int, payload: bytes = b"", seq: int | None = None) -> bytes:
    if not 0 <= cmd <= 0xFFFF:
        raise ValueError("cmd out of range")
    if (cmd in SEQUENCED_COMMANDS) != (seq is not None):
        raise ValueError(f"command 0x{cmd:04x} sequence number mismatch (seq={seq!r})")
    inner = bytes([RESERVED_BYTE]) + cmd.to_bytes(2, "big")
    if seq is not None:
        if not 0 <= seq <= 0xFFFFFFFF:
            raise ValueError("seq out of range")
        inner += seq.to_bytes(4, "big")
    inner += payload
    if len(inner) > MAX_INNER_LENGTH:
        raise FrameTooLarge(f"inner length {len(inner)} exceeds {MAX_INNER_LENGTH}")
    return FRAME_PREFIX + encode_varint(len(inner)) + inner


def _parse_inner(inner: bytes) -> Frame:
    if len(inner) < 3:
        raise ProtocolError(f"inner message too short ({len(inner)} bytes)")
    if inner[0] != RESERVED_BYTE:
        raise ProtocolError(f"unexpected reserved byte 0x{inner[0]:02x}")
    cmd = int.from_bytes(inner[1:3], "big")
    body = inner[3:]
    if cmd in SEQUENCED_COMMANDS:
        if len(body) < 4:
            raise ProtocolError("sequenced frame missing sequence number")
        return Frame(cmd=cmd, seq=int.from_bytes(body[:4], "big"), payload=bytes(body[4:]))
    return Frame(cmd=cmd, seq=None, payload=bytes(body))


class FrameDecoder:
    """Incremental decoder that tolerates partial reads and coalesced frames.

    feed(data) returns the complete frames found so far. Corruption raises
    ProtocolError; the caller should drop the connection and reconnect rather
    than try to resynchronise on a heating appliance.
    """

    def __init__(self, max_buffer: int = MAX_BUFFER_LENGTH, max_inner: int = MAX_INNER_LENGTH) -> None:
        self._buf = bytearray()
        self._max_buffer = max_buffer
        self._max_inner = max_inner

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        if len(self._buf) + len(data) > self._max_buffer:
            self._buf.clear()
            raise BufferOverflow("receive buffer bound exceeded")
        self._buf.extend(data)
        frames: list[Frame] = []
        while True:
            frame = self._try_pop()
            if frame is None:
                return frames
            frames.append(frame)

    def _try_pop(self) -> Frame | None:
        buf = self._buf
        if len(buf) < len(FRAME_PREFIX):
            if not FRAME_PREFIX.startswith(bytes(buf)):
                buf.clear()
                raise ProtocolError("bad frame prefix")
            return None
        if bytes(buf[: len(FRAME_PREFIX)]) != FRAME_PREFIX:
            buf.clear()
            raise ProtocolError("bad frame prefix")
        try:
            decoded = decode_varint(buf, len(FRAME_PREFIX))
        except ProtocolError:
            buf.clear()
            raise
        if decoded is None:
            return None
        inner_len, varint_len = decoded
        if inner_len < 3 or inner_len > self._max_inner:
            buf.clear()
            raise ProtocolError(f"invalid inner length {inner_len}")
        start = len(FRAME_PREFIX) + varint_len
        end = start + inner_len
        if len(buf) < end:
            return None
        inner = bytes(buf[start:end])
        del buf[:end]
        try:
            return _parse_inner(inner)
        except ProtocolError:
            buf.clear()
            raise


# --------------------------------------------------------------------------- #
# State report (response 0x0091)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StateReport:
    """Decoded 0x0091 payload. Temperatures decoded from Fahrenheit regardless
    of the display flag (reference behaviour)."""

    power: bool
    internal_light: bool
    external_light: bool
    preset_enabled: bool
    celsius_display: bool
    uvb: bool
    led: int
    right: int
    left: int
    target_f: int
    set_hour: int
    set_minute: int
    preset_hour: int
    preset_minute: int
    serial: int
    current_f: int
    heart: int
    raw: bytes

    @property
    def target_c(self) -> int:
        return f_to_c(self.target_f)

    @property
    def current_c(self) -> int:
        return f_to_c(self.current_f)

    @property
    def timer_minutes(self) -> int:
        return self.set_hour * 60 + self.set_minute

    def to_dict(self) -> dict:
        return {
            "power": self.power,
            "internalLight": self.internal_light,
            "externalLight": self.external_light,
            "presetEnabled": self.preset_enabled,
            "celsiusDisplay": self.celsius_display,
            "uvb": self.uvb,
            "targetC": self.target_c,
            "targetF": self.target_f,
            "currentC": self.current_c,
            "currentF": self.current_f,
            "setHour": self.set_hour,
            "setMinute": self.set_minute,
            "timerMinutes": self.timer_minutes,
            "presetHour": self.preset_hour,
            "presetMinute": self.preset_minute,
            "serialField": self.serial,
            "heart": self.heart,
        }


# Plausibility bounds. The sensor might legitimately read below 20C in a cold
# shop, but a reading outside these bounds is treated as malformed.
TARGET_F_MIN, TARGET_F_MAX = 32, 230
CURRENT_F_MIN, CURRENT_F_MAX = 0, 250


def parse_state_payload(payload: bytes) -> StateReport:
    if len(payload) < STATE_PAYLOAD_MIN_LEN:
        raise ProtocolError(f"state payload too short ({len(payload)} < {STATE_PAYLOAD_MIN_LEN})")
    flags = payload[1]
    target_f = payload[5]
    set_hour = payload[6]
    set_minute = payload[7]
    preset_hour = payload[8]
    preset_minute = payload[9]
    current_f = payload[11]
    if not TARGET_F_MIN <= target_f <= TARGET_F_MAX:
        raise ProtocolError(f"impossible target temperature {target_f}F")
    if not CURRENT_F_MIN <= current_f <= CURRENT_F_MAX:
        raise ProtocolError(f"impossible current temperature {current_f}F")
    if set_hour > 23 or set_minute > 59:
        raise ProtocolError(f"impossible timer {set_hour}:{set_minute:02d}")
    if preset_hour > 23 or preset_minute > 59:
        raise ProtocolError(f"impossible preset {preset_hour}:{preset_minute:02d}")
    return StateReport(
        power=bool(flags & Flag.POWER),
        internal_light=bool(flags & Flag.INTERNAL_LIGHT),
        external_light=bool(flags & Flag.EXTERNAL_LIGHT),
        preset_enabled=bool(flags & Flag.PRESET_ENABLED),
        celsius_display=bool(flags & Flag.CELSIUS_DISPLAY),
        uvb=bool(flags & Flag.UVB),
        led=payload[2],
        right=payload[3],
        left=payload[4],
        target_f=target_f,
        set_hour=set_hour,
        set_minute=set_minute,
        preset_hour=preset_hour,
        preset_minute=preset_minute,
        serial=payload[10],
        current_f=current_f,
        heart=payload[12],
        raw=bytes(payload),
    )


def build_state_payload(
    *,
    power: bool = False,
    internal_light: bool = False,
    external_light: bool = False,
    preset_enabled: bool = False,
    celsius_display: bool = True,
    uvb: bool = False,
    led: int = 0,
    right: int = 0,
    left: int = 0,
    target_f: int = 68,
    set_hour: int = 0,
    set_minute: int = 0,
    preset_hour: int = 0,
    preset_minute: int = 0,
    serial: int = 0,
    current_f: int = 68,
    heart: int = 0,
    extra: bytes = b"",
) -> bytes:
    """Build a 0x0091 payload (used by the simulator and fixtures)."""
    flags = 0
    if external_light:
        flags |= Flag.EXTERNAL_LIGHT
    if internal_light:
        flags |= Flag.INTERNAL_LIGHT
    if preset_enabled:
        flags |= Flag.PRESET_ENABLED
    if power:
        flags |= Flag.POWER
    if celsius_display:
        flags |= Flag.CELSIUS_DISPLAY
    if uvb:
        flags |= Flag.UVB
    return bytes([
        0x00, flags, led & 0xFF, right & 0xFF, left & 0xFF,
        target_f & 0xFF, set_hour & 0xFF, set_minute & 0xFF,
        preset_hour & 0xFF, preset_minute & 0xFF, serial & 0xFF,
        current_f & 0xFF, heart & 0xFF,
    ]) + extra


# --------------------------------------------------------------------------- #
# Login payloads
# --------------------------------------------------------------------------- #

def login_payload_from_passcode_response(passcode_payload: bytes) -> bytes:
    """Login echoes the complete passcode response payload, wrapper included."""
    if not passcode_payload:
        raise ProtocolError("empty passcode response")
    return bytes(passcode_payload)


def login_succeeded(login_response_payload: bytes) -> bool:
    return len(login_response_payload) >= 1 and login_response_payload[0] == 0x00


# --------------------------------------------------------------------------- #
# Control payloads (reference section 6). Building a payload sends nothing;
# the device layer refuses to transmit control frames unless writes are
# explicitly enabled by commissioning configuration.
# --------------------------------------------------------------------------- #

class ControlKind(IntEnum):
    FLAGS = 0x00
    TARGET = 0x04
    TIMER = 0x10


def _control_base() -> bytearray:
    payload = bytearray(CONTROL_PAYLOAD_LEN)
    payload[0] = 0x01
    return payload


def build_flag_control(flag: Flag, enabled: bool) -> bytes:
    if flag not in (Flag.POWER, Flag.INTERNAL_LIGHT, Flag.EXTERNAL_LIGHT):
        raise ValueError("only power and the two lights may be controlled")
    payload = _control_base()
    payload[1] = ControlKind.FLAGS
    payload[2] = int(flag)
    payload[3] = int(flag) if enabled else 0x00
    return bytes(payload)


def build_target_control(target_c: int) -> bytes:
    if not isinstance(target_c, int) or isinstance(target_c, bool):
        raise ValueError("target must be an integer Celsius value")
    if not TEMP_MIN_C <= target_c <= TEMP_MAX_C:
        raise ValueError(f"target {target_c}C outside {TEMP_MIN_C}-{TEMP_MAX_C}C")
    payload = _control_base()
    payload[1] = ControlKind.TARGET
    payload[7] = c_to_f(target_c)
    return bytes(payload)


def build_timer_control(minutes: int) -> bytes:
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError("timer must be an integer number of minutes")
    if not TIMER_MIN_MINUTES <= minutes <= TIMER_MAX_MINUTES:
        raise ValueError(f"timer {minutes} outside {TIMER_MIN_MINUTES}-{TIMER_MAX_MINUTES} minutes")
    payload = _control_base()
    payload[1] = ControlKind.TIMER
    payload[9] = minutes
    return bytes(payload)


# --------------------------------------------------------------------------- #
# Confirmation predicates (reference section 6). Pure functions over fresh
# readback so they can be unit tested without a device.
# --------------------------------------------------------------------------- #

def confirms_flag(report: StateReport, flag: Flag, enabled: bool) -> bool:
    if flag == Flag.POWER:
        return report.power == enabled
    if flag == Flag.INTERNAL_LIGHT:
        return report.internal_light == enabled
    if flag == Flag.EXTERNAL_LIGHT:
        return report.external_light == enabled
    return False


def confirms_target(report: StateReport, target_c: int) -> bool:
    return abs(report.target_f - c_to_f(target_c)) <= 1


def confirms_timer(report: StateReport, minutes: int) -> bool:
    return report.set_hour == 0 and (minutes - 1) <= report.set_minute <= minutes
