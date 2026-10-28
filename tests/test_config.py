import json

import pytest

from recoveryroom_sauna.config import ConfigError, load_config, load_config_dict

BASE = {"schemaVersion": 1}


def test_example_config_loads_and_blocks_everything(tmp_path):
    cfg = load_config("config/example.config.json")
    blockers = cfg.scheduled_heating_blockers()
    assert "commissioning.writesEnabled is false" in blockers
    assert "businessHours.weekly is null (hours not confirmed)" in blockers
    assert "continuousOperationLimitMinutes is null (no approved maximum run)" in blockers
    assert "businessHours.autoStartEnabled is false" in blockers
    assert "saunaHost is null (device identity not confirmed)" in blockers
    assert cfg.manual_heating_blockers()
    assert cfg.private_dir.is_absolute()


def test_simulated_config_loads():
    cfg = load_config("config/simulated.config.json")
    assert cfg.simulate and cfg.business_hours.configured
    assert "saunaHost is null (device identity not confirmed)" not in cfg.scheduled_heating_blockers()
    assert "commissioning.writesEnabled is false" in cfg.scheduled_heating_blockers()


def test_defaults_match_handoff_contract():
    cfg = load_config_dict(BASE)
    assert (cfg.sauna_port, cfg.web_bind, cfg.web_port) == (12416, "127.0.0.1", 8787)
    assert (cfg.target_temp_c, cfg.ready_temp_c, cfg.hardware_timer_max_minutes) == (80, 55, 60)
    assert (cfg.renew_every_minutes, cfg.delivery_reserve_seconds) == (30, 120)
    assert cfg.business_hours.preheat_lead_minutes == 30 and cfg.business_hours.shutdown_after_close_minutes == 30
    assert cfg.auto_start_enabled is False and cfg.require_daily_arming is True
    assert cfg.continuous_operation_limit_minutes is None
    assert not cfg.cloud_fallback.enabled and not cfg.remote_access.enabled and not cfg.commissioning.writes_enabled


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config_dict({**BASE, "autoStartEnabled": True})
    with pytest.raises(ConfigError, match="unknown businessHours keys"):
        load_config_dict({**BASE, "businessHours": {"autostart": True}})


def test_schema_version_required():
    with pytest.raises(ConfigError):
        load_config_dict({})


def test_non_loopback_bind_rejected():
    with pytest.raises(ConfigError, match="loopback"):
        load_config_dict({**BASE, "webBind": "0.0.0.0"})


@pytest.mark.parametrize("key,value", [("targetTempC", 81), ("targetTempC", 19), ("hardwareTimerMaxMinutes", 61),
                                       ("hardwareTimerMaxMinutes", 300), ("renewEveryMinutes", 60),
                                       ("continuousOperationLimitMinutes", 29), ("continuousOperationLimitMinutes", 1441),
                                       ("continuousOperationLimitMinutes", "300"), ("saunaHost", ""), ("webPort", 80)])
def test_range_validation(key, value):
    with pytest.raises(ConfigError):
        load_config_dict({**BASE, key: value})


def test_ready_cannot_exceed_target():
    with pytest.raises(ConfigError):
        load_config_dict({**BASE, "targetTempC": 50, "readyTempC": 55})


def test_bad_hours_surface_as_config_error():
    with pytest.raises(ConfigError, match="businessHours"):
        load_config_dict({**BASE, "businessHours": {"weekly": {"monday": {"open": "17:00", "close": "09:00"}}}})


def test_no_hidden_all_day_default():
    """An all-day schedule needs explicit hours AND an approved limit; nothing enables it implicitly."""
    weekly = {d: {"open": "00:00", "close": "23:59"} for d in
              ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
    cfg = load_config_dict({**BASE, "saunaHost": "192.0.2.10", "businessHours": {"weekly": weekly, "autoStartEnabled": True},
                            "commissioning": {"writesEnabled": True, "inspectionProcedureApproved": True,
                                              "manufacturerRemoteStartApproved": True}})
    assert cfg.scheduled_heating_blockers() == ["continuousOperationLimitMinutes is null (no approved maximum run)"]


def test_fully_approved_config_has_no_config_level_blockers():
    weekly = {d: {"open": "09:00", "close": "17:00"} for d in
              ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
    cfg = load_config_dict({**BASE, "saunaHost": "192.0.2.10", "continuousOperationLimitMinutes": 300,
                            "businessHours": {"weekly": weekly, "autoStartEnabled": True},
                            "commissioning": {"writesEnabled": True, "inspectionProcedureApproved": True,
                                              "manufacturerRemoteStartApproved": True}})
    assert cfg.scheduled_heating_blockers() == []
    assert cfg.manual_heating_blockers() == []


def test_missing_and_invalid_files(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.json")
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError, match="valid JSON"):
        load_config(p)
    p.write_text(json.dumps({"schemaVersion": 1, "privateDir": "priv"}))
    assert load_config(p).private_dir == (tmp_path / "priv").resolve()
