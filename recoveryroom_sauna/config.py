"""Configuration contract (handoff section 13).

Non-secret JSON config. Null means unconfigured and blocks scheduled heating.
Secrets (session hashes, pairing PINs, any future cloud identity) live in a
separate private directory, never in this file.

Schema is versioned; v1 is defined here. Unknown top-level keys are rejected
so typos cannot silently disable a safety field.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .business_hours import BusinessHoursPolicy, HoursError

SCHEMA_VERSION = 1


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CloudFallbackConfig:
    enabled: bool = False


@dataclass(frozen=True)
class RemoteAccessConfig:
    enabled: bool = False
    public_hostname: str | None = None


@dataclass(frozen=True)
class CommissioningConfig:
    writes_enabled: bool = False
    inspection_procedure_approved: bool = False
    manufacturer_remote_start_approved: bool = False
    approved_by: str | None = None
    approved_on: str | None = None


@dataclass(frozen=True)
class SecurityConfig:
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8787", "localhost:8787")
    session_lifetime_days: int = 30
    pairing_pin_lifetime_minutes: int = 10
    pairing_max_attempts: int = 5


@dataclass(frozen=True)
class AppConfig:
    installation_name: str
    sauna_host: str | None
    sauna_port: int
    web_bind: str
    web_port: int
    timezone: str
    target_temp_c: int
    ready_temp_c: int
    hardware_timer_max_minutes: int
    renew_every_minutes: int
    delivery_reserve_seconds: int
    business_hours: BusinessHoursPolicy
    auto_start_enabled: bool
    require_daily_arming: bool
    continuous_operation_limit_minutes: int | None
    cloud_fallback: CloudFallbackConfig
    remote_access: RemoteAccessConfig
    commissioning: CommissioningConfig
    security: SecurityConfig
    simulate: bool = False
    private_dir: Path = field(default_factory=lambda: Path("private"))
    source_path: Path | None = None

    # ------------------------------------------------------------- blockers

    def scheduled_heating_blockers(self) -> list[str]:
        """Every reason automatic/scheduled heating must stay disabled.
        Empty list is necessary but not sufficient: runtime arming, fresh OFF
        and no pending Stop are checked separately by the supervisor."""
        reasons: list[str] = []
        if not self.commissioning.writes_enabled:
            reasons.append("commissioning.writesEnabled is false")
        if not self.commissioning.inspection_procedure_approved:
            reasons.append("commissioning.inspectionProcedureApproved is false")
        if not self.commissioning.manufacturer_remote_start_approved:
            reasons.append("commissioning.manufacturerRemoteStartApproved is false")
        if not self.business_hours.configured:
            reasons.append("businessHours.weekly is null (hours not confirmed)")
        if self.continuous_operation_limit_minutes is None:
            reasons.append("continuousOperationLimitMinutes is null (no approved maximum run)")
        if not self.auto_start_enabled:
            reasons.append("businessHours.autoStartEnabled is false")
        if self.sauna_host is None and not self.simulate:
            reasons.append("saunaHost is null (device identity not confirmed)")
        return reasons

    def manual_heating_blockers(self) -> list[str]:
        reasons: list[str] = []
        if not self.commissioning.writes_enabled:
            reasons.append("commissioning.writesEnabled is false")
        if self.continuous_operation_limit_minutes is None:
            reasons.append("continuousOperationLimitMinutes is null (no approved maximum run)")
        if self.sauna_host is None and not self.simulate:
            reasons.append("saunaHost is null (device identity not confirmed)")
        return reasons

    def public_dict(self) -> dict:
        """Non-secret view for the dashboard."""
        return {
            "installationName": self.installation_name,
            "timezone": self.timezone,
            "targetTempC": self.target_temp_c,
            "readyTempC": self.ready_temp_c,
            "hardwareTimerMaxMinutes": self.hardware_timer_max_minutes,
            "renewEveryMinutes": self.renew_every_minutes,
            "deliveryReserveSeconds": self.delivery_reserve_seconds,
            "autoStartEnabled": self.auto_start_enabled,
            "requireDailyArming": self.require_daily_arming,
            "continuousOperationLimitMinutes": self.continuous_operation_limit_minutes,
            "cloudFallbackEnabled": self.cloud_fallback.enabled,
            "remoteAccessEnabled": self.remote_access.enabled,
            "writesEnabled": self.commissioning.writes_enabled,
            "simulate": self.simulate,
            "businessHours": self.business_hours.to_dict(),
            "scheduledHeatingBlockers": self.scheduled_heating_blockers(),
            "manualHeatingBlockers": self.manual_heating_blockers(),
        }


_TOP_LEVEL_KEYS = {
    "schemaVersion", "installationName", "saunaHost", "saunaPort", "webBind", "webPort",
    "timezone", "targetTempC", "readyTempC", "hardwareTimerMaxMinutes", "renewEveryMinutes",
    "deliveryReserveSeconds", "businessHours", "continuousOperationLimitMinutes",
    "cloudFallback", "remoteAccess", "commissioning", "security", "simulate", "privateDir",
}


def _int_in(data: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = data.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{key} must be an integer")
    if not lo <= v <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return v


def _bool(data: dict, key: str, default: bool = False) -> bool:
    v = data.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{key} must be true or false")
    return v


def load_config_dict(data: dict, source_path: Path | None = None) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise ConfigError(f"schemaVersion must be {SCHEMA_VERSION}")

    sauna_host = data.get("saunaHost")
    if sauna_host is not None and (not isinstance(sauna_host, str) or not sauna_host.strip()):
        raise ConfigError("saunaHost must be null or a non-empty string")

    web_bind = data.get("webBind", "127.0.0.1")
    if web_bind not in ("127.0.0.1", "::1"):
        raise ConfigError("webBind must be loopback (127.0.0.1 or ::1); never expose the backend directly")

    hours_raw = data.get("businessHours") or {}
    if not isinstance(hours_raw, dict):
        raise ConfigError("businessHours must be an object")
    unknown_h = set(hours_raw) - {"weekly", "dateOverrides", "preheatLeadMinutes", "shutdownAfterCloseMinutes",
                                  "autoStartEnabled", "requireDailyArming", "revision", "timezone"}
    if unknown_h:
        raise ConfigError(f"unknown businessHours keys: {sorted(unknown_h)}")
    timezone = str(data.get("timezone", "Australia/Brisbane"))
    try:
        policy = BusinessHoursPolicy.from_dict(
            {**hours_raw, "timezone": hours_raw.get("timezone", timezone)},
            revision=str(hours_raw.get("revision", "unset")),
        )
    except HoursError as exc:
        raise ConfigError(f"businessHours: {exc}") from exc

    limit = data.get("continuousOperationLimitMinutes")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 30 <= limit <= 24 * 60:
            raise ConfigError("continuousOperationLimitMinutes must be null or an integer 30-1440")

    cf = data.get("cloudFallback") or {}
    ra = data.get("remoteAccess") or {}
    cm = data.get("commissioning") or {}
    sec = data.get("security") or {}
    web_port = _int_in(data, "webPort", 8787, 1024, 65535)
    allowed_hosts = tuple(sec.get("allowedHosts", (f"127.0.0.1:{web_port}", f"localhost:{web_port}")))
    for h in allowed_hosts:
        if not isinstance(h, str) or not h:
            raise ConfigError("security.allowedHosts entries must be non-empty strings")

    target = _int_in(data, "targetTempC", 80, 20, 80)
    ready = _int_in(data, "readyTempC", 55, 20, 80)
    if ready > target:
        raise ConfigError("readyTempC cannot exceed targetTempC")

    return AppConfig(
        installation_name=str(data.get("installationName", "Recovery Room")),
        sauna_host=sauna_host,
        sauna_port=_int_in(data, "saunaPort", 12416, 1, 65535),
        web_bind=web_bind,
        web_port=web_port,
        timezone=timezone,
        target_temp_c=target,
        ready_temp_c=ready,
        hardware_timer_max_minutes=_int_in(data, "hardwareTimerMaxMinutes", 60, 1, 60),
        renew_every_minutes=_int_in(data, "renewEveryMinutes", 30, 5, 59),
        delivery_reserve_seconds=_int_in(data, "deliveryReserveSeconds", 120, 30, 600),
        business_hours=policy,
        auto_start_enabled=_bool(hours_raw, "autoStartEnabled", False),
        require_daily_arming=_bool(hours_raw, "requireDailyArming", True),
        continuous_operation_limit_minutes=limit,
        cloud_fallback=CloudFallbackConfig(enabled=_bool(cf, "enabled", False)),
        remote_access=RemoteAccessConfig(enabled=_bool(ra, "enabled", False), public_hostname=ra.get("publicHostname")),
        commissioning=CommissioningConfig(
            writes_enabled=_bool(cm, "writesEnabled", False),
            inspection_procedure_approved=_bool(cm, "inspectionProcedureApproved", False),
            manufacturer_remote_start_approved=_bool(cm, "manufacturerRemoteStartApproved", False),
            approved_by=cm.get("approvedBy"),
            approved_on=cm.get("approvedOn"),
        ),
        security=SecurityConfig(
            allowed_hosts=allowed_hosts,
            session_lifetime_days=_int_in(sec, "sessionLifetimeDays", 30, 1, 365),
            pairing_pin_lifetime_minutes=_int_in(sec, "pairingPinLifetimeMinutes", 10, 1, 60),
            pairing_max_attempts=_int_in(sec, "pairingMaxAttempts", 5, 1, 20),
        ),
        simulate=_bool(data, "simulate", False),
        private_dir=Path(data.get("privateDir", "private")),
        source_path=source_path,
    )


def load_config(path: str | os.PathLike) -> AppConfig:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}")
    cfg = load_config_dict(data, source_path=p)
    if not cfg.private_dir.is_absolute():
        cfg = AppConfig(**{**cfg.__dict__, "private_dir": (p.parent / cfg.private_dir).resolve()})
    return cfg
