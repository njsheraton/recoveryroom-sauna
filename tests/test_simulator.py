from recoveryroom_sauna import protocol as P
from recoveryroom_sauna.simulator import SimulatedSauna


def test_model_heats_only_when_on_and_timer_expires():
    s = SimulatedSauna(target_f=176, heat_rate_f_per_min=10)
    s.tick(60)
    assert not s.power and s.current_f == 68
    s.apply_control(P.build_flag_control(P.Flag.POWER, True))
    assert s.power and s.timer_seconds == 3600
    s.tick(60)
    assert s.current_f == 78
    s.apply_control(P.build_timer_control(1))
    s.tick(60)
    assert s.timer_seconds == 0 and s.power is False      # hardware timer expiry => OFF


def test_model_lights_independent_of_power():
    s = SimulatedSauna()
    assert s.apply_control(P.build_flag_control(P.Flag.INTERNAL_LIGHT, True))
    assert s.internal_light and not s.external_light and not s.power
    assert s.apply_control(P.build_flag_control(P.Flag.EXTERNAL_LIGHT, True))
    assert s.apply_control(P.build_flag_control(P.Flag.INTERNAL_LIGHT, False))
    assert not s.internal_light and s.external_light


def test_model_rejects_malformed_control():
    s = SimulatedSauna()
    assert not s.apply_control(b"\x00" * 13)
    assert not s.apply_control(b"\x01\x00\x20\x20" + b"\x00" * 9)   # UVB mask refused
    assert not s.apply_control(b"\x01")


def test_state_payload_roundtrip():
    s = SimulatedSauna(power=True, target_f=176, timer_seconds=45 * 60, current_f=131.4)
    r = P.parse_state_payload(s.state_payload())
    assert r.power and r.target_c == 80 and r.set_minute == 45 and r.current_c == 55
