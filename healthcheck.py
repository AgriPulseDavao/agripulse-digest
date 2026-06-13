#!/usr/bin/env python3
"""
AgriPulse data health-check.
Checks the newest reading in Supabase. If no data has arrived for longer than
STALE_HOURS, emails an alert (via Resend) so you know the ThingsBoard -> Supabase
capture has stopped. Stays silent when data is flowing normally.

Env: SUPABASE_URL, SUPABASE_KEY, RESEND_API_KEY, MAIL_FROM, MAIL_TO, STALE_HOURS
"""
import os, sys, json, datetime as dt
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
MAIL_FROM = os.environ.get("MAIL_FROM") or "AgriPulse <onboarding@resend.dev>"
MAIL_TO = [e.strip() for e in os.environ["MAIL_TO"].split(",") if e.strip()]
STALE_HOURS = float(os.environ.get("STALE_HOURS", "8"))
TZ_OFFSET_H = 8


def newest():
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    url = f"{SUPABASE_URL}/rest/v1/telemetry?select=reading_at,device_name&order=reading_at.desc&limit=1"
    r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def alert(hours, last_desc):
    when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET_H)).strftime("%b %d, %Y %I:%M %p PHT")
    hrs = f"{hours:.0f}" if hours is not None else "many"
    body = (f"<p><b>Heads up - the farm sensors may have gone quiet.</b></p>"
            f"<p>No new readings have reached the AgriPulse database in about <b>{hrs} hours</b>.</p>"
            f"<p>Last reading on record: {last_desc or 'none'}.</p>"
            f"<p>This usually means the ThingsBoard-to-database link dropped, or the sensors stopped "
            f"sending. Worth a quick look at the ThingsBoard dashboard and the backup rule node "
            f"(\"Push to Supabase\").</p>"
            f"<p style='color:#637067;font-size:12px'>Automated AgriPulse data health-check - {when}. "
            f"Alert threshold: {STALE_HOURS:.0f} hours.</p>")
    payload = {"from": MAIL_FROM, "to": MAIL_TO,
               "subject": f"AgriPulse ALERT: no sensor data for ~{hrs}h",
               "html": body}
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                      data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    print("Alert email sent.")


def main():
    row = newest()
    if not row:
        print("No rows in telemetry at all - alerting.")
        alert(None, None)
        return
    ts = dt.datetime.fromisoformat(row["reading_at"].replace("Z", "+00:00"))
    hours = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 3600
    print(f"Newest reading: {row['reading_at']} ({hours:.1f}h ago) from {row.get('device_name')}.")
    if hours > STALE_HOURS:
        print(f"STALE (> {STALE_HOURS}h) - alerting.")
        alert(hours, f"{row.get('device_name')} at {row['reading_at']}")
    else:
        print("Data is fresh - no alert needed.")


if __name__ == "__main__":
    sys.exit(main())
