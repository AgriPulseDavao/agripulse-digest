#!/usr/bin/env python3
"""
AgriPulse Weekly Farm Health Score digest.
Reads sensor telemetry from Supabase, computes a Microsoft-Secure-Score-style
farm health score, renders a PDF, and emails it via Resend.

Config from env vars:
  SUPABASE_URL, SUPABASE_KEY, RESEND_API_KEY, MAIL_FROM, MAIL_TO
"""
import os, sys, json, base64, datetime as dt
from collections import defaultdict
import requests
from weasyprint import HTML

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
MAIL_FROM = os.environ.get("MAIL_FROM") or "AgriPulse <onboarding@resend.dev>"
MAIL_TO = [e.strip() for e in os.environ["MAIL_TO"].split(",") if e.strip()]
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "30"))
TZ_OFFSET_H = 8  # Davao / Asia-Manila, UTC+8

WEIGHTS = {"ph": 18, "ec": 18, "moisture": 14, "soil_temp": 12,
           "panama": 13, "sigatoka": 13, "sensors": 12}
LABELS = {"ph": "Soil pH", "ec": "Soil EC (nutrients)", "moisture": "Soil moisture",
          "soil_temp": "Soil temperature", "panama": "Panama disease risk",
          "sigatoka": "Sigatoka disease risk", "sensors": "Sensor & battery health"}
TARGETS = {"ph": "5.0-7.5", "ec": "200-800 uS/cm", "moisture": "30-85%",
           "soil_temp": "20-32 C", "panama": "Risk = LOW", "sigatoka": "Risk = LOW",
           "sensors": "All reporting, battery OK"}
BATT_MIN = 3.3

DISPLAY = [("ph", "Soil pH", ""), ("ec", "Soil EC", " uS/cm"), ("moisture", "Soil moisture", "%"),
           ("soil_temp", "Soil temperature", " C"), ("leaf", "Leaf wetness", "%"),
           ("battery", "Sensor battery", " V"), ("irrigation", "Irrigation status", ""),
           ("valve", "Valve state", "")]


def fetch_rows():
    since = (dt.datetime.utcnow() - dt.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    rows, offset, page = [], 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/telemetry?select=device_name,reading_at,data"
               f"&reading_at=gte.{since}&order=reading_at.asc&limit={page}&offset={offset}")
        r = requests.get(url, headers=headers, timeout=60)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def collect(rows):
    series = defaultdict(list)
    latest_status, last_seen = {}, {}
    for row in rows:
        ts = row["reading_at"]
        data = row.get("data") or {}
        if isinstance(data, str):
            try: data = json.loads(data)
            except Exception: data = {}

        def put(metric, key):
            if data.get(key) is not None:
                series[metric].append((ts, data[key]))
                last_seen[metric] = ts
        put("ph", "ph"); put("ph", "ph_value")
        put("ec", "conduct_SOIL")
        put("moisture", "water_SOIL"); put("moisture", "soil_moisture")
        put("soil_temp", "temp_SOIL")
        put("battery", "BatV")
        put("leaf", "leaf")
        put("panama", "panama_risk")
        put("sigatoka", "sigatoka_risk")
        put("irrigation", "irrigation_status")
        put("valve", "valve_state")
        for metric, key in [("ph", "ph_status"), ("ec", "ec_status"), ("ec", "ec_guidance"),
                            ("panama", "panama_status"), ("sigatoka", "sigatoka_status")]:
            if data.get(key):
                latest_status[metric] = str(data[key])
    return series, latest_status, last_seen


def pct_in_range(vals, lo, hi):
    nums = [n for n in (to_num(v) for _, v in vals) if n is not None]
    if not nums: return None, 0
    return round(100 * sum(1 for n in nums if lo <= n <= hi) / len(nums)), len(nums)


def pct_equal(vals, target):
    items = [str(v).upper() for _, v in vals if v is not None]
    if not items: return None, 0
    return round(100 * sum(1 for v in items if v == target) / len(items)), len(items)


def hours_since(iso_ts):
    if not iso_ts: return None
    t = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600


def compute(series, latest_status, last_seen):
    f = {}
    s, n = pct_in_range(series["ph"], 5.0, 7.5);      f["ph"] = dict(score=s, n=n)
    s, n = pct_in_range(series["ec"], 200, 800);      f["ec"] = dict(score=s, n=n)
    s, n = pct_in_range(series["moisture"], 30, 85);  f["moisture"] = dict(score=s, n=n)
    s, n = pct_in_range(series["soil_temp"], 20, 32); f["soil_temp"] = dict(score=s, n=n)
    s, n = pct_equal(series["panama"], "LOW");        f["panama"] = dict(score=s, n=n)
    s, n = pct_equal(series["sigatoka"], "LOW");      f["sigatoka"] = dict(score=s, n=n)
    sensors = {"ph": series["ph"], "soil": series["ec"], "leaf": series["sigatoka"]}
    reporting = sum(1 for v in sensors.values() if v and (hours_since(v[-1][0]) or 1e9) <= 48)
    sens_score = round(100 * reporting / 3)
    batt_latest = to_num(series["battery"][-1][1]) if series.get("battery") else None
    if batt_latest is not None and batt_latest < BATT_MIN:
        sens_score = max(0, sens_score - 40)
    f["sensors"] = dict(score=sens_score, n=reporting, of=3, battery=batt_latest)
    tw = sw = 0
    for k, w in WEIGHTS.items():
        if f[k]["score"] is not None:
            tw += w; sw += w * f[k]["score"]
    composite = round(sw / tw) if tw else None
    for k in f:
        f[k]["latest"] = series[k][-1][1] if series.get(k) else None
        f[k]["status"] = latest_status.get(k)
        f[k]["age_h"] = hours_since(last_seen.get(k))
        f[k]["weight"] = WEIGHTS[k]
    return composite, f


def weekly_trend(series):
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for i in range(5, 0, -1):
        lo = now - dt.timedelta(days=i * 7); hi = now - dt.timedelta(days=(i - 1) * 7)
        def bucket(m):
            return [(t, v) for (t, v) in series.get(m, [])
                    if lo <= dt.datetime.fromisoformat(t.replace("Z", "+00:00")) < hi]
        parts = {"ph": pct_in_range(bucket("ph"), 5.0, 7.5)[0],
                 "ec": pct_in_range(bucket("ec"), 200, 800)[0],
                 "moisture": pct_in_range(bucket("moisture"), 30, 85)[0],
                 "soil_temp": pct_in_range(bucket("soil_temp"), 20, 32)[0],
                 "panama": pct_equal(bucket("panama"), "LOW")[0],
                 "sigatoka": pct_equal(bucket("sigatoka"), "LOW")[0]}
        tw = sw = 0
        for k, v in parts.items():
            if v is not None: tw += WEIGHTS[k]; sw += WEIGHTS[k] * v
        out.append((lo.strftime("%b %d"), round(sw / tw) if tw else None))
    return out


def grade(s):
    if s is None: return ("No data", "#637067")
    if s >= 90: return ("Excellent", "#2e9e54")
    if s >= 80: return ("Good", "#5aa700")
    if s >= 70: return ("Fair", "#c9851b")
    if s >= 60: return ("Needs attention", "#d9731a")
    return ("Critical", "#c0392b")


def bar_color(s):
    if s is None: return "#c9ced0"
    if s >= 80: return "#2e9e54"
    if s >= 50: return "#c9851b"
    return "#c0392b"


def render_html(composite, f, series, last_seen, trend, generated):
    g = grade(composite)
    acts = []
    for k, d in f.items():
        if d["score"] is None: continue
        gain = round(d["weight"] * (100 - d["score"]) / 100, 1)
        if gain > 0:
            acts.append((gain, LABELS[k], d.get("status") or f"Below target ({TARGETS[k]})."))
    acts.sort(reverse=True)
    act_rows = "".join(
        f"<tr><td><b>{i+1}. {name}</b><div class='sub'>{status}</div></td><td class='pts'>+{gain}</td></tr>"
        for i, (gain, name, status) in enumerate(acts)) or "<tr><td>All factors on target.</td><td class='pts'>+0</td></tr>"
    fac = ""
    for k, d in f.items():
        s = d["score"]; width = max(s, 2) if s is not None else 2
        sval = f"{s}/100" if s is not None else "no data"
        latest = f" &middot; latest: {d['latest']}" if d.get("latest") is not None else ""
        stale = f" <span class='stale'>&middot; {round(d['age_h']/24)}d old</span>" if d.get("age_h") and d["age_h"] > 72 else ""
        fac += (f"<div class='factor'><div class='ftop'><span class='fn'>{LABELS[k]}</span>"
                f"<span class='fv'>{sval}{latest}{stale}</span></div>"
                f"<div class='bar'><span style='width:{width}%;background:{bar_color(s)}'></span></div>"
                f"<div class='fs'>{d.get('status') or ''}</div></div>")
    read_rows = ""
    for key, label, unit in DISPLAY:
        latest = series[key][-1][1] if series.get(key) else None
        age = hours_since(last_seen.get(key))
        val = f"{latest}{unit}" if latest is not None else "<span class='muted'>no data yet</span>"
        ageTxt = ""
        if age is not None:
            ageTxt = f"{round(age)}h ago" if age < 72 else f"{round(age/24)}d ago"
        read_rows += f"<tr><td>{label}</td><td><b>{val}</b></td><td class='muted'>{ageTxt}</td></tr>"
    vals = [v for _, v in trend if v is not None] + ([composite] if composite else [])
    mx = max(vals + [50]) if vals else 50
    tbars = ""
    for label, v in trend + [("Now", composite)]:
        if v is None:
            tbars += f"<td class='tb'><div class='tsc'>&mdash;</div><div class='bcol gap' style='height:5px'></div><div class='tl'>{label}</div></td>"
        else:
            h = int(120 * v / mx); cls = "bcol now" if label == "Now" else "bcol"
            tbars += f"<td class='tb'><div class='tsc'>{v}</div><div class='{cls}' style='height:{h}px'></div><div class='tl'>{label}</div></td>"
    method = "".join(f"<tr><td>{LABELS[k]}</td><td>{TARGETS[k]}</td><td class='pts'>{w}%</td></tr>" for k, w in WEIGHTS.items())
    score_txt = composite if composite is not None else "&mdash;"
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page {{ size: A4; margin: 16mm 14mm; }}
    body{{font-family:'Helvetica','Arial',sans-serif;color:#19241d;font-size:12px;line-height:1.5}}
    .head{{border-bottom:3px solid #1f7a3d;padding-bottom:10px;margin-bottom:14px}}
    .head h1{{margin:0;color:#15592c;font-size:20px}} .sub{{color:#637067;font-size:11px}} .muted{{color:#8a948c}}
    .scorebox{{text-align:center;border:1px solid #e3e9e2;border-radius:10px;padding:12px;margin-bottom:12px}}
    .big{{font-size:50px;font-weight:800;color:{g[1]}}} .grade{{display:inline-block;color:#fff;background:{g[1]};padding:3px 14px;border-radius:20px;font-weight:800;font-size:12px}}
    h2{{color:#15592c;font-size:13px;text-transform:uppercase;letter-spacing:.04em;margin:16px 0 8px;border-bottom:1px solid #e3e9e2;padding-bottom:4px}}
    table{{width:100%;border-collapse:collapse}} td,th{{padding:6px 8px;border-bottom:1px solid #eef1ee;text-align:left;vertical-align:top}}
    th{{font-size:10px;text-transform:uppercase;color:#637067}} .pts{{text-align:right;font-weight:800;white-space:nowrap}}
    .sub{{color:#637067;font-size:11px;margin-top:2px}}
    .factor{{margin-bottom:10px}} .ftop{{display:flex;justify-content:space-between;font-size:12px}}
    .fn{{font-weight:700}} .fv{{color:#637067;font-size:11px}} .stale{{color:#c9851b;font-weight:700}}
    .bar{{height:11px;background:#edf1ec;border-radius:6px;overflow:hidden;margin:4px 0}}
    .bar span{{display:block;height:100%;border-radius:6px}} .fs{{color:#637067;font-size:11px}}
    .trend{{width:100%}} .tb{{text-align:center;vertical-align:bottom;border:none}}
    .bcol{{width:60%;margin:0 auto;background:#3aa55e;border-radius:4px 4px 0 0}}
    .bcol.now{{background:#15592c}} .bcol.gap{{background:#d6ddd6}}
    .tsc{{font-size:11px;font-weight:700}} .tl{{font-size:10px;color:#637067;margin-top:3px}}
    .note{{background:#fff8e6;border-left:3px solid #c9851b;padding:8px 11px;font-size:10.5px;color:#5b4a16;margin-top:8px}}
    </style></head><body>
    <div class='head'><h1>&#127820; AgriPulse Farm Health Score</h1>
      <div class='sub'>Davao, Philippines &middot; Weekly digest &middot; Generated {generated}</div></div>
    <div class='scorebox'><div class='big'>{score_txt}</div>
      <div>out of 100 &nbsp; <span class='grade'>{g[0]}</span></div></div>
    <h2>Priority actions &mdash; ranked by score impact</h2>
    <table><tr><th>Action</th><th class='pts'>Points to gain</th></tr>{act_rows}</table>
    <h2>Latest readings</h2>
    <table><tr><th>Sensor</th><th>Reading</th><th>When</th></tr>{read_rows}</table>
    <h2>Factor breakdown</h2>{fac}
    <h2>Score trend (last 5 weeks)</h2>
    <table class='trend'><tr>{tbars}</tr></table>
    <h2>How the score works</h2>
    <table><tr><th>Factor</th><th>Target</th><th class='pts'>Weight</th></tr>{method}</table>
    <div class='note'>Auto-generated from your Supabase sensor backup. Factors with no recent data are
      excluded from the score. NPK and weather are not yet included. Readings appear as your sensors report.</div>
    </body></html>"""


def summary_line(composite, f):
    g = grade(composite)[0]
    weak = sorted([(d["weight"] * (100 - d["score"]) / 100, LABELS[k])
                   for k, d in f.items() if d["score"] is not None and d["score"] < 80], reverse=True)
    top = ", ".join(n for _, n in weak[:2]) if weak else "no major gaps"
    return f"Farm health is {g} ({composite if composite is not None else 'n/a'}/100). Focus areas: {top}."


def send_email(pdf_bytes, composite, summary, generated):
    score = composite if composite is not None else "n/a"
    html = (f"<p>Your weekly AgriPulse Farm Health Score is attached as a PDF.</p>"
            f"<p><b>Score: {score}/100</b><br>{summary}</p>"
            f"<p style='color:#637067;font-size:12px'>Generated {generated}. Runs automatically every Friday.</p>")
    payload = {"from": MAIL_FROM, "to": MAIL_TO,
               "subject": f"AgriPulse Farm Health Score - {score}/100 ({generated})",
               "html": html,
               "attachments": [{"filename": f"AgriPulse_Farm_Health_{dt.date.today()}.pdf",
                                "content": base64.b64encode(pdf_bytes).decode()}]}
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                      data=json.dumps(payload), timeout=60)
    r.raise_for_status()
    print("Email sent:", r.json())


def main():
    generated = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET_H)).strftime("%B %d, %Y %I:%M %p PHT")
    rows = fetch_rows()
    print(f"Fetched {len(rows)} telemetry rows.")
    series, latest_status, last_seen = collect(rows)
    composite, f = compute(series, latest_status, last_seen)
    trend = weekly_trend(series)
    summary = summary_line(composite, f)
    html = render_html(composite, f, series, last_seen, trend, generated)
    pdf = HTML(string=html).write_pdf()
    print(f"PDF built ({len(pdf)} bytes). Composite={composite}.")
    send_email(pdf, composite, summary, generated)


if __name__ == "__main__":
    sys.exit(main())
