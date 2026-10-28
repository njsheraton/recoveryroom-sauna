import pytest

from recoveryroom_sauna import protocol as P


def state_frame(**kw) -> bytes:
    return P.encode_frame(P.Cmd.STATE_RSP, P.build_state_payload(**kw))


# ---------------------------------------------------------------- varint

@pytest.mark.parametrize("value,expected", [(0, b"\x00"), (1, b"\x01"), (127, b"\x7f"), (128, b"\x80\x01"),
                                            (300, b"\xac\x02"), (16383, b"\xff\x7f"), (16384, b"\x80\x80\x01")])
def test_varint_roundtrip(value, expected):
    assert P.encode_varint(value) == expected
    assert P.decode_varint(expected) == (value, len(expected))


def test_varint_incomplete_returns_none():
    assert P.decode_varint(b"\x80") is None


def test_varint_too_long_rejected():
    with pytest.raises(P.ProtocolError):
        P.decode_varint(b"\x80\x80\x80\x80\x80\x80")


# ---------------------------------------------------------------- framing

def test_encode_frame_layout():
    f = P.encode_frame(P.Cmd.STATE_REQ, b"\x02")
    assert f == b"\x00\x00\x00\x03" + b"\x04" + b"\x00\x00\x90\x02"


def test_control_frame_carries_sequence():
    f = P.encode_frame(P.Cmd.CONTROL_REQ, b"\xaa", seq=0x01020304)
    assert f == b"\x00\x00\x00\x03\x08\x00\x00\x93\x01\x02\x03\x04\xaa"
    [frame] = P.FrameDecoder().feed(f)
    assert frame.cmd == P.Cmd.CONTROL_REQ and frame.seq == 0x01020304 and frame.payload == b"\xaa"


def test_sequence_mismatch_rejected():
    with pytest.raises(ValueError):
        P.encode_frame(P.Cmd.CONTROL_REQ, b"")
    with pytest.raises(ValueError):
        P.encode_frame(P.Cmd.PING, b"", seq=1)


def test_fragmented_frames_reassemble():
    data = state_frame(power=True) + P.encode_frame(P.Cmd.PONG)
    dec = P.FrameDecoder()
    got = []
    for i in range(len(data)):
        got += dec.feed(data[i:i + 1])
    assert [f.cmd for f in got] == [P.Cmd.STATE_RSP, P.Cmd.PONG]
    assert dec.pending_bytes == 0


def test_coalesced_frames_split():
    data = P.encode_frame(P.Cmd.PONG) * 3 + state_frame()
    got = P.FrameDecoder().feed(data)
    assert [f.cmd for f in got] == [P.Cmd.PONG] * 3 + [P.Cmd.STATE_RSP]


def test_bad_prefix_raises_and_clears():
    dec = P.FrameDecoder()
    with pytest.raises(P.ProtocolError):
        dec.feed(b"\x00\x00\x00\x04\x01\x00")
    assert dec.pending_bytes == 0


def test_bad_prefix_detected_early():
    with pytest.raises(P.ProtocolError):
        P.FrameDecoder().feed(b"\x01")


def test_invalid_inner_length_rejected():
    dec = P.FrameDecoder()
    with pytest.raises(P.ProtocolError):
        dec.feed(b"\x00\x00\x00\x03" + P.encode_varint(P.MAX_INNER_LENGTH + 1))
    with pytest.raises(P.ProtocolError):
        P.FrameDecoder().feed(b"\x00\x00\x00\x03\x02\x00\x00")   # inner length 2 < 3


def test_buffer_bound_enforced():
    dec = P.FrameDecoder(max_buffer=64)
    with pytest.raises(P.BufferOverflow):
        dec.feed(b"\x00\x00\x00\x03" + P.encode_varint(100) + b"\x00" * 100)


def test_reserved_byte_must_be_zero():
    with pytest.raises(P.ProtocolError):
        P.FrameDecoder().feed(b"\x00\x00\x00\x03\x03\x01\x00\x16")


def test_unknown_command_is_still_a_frame():
    [f] = P.FrameDecoder().feed(b"\x00\x00\x00\x03\x04\x00\x12\x34\xff")
    assert f.cmd == 0x1234 and f.cmd_name == "0x1234" and f.payload == b"\xff"


def test_sequenced_frame_too_short():
    with pytest.raises(P.ProtocolError):
        P.FrameDecoder().feed(b"\x00\x00\x00\x03\x05\x00\x00\x94\x00\x00")


# ---------------------------------------------------------------- conversion

@pytest.mark.parametrize("c,f", [(20, 68), (55, 131), (80, 176), (0, 32), (100, 212)])
def test_c_to_f(c, f):
    assert P.c_to_f(c) == f


@pytest.mark.parametrize("f,c", [(68, 20), (131, 55), (176, 80), (32, 0), (100, 38)])
def test_f_to_c(f, c):
    assert P.f_to_c(f) == c


# ---------------------------------------------------------------- state payload

def test_state_payload_decodes_independent_bits():
    for power in (False, True):
        for inside in (False, True):
            for outside in (False, True):
                r = P.parse_state_payload(P.build_state_payload(power=power, internal_light=inside, external_light=outside,
                                                                target_f=176, current_f=131, set_hour=0, set_minute=45))
                assert (r.power, r.internal_light, r.external_light) == (power, inside, outside)
    assert r.target_c == 80 and r.current_c == 55 and r.timer_minutes == 45


def test_state_payload_fahrenheit_regardless_of_display_flag():
    r = P.parse_state_payload(P.build_state_payload(celsius_display=False, target_f=176, current_f=140))
    assert r.celsius_display is False and r.target_c == 80 and r.current_c == 60


def test_state_payload_too_short():
    with pytest.raises(P.ProtocolError):
        P.parse_state_payload(b"\x00" * 12)


def test_state_payload_preserves_extra_bytes():
    r = P.parse_state_payload(P.build_state_payload(extra=b"\xde\xad"))
    assert r.raw.endswith(b"\xde\xad")


@pytest.mark.parametrize("kw", [dict(set_hour=24), dict(set_minute=60), dict(preset_hour=25), dict(target_f=250), dict(current_f=251)])
def test_state_payload_impossible_values(kw):
    with pytest.raises(P.ProtocolError):
        P.parse_state_payload(P.build_state_payload(**kw))


# ---------------------------------------------------------------- login

def test_login_echoes_full_passcode_payload():
    pc = b"\x00\x0aABCDEFGHIJ"
    assert P.login_payload_from_passcode_response(pc) == pc
    with pytest.raises(P.ProtocolError):
        P.login_payload_from_passcode_response(b"")
    assert P.login_succeeded(b"\x00") and not P.login_succeeded(b"\x01") and not P.login_succeeded(b"")


# ---------------------------------------------------------------- control payloads

def test_flag_control_layout():
    p = P.build_flag_control(P.Flag.POWER, True)
    assert len(p) == 13 and p[0] == 1 and p[1] == 0 and p[2] == 0x08 and p[3] == 0x08
    p = P.build_flag_control(P.Flag.INTERNAL_LIGHT, False)
    assert p[2] == 0x02 and p[3] == 0x00
    with pytest.raises(ValueError):
        P.build_flag_control(P.Flag.UVB, True)


def test_target_control_validation():
    p = P.build_target_control(80)
    assert p[1] == 0x04 and p[7] == 176
    for bad in (19, 81, 80.0, True, "80"):
        with pytest.raises(ValueError):
            P.build_target_control(bad)


def test_timer_control_validation():
    p = P.build_timer_control(60)
    assert p[1] == 0x10 and p[9] == 60
    for bad in (0, 61, 300, 30.0, False):
        with pytest.raises(ValueError):
            P.build_timer_control(bad)


def test_confirmation_predicates():
    r = P.parse_state_payload(P.build_state_payload(power=True, target_f=175, set_hour=0, set_minute=29))
    assert P.confirms_flag(r, P.Flag.POWER, True) and not P.confirms_flag(r, P.Flag.POWER, False)
    assert P.confirms_target(r, 80)              # 176 vs 175 within 1F
    assert not P.confirms_target(r, 78)
    assert P.confirms_timer(r, 30) and P.confirms_timer(r, 29) and not P.confirms_timer(r, 31)
    r2 = P.parse_state_payload(P.build_state_payload(set_hour=1, set_minute=0))
    assert not P.confirms_timer(r2, 60)          # set_hour must be 0
