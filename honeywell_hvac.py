#!/usr/bin/env python3
"""honeywell_hvac.py - poll Honeywell Total Connect Comfort thermostats via pyhtcc.

Prints a table to stdout by default. With --sendemail it mails an HTML/text
summary. --push-api POSTs the fields in UI_FIELD_MAP/FAN_FIELD_MAP below
to sensors-backend-fastapi for Grafana time-series charting -- this is
what the scheduled/automated run uses.

Configuration comes from a .env file (see .env.example).
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from dotenv import load_dotenv
from pyhtcc import AuthenticationError, FanMode, PyHTCC, SystemMode, TooManyAttemptsError

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    requests = None  # only required for --push-api


# pyhtcc has no named enum for these three fields (SystemMode/FanMode
# above are its own) -- these are reverse-engineered from `somecomfort`
# (the older library Home Assistant's original Honeywell integration
# used), cross-validated against pyhtcc's own control-write methods
# (end_hold() sends 0, set_temp_*_setpoint() sends 1,
# set_permanent_*_setpoint() sends 2).
class HoldStatus(enum.IntEnum):
    """Values for status_heat / status_cool (same enum, both fields)."""

    Schedule = 0
    Temporary = 1
    Permanent = 2


class EquipmentStatus(enum.IntEnum):
    """Values for equipment_output_status.

    0 genuinely means "not heating/cooling," not "the whole system is
    off" -- fan_is_running (a separately logged field) is the companion
    signal that distinguishes truly idle from fan-only.
    """

    OffOrFan = 0
    Heat = 1
    Cool = 2

# Snake-cased field name -> raw Honeywell key, in the exact shape pyhtcc's
# CheckDataSession call returns (latestData.uiData / latestData.fanData).
# Shared verbatim with sensors-backend-fastapi's HvacZoneLogRequest schema
# field names. Originally logged every raw field pyhtcc exposes; pruned
# to these 15 after studying real data showed the rest never or rarely
# changed (capability/allowed/available/not_fault flags, hold/vacation
# bookkeeping, status/limit fields).
UI_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("disp_temperature", "DispTemperature"),
    ("heat_setpoint", "HeatSetpoint"),
    ("cool_setpoint", "CoolSetpoint"),
    ("display_units", "DisplayUnits"),
    ("status_heat", "StatusHeat"),
    ("status_cool", "StatusCool"),
    ("hold_until_capable", "HoldUntilCapable"),
    ("schedule_capable", "ScheduleCapable"),
    ("dual_setpoint_status", "DualSetpointStatus"),
    ("schedule_heat_sp", "ScheduleHeatSp"),
    ("schedule_cool_sp", "ScheduleCoolSp"),
    ("system_switch_position", "SystemSwitchPosition"),
    ("indoor_humidity", "IndoorHumidity"),
    ("outdoor_temperature", "OutdoorTemperature"),
    ("equipment_output_status", "EquipmentOutputStatus"),
)
FAN_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("fan_mode", "fanMode"),
    ("fan_is_running", "fanIsRunning"),
)


# --- Data model --------------------------------------------------------------


@dataclass
class Reading:
    device_id: str
    name: str
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def get(self, key: str) -> Any:
        return self.metrics.get(key)

    @property
    def mode_text(self) -> str:
        return self.get("system_switch_position") or "?"

    @property
    def fan_text(self) -> str:
        name = self.get("fan_mode") or "?"
        return f"{name} (running)" if self.get("fan_is_running") else name

    @property
    def temp_text(self) -> str:
        units = self.get("display_units") or "F"
        temp = self.get("disp_temperature")
        return f"{temp}\N{DEGREE SIGN}{units}" if temp is not None else "-"

    @property
    def humidity_text(self) -> str:
        humidity = self.get("indoor_humidity")
        return f"{humidity}%" if humidity is not None else "-"

    @property
    def setpoints_text(self) -> str:
        heat = self.get("heat_setpoint")
        cool = self.get("cool_setpoint")
        return f"heat {heat if heat is not None else '-'} / cool {cool if cool is not None else '-'}"


# --- Config ------------------------------------------------------------------


@dataclass
class Config:
    username: str
    password: str
    api_base_url: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: list[str]
    email_subject_prefix: str

    @classmethod
    def from_env(cls) -> "Config":
        email_to = [a.strip() for a in os.getenv("EMAIL_TO", "").split(",") if a.strip()]
        return cls(
            username=os.getenv("HONEYWELL_USERNAME", ""),
            password=os.getenv("HONEYWELL_PASSWORD", ""),
            api_base_url=os.getenv("API_BASE_URL", ""),
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", "").replace(" ", ""),
            email_from=os.getenv("EMAIL_FROM", ""),
            email_to=email_to,
            email_subject_prefix=os.getenv("EMAIL_SUBJECT_PREFIX", "[Honeywell HVAC]"),
        )


# --- Honeywell TCC -------------------------------------------------------


def _enum_name(enum_cls: type, value: Any) -> str | None:
    """Decode a raw pyhtcc IntEnum value to its .name, e.g. 3 -> "Cool".

    An unrecognized value (enum drift, or a device pyhtcc doesn't fully
    know) becomes "unknown(N)" rather than raising, so one odd zone
    doesn't kill the whole collection run.
    """
    if value is None:
        return None
    try:
        return enum_cls(value).name
    except ValueError:
        return f"unknown({value})"


def collect(cfg: Config) -> list[Reading]:
    if not cfg.username or not cfg.password:
        sys.exit("HONEYWELL_USERNAME and HONEYWELL_PASSWORD must be set (copy .env.example to .env).")

    pyhtcc = PyHTCC(cfg.username, cfg.password)
    readings: list[Reading] = []
    for zone in pyhtcc.get_all_zones():
        device_id = str(zone.device_id)
        try:
            latest = zone.zone_info["latestData"]
            ui = latest["uiData"]
            fan = latest["fanData"]
            metrics = {snake: ui.get(raw) for snake, raw in UI_FIELD_MAP}
            metrics.update({snake: fan.get(raw) for snake, raw in FAN_FIELD_MAP})
            # Stored/logged as the enum name, not the raw int -- see
            # sensors-backend-fastapi's matching schema/model change.
            metrics["system_switch_position"] = _enum_name(
                SystemMode, metrics["system_switch_position"]
            )
            metrics["fan_mode"] = _enum_name(FanMode, metrics["fan_mode"])
            metrics["status_heat"] = _enum_name(HoldStatus, metrics["status_heat"])
            metrics["status_cool"] = _enum_name(HoldStatus, metrics["status_cool"])
            metrics["equipment_output_status"] = _enum_name(
                EquipmentStatus, metrics["equipment_output_status"]
            )
            readings.append(Reading(device_id=device_id, name=zone.get_name(), metrics=metrics))
        except Exception as exc:  # noqa: BLE001 - one bad zone must not kill the run
            name = zone.zone_info.get("Name", device_id) if zone.zone_info else device_id
            readings.append(Reading(device_id=device_id, name=name, error=str(exc)))

    return readings


# --- stdout ------------------------------------------------------------------


def print_table(readings: list[Reading], generated: datetime) -> None:
    if not readings:
        print("No Honeywell TCC zones found on this account.")
        return

    headers = ("Zone", "Mode", "Temp", "Humidity", "Setpoints", "Fan")
    rows = [
        (r.name, "ERROR" if r.error else r.mode_text, r.temp_text, r.humidity_text, r.setpoints_text, r.fan_text)
        for r in readings
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    print(f"Honeywell HVAC report - {generated:%Y-%m-%d %H:%M:%S %Z}")
    print()
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(line(row))

    errored = [r.name for r in readings if r.error]
    if errored:
        print()
        print(f"Errored: {', '.join(errored)}")


# --- Email -------------------------------------------------------------------


def _html_report(readings: list[Reading], generated: datetime) -> str:
    rows = "".join(
        "<tr>"
        f'<td style="padding:6px 14px 6px 0;">{r.name}</td>'
        f'<td style="padding:6px 14px 6px 0;">{"ERROR: " + r.error if r.error else r.mode_text}</td>'
        f'<td style="padding:6px 14px 6px 0;">{r.temp_text}</td>'
        f'<td style="padding:6px 14px 6px 0;">{r.humidity_text}</td>'
        f'<td style="padding:6px 14px 6px 0;">{r.setpoints_text}</td>'
        f'<td style="padding:6px 0;">{r.fan_text}</td>'
        "</tr>"
        for r in readings
    )
    return f"""<html><body style="margin:0;padding:24px;background:#f9f9f7;
font-family:system-ui,-apple-system,'Segoe UI',sans-serif;">
<div style="max-width:820px;margin:0 auto;background:#fcfcfb;padding:24px;
border-radius:8px;border:1px solid rgba(11,11,11,0.10);">
<h1 style="margin:0 0 4px;font-size:18px;">Honeywell HVAC report</h1>
<p style="margin:0 0 20px;font-size:12px;color:#52514e;">
Generated {generated:%Y-%m-%d %H:%M:%S %Z}</p>
<table style="border-collapse:collapse;font-size:13px;width:100%;">
<thead><tr>
<th style="text-align:left;padding:0 14px 6px 0;border-bottom:1px solid #c3c2b7;">Zone</th>
<th style="text-align:left;padding:0 14px 6px 0;border-bottom:1px solid #c3c2b7;">Mode</th>
<th style="text-align:left;padding:0 14px 6px 0;border-bottom:1px solid #c3c2b7;">Temp</th>
<th style="text-align:left;padding:0 14px 6px 0;border-bottom:1px solid #c3c2b7;">Humidity</th>
<th style="text-align:left;padding:0 14px 6px 0;border-bottom:1px solid #c3c2b7;">Setpoints</th>
<th style="text-align:left;padding:0 0 6px;border-bottom:1px solid #c3c2b7;">Fan</th>
</tr></thead>
<tbody>{rows}</tbody></table>
<p style="margin:20px 0 0;font-size:11px;color:#898781;">
Full raw metrics (uiData/fanData) are pushed to the API on every scheduled run --
this email is just a human-readable summary.</p>
</div></body></html>"""


def _text_report(readings: list[Reading], generated: datetime) -> str:
    lines = [f"Honeywell HVAC report - {generated:%Y-%m-%d %H:%M:%S %Z}", ""]
    for r in readings:
        if r.error:
            lines.append(f"{r.name}: ERROR ({r.error})")
        else:
            lines.append(
                f"{r.name}: {r.mode_text}, temp {r.temp_text}, humidity {r.humidity_text}, "
                f"{r.setpoints_text}, fan {r.fan_text}"
            )
    return "\n".join(lines)


def send_email(cfg: Config, readings: list[Reading], generated: datetime) -> None:
    sender = cfg.email_from or cfg.smtp_username
    missing = [
        name
        for name, value in (
            ("SMTP_USERNAME", cfg.smtp_username),
            ("SMTP_PASSWORD", cfg.smtp_password),
            ("EMAIL_TO", cfg.email_to),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"--sendemail needs these .env settings: {', '.join(missing)}")

    errored = [r.name for r in readings if r.error]
    subject = f"{cfg.email_subject_prefix} report"
    if errored:
        subject = f"{subject} - {len(errored)} error(s)"

    msg = EmailMessage()
    msg["Subject"] = f"{subject} ({generated:%Y-%m-%d %H:%M})"
    msg["From"] = sender
    msg["To"] = ", ".join(cfg.email_to)
    msg.set_content(_text_report(readings, generated))
    msg.add_alternative(_html_report(readings, generated), subtype="html")

    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port) as smtp:
            smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(msg)

    print(f"Emailed report to {', '.join(cfg.email_to)}.")


# --- API push ------------------------------------------------------------


def push_readings_to_api(cfg: Config, readings: list[Reading]) -> int:
    """POST every raw metric for each zone to sensors-backend-fastapi.

    Used for the scheduled/automated run -- skips the table/JSON/email
    output entirely. Zones that errored during collection are skipped
    rather than pushing a partial/fabricated reading.
    """
    if requests is None:
        sys.exit("--push-api needs the 'requests' package.  Run: pip install -r requirements.txt")
    if not cfg.api_base_url:
        sys.exit("--push-api needs API_BASE_URL set in .env")

    base = cfg.api_base_url.rstrip("/")
    ok_readings = [r for r in readings if not r.error]
    pushed = 0
    for r in ok_readings:
        url = f"{base}/hvac-zones/{r.device_id}/log"
        body = {"name": r.name, **r.metrics}
        try:
            response = requests.post(url, json=body, timeout=10)
            response.raise_for_status()
            pushed += 1
        except requests.RequestException as exc:
            print(f"warning: failed to push {r.name!r} to API: {exc}", file=sys.stderr)

    print(f"Pushed {pushed}/{len(ok_readings)} reading(s) to {base}.")
    return 0 if pushed else 1


# --- Entry point -------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report Honeywell Total Connect Comfort thermostat status for every zone."
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument(
        "--sendemail", action="store_true", help="email an HTML/text summary via SMTP"
    )
    parser.add_argument(
        "--push-api",
        action="store_true",
        help="POST every raw metric for each zone to API_BASE_URL and exit -- "
        "no table/JSON/email output. This is what the scheduled/automated run uses.",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default .env)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    env_path = args.env_file
    if os.path.isfile(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()

    cfg = Config.from_env()

    try:
        readings = collect(cfg)
    except AuthenticationError as exc:
        print(f"Honeywell TCC authentication failed: {exc}", file=sys.stderr)
        return 1
    except TooManyAttemptsError as exc:
        print(f"Honeywell TCC is rate-limiting login attempts: {exc}", file=sys.stderr)
        return 1

    generated = datetime.now(timezone.utc)

    if args.push_api:
        return push_readings_to_api(cfg, readings)

    if args.json:
        print(
            json.dumps(
                [
                    {"device_id": r.device_id, "name": r.name, "error": r.error, **r.metrics}
                    for r in readings
                ],
                indent=2,
            )
        )
    else:
        print_table(readings, generated)

    if args.sendemail:
        send_email(cfg, readings, generated)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
