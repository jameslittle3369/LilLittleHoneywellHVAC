# honeywell_hvac.py

Polls every thermostat zone on a Honeywell Total Connect Comfort account
(via [`pyhtcc`](https://github.com/csm10495/pyhtcc)) and reports the full
raw status: system mode, fan mode/status, indoor/outdoor temperature and
humidity, setpoints, hold/schedule state, and every other field
`mytotalconnectcomfort.com` exposes per zone.

By default it prints a human-readable table to stdout. With `--sendemail`
it mails an HTML/text summary. `--json`/`--push-api` carry the **full**
raw field set (not just what the table shows) -- there's no charting
here on purpose; that's Grafana's job against the data `--push-api` logs.

---

## Requirements

- Python 3.11+ (this fleet's venvs are 3.14)
- A Honeywell Total Connect Comfort account with at least one zone

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
```

Then edit `.env`. Every key is documented inline. The minimum for stdout
reporting is `HONEYWELL_USERNAME`/`HONEYWELL_PASSWORD`; the minimum for
`--sendemail` adds `SMTP_USERNAME`, `SMTP_PASSWORD`, and `EMAIL_TO`. No
pairing or 2FA step is needed -- plain username/password login, same as
the portal's own web login.

**Rate limits**: `mytotalconnectcomfort.com` throttles frequent logins,
and this script logs in fresh every run. Don't schedule it more often
than every 15 minutes.

## Usage

```bash
.venv/bin/python honeywell_hvac.py [options]
```

| Flag | Effect |
| --- | --- |
| *(none)* | Print a table (one row per zone) |
| `--json` | Print every raw field as JSON instead of a table |
| `--sendemail` | Email an HTML/text summary via SMTP |
| `--push-api` | POST every raw field per zone to `API_BASE_URL` and exit -- no table/JSON/email output. This is what the scheduled/automated run uses. |
| `--env-file PATH` | Use a `.env` file other than `./.env` |

## What gets logged

`--push-api` posts to `POST {API_BASE_URL}/hvac-zones/{device_id}/log`
with the fields listed in `UI_FIELD_MAP`/`FAN_FIELD_MAP` in
`honeywell_hvac.py` (15 fields, pruned from the original ~53 raw
`uiData`/`fanData` fields once real logged data showed the rest never
or rarely changed). The backend get-or-creates the zone and only
inserts a new log row if at least one field changed since the last poll
(dedup), so an idle thermostat doesn't spam the table.

## Scheduling

The scheduled/automated run should only ever use `--push-api` -- run
manually, or set up your own cron job, for `--sendemail`/`--json`.
