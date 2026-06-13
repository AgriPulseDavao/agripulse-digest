#!/usr/bin/env python3
"""
AgriPulse Weekly Farm Health Score digest - board-friendly edition.
Reads sensor telemetry from Supabase, computes a farm health score, renders a
plain-language PDF for coop leaders/board, and emails it via Resend.

Env: SUPABASE_URL, SUPABASE_KEY, RESEND_API_KEY, MAIL_FROM, MAIL_TO
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
TZ_OFFSET_H = 8

WEIGHTS = {"ph": 18, "ec": 18, "moisture": 14, "soil_temp": 12,
           "panama": 13, "sigatoka": 13, "sensors": 12}
LABELS = {"ph": "Soil pH (acidity)", "ec": "Soil nutrients (EC)", "moisture": "Soil moisture",
          "soil_temp": "Soil temperature", "panama": "Panama disease risk",
          "sigatoka": "Sigatoka disease risk", "sensors": "Sensors & battery"}
TARGETS = {"ph": "5.0-7.5", "ec": "200-800 uS/cm", "moisture": "30-85%",
           "soil_temp": "20-32 C", "panama": "Risk stays LOW", "sigatoka": "Risk stays LOW",
           "sensors": "All online, battery OK"}
EXPLAIN = {
    "ph": "How acidic the soil is. Bananas feed best when pH stays between 5.0 and 7.5.",
    "ec": "The soil's nutrient level. Too low means the crop is underfed; too high means salty soil.",
    "moisture": "How wet the soil is. Too dry stresses the plants; too wet invites root disease.",
    "soil_temp": "Temperature around the roots.",
    "panama": "Risk of Panama disease (Fusarium wilt) - a fatal, soil-borne banana disease.",
    "sigatoka": "Risk of Black Sigatoka - a leaf fungus that cuts bunch yield.",
    "sensors": "Whether the monitoring devices are online and have battery left.",
}
BATT_MIN = 3.3
DISPLAY = [("ph", "Soil pH", ""), ("ec", "Soil nutrients (EC)", " uS/cm"), ("moisture", "Soil moisture", "%"),
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


def status_pill(score):
    if score is None: return ("No reading", "#8a948c", "#eef1ee")
    if score >= 80: return ("Good", "#2e9e54", "#e7f5ec")
    if score >= 50: return ("Watch", "#b9770c", "#fdf3e0")
    return ("Needs action", "#c0392b", "#fbecea")


def plain_state(k, d):
    latest, status = d.get("latest"), (d.get("status") or "")
    n = to_num(latest)
    if d["score"] is None:
        return "No reading - sensor offline."
    if k == "ph":
        if n is not None and n < 5.0: return f"pH {latest} - too acidic for best growth."
        if n is not None and n > 7.5: return f"pH {latest} - too alkaline."
        return f"pH {latest} - in the ideal range."
    if k == "ec":
        if "low" in status.lower(): return f"{latest} uS/cm - nutrients running low."
        if "high" in status.lower(): return f"{latest} uS/cm - soil too salty."
        return f"{latest} uS/cm - healthy nutrient level."
    if k == "moisture":
        if n is not None and n < 30: return f"{latest}% - soil is dry."
        if n is not None and n > 85: return f"{latest}% - soil is waterlogged."
        return f"{latest}% - moisture is healthy."
    if k == "soil_temp":
        return f"{latest} C around the roots."
    if k == "panama":
        return "Currently LOW - no Panama warning." if str(latest).upper() == "LOW" else f"{latest} - Panama warning active."
    if k == "sigatoka":
        return "Currently LOW - leaves safe." if str(latest).upper() == "LOW" else f"{latest} - spray window open."
    if k == "sensors":
        b = d.get("battery")
        return f"{d.get('n',0)} of {d.get('of',3)} sensors online" + (f", battery {b}V." if b is not None else ".")
    return status


def recommend(k, d):
    latest, status = d.get("latest"), (d.get("status") or "")
    n = to_num(latest)
    if k == "ph":
        if n is not None and n < 5.0:
            return ("Lime the soil to fix acidity",
                    "Soil is too acidic, which locks up nutrients and stunts banana growth.",
                    "Apply agricultural lime and re-test in 1-2 weeks.")
        if n is not None and n > 7.5:
            return ("Lower soil pH",
                    "Soil is too alkaline, which limits nutrient uptake.",
                    "Apply elemental sulfur or organic matter (compost).")
        return ("Keep an eye on soil pH",
                "Acidity drifted out of the ideal band part of this period.",
                "Recheck after the next readings; treat if it stays out of 5.0-7.5.")
    if k == "ec":
        if "high" in status.lower():
            return ("Flush salty soil",
                    "Nutrient/salt level is too high, which can burn roots.",
                    "Irrigate well to flush salts and pause fertilizer for now.")
        return ("Feed the soil",
                "Nutrient level is below the healthy range, so the crop is underfed.",
                "Apply a balanced fertilizer and re-check the reading after.")
    if k == "moisture":
        if n is not None and n > 85:
            return ("Reduce watering / improve drainage",
                    "Soil is waterlogged, which stresses roots and raises Panama disease risk.",
                    "Ease off irrigation and clear drainage canals.")
        return ("Increase irrigation",
                "Soil has been drier than ideal, which stresses the plants.",
                "Add irrigation until moisture is back in the 30-85% range.")
    if k == "soil_temp":
        return ("Watch root-zone temperature",
                "Soil temperature has been outside the comfortable 20-32 C range.",
                "Mulch to buffer temperature; usually self-corrects with weather.")
    if k == "panama":
        return ("Act on Panama disease risk",
                "Conditions favoured Panama disease (a fatal, soil-borne wilt) part of this period.",
                "Inspect plants for yellowing/wilting, improve drainage, and do not move soil between blocks.")
    if k == "sigatoka":
        cur_low = str(latest).upper() == "LOW"
        if cur_low:
            return ("Stay ready for Sigatoka",
                    "Leaf-disease risk spiked earlier this period; it is safe right now but conditions can return.",
                    "Keep monitoring; spray fungicide promptly if warm, humid, wet-leaf conditions come back.")
        return ("Spray for Sigatoka now",
                "Warm, humid conditions with wet leaves favour Black Sigatoka, which cuts yield.",
                "Apply fungicide and improve airflow / drainage between rows.")
    return (LABELS[k], status or "Below target.", "Review the readings.")


def build_actions(f):
    actions = []
    soil_missing = [LABELS[k] for k in ("ec", "moisture", "soil_temp", "panama") if f[k]["score"] is None]
    if soil_missing:
        actions.append(dict(
            title="Bring the soil sensor back online",
            why="The soil sensor is offline, so there are no readings for soil nutrients, moisture, temperature, or Panama-disease risk - the farm's most important early warnings.",
            do="Replace the sensor's batteries (Dragino ER14505, 3.6V) and confirm it appears again on the dashboard.",
            impact=None, urgent=True))
    gaps = []
    for k, d in f.items():
        if k == "sensors" or d["score"] is None or d["score"] >= 80:
            continue
        pts = round(d["weight"] * (100 - d["score"]) / 100, 1)
        t, why, do = recommend(k, d)
        gaps.append((pts, t, why, do))
    gaps.sort(reverse=True)
    for pts, t, why, do in gaps:
        actions.append(dict(title=t, why=why, do=do, impact=pts, urgent=False))
    return actions


def render_html(composite, f, series, last_seen, trend, generated):
    g = grade(composite)
    score_txt = composite if composite is not None else "&mdash;"
    good = [LABELS[k] for k, d in f.items() if d["score"] is not None and d["score"] >= 80 and k != "sensors"]
    soil_off = f["ec"]["score"] is None
    bl = f"This week the farm scored <b>{score_txt}/100 ({g[0]})</b>. "
    if good:
        bl += "Doing well: " + ", ".join(good[:3]).lower() + ". "
    if soil_off:
        bl += "But the <b>soil sensor is offline</b>, so we have no soil nutrient, moisture, or Panama-risk data right now - getting it back online is the top priority. "
    else:
        bl += "See the recommended actions below to raise the score. "
    actions = build_actions(f)
    act_html = ""
    for i, a in enumerate(actions):
        tag = "<span class='urg'>DO FIRST</span>" if a.get("urgent") else (f"<span class='gain'>+{a['impact']} pts</span>" if a.get("impact") else "")
        act_html += (f"<div class='act'><div class='acttop'><span class='anum'>{i+1}</span>"
                     f"<span class='atitle'>{a['title']}</span>{tag}</div>"
                     f"<div class='awhy'><b>Why:</b> {a['why']}</div>"
                     f"<div class='ado'><b>Do this:</b> {a['do']}</div></div>")
    if not act_html:
        act_html = "<div class='act'><div class='atitle'>No action needed - everything is on target. Keep monitoring.</div></div>"
    glance = ""
    order = ["ph", "ec", "moisture", "soil_temp", "panama", "sigatoka", "sensors"]
    for k in order:
        d = f[k]; lab, col, bg = status_pill(d["score"])
        glance += (f"<tr><td><b>{LABELS[k]}</b><div class='exp'>{EXPLAIN[k]}</div></td>"
                   f"<td>{plain_state(k, d)}</td>"
                   f"<td><span class='pill' style='color:{col};background:{bg}'>{lab}</span></td></tr>")
    well = [f"<li>{LABELS[k]} - {plain_state(k, f[k]).lower()}</li>" for k, d in f.items()
            if d["score"] is not None and d["score"] >= 80]
    well_html = ("<ul class='well'>" + "".join(well) + "</ul>") if well else "<p class='muted'>Full picture returns once the soil sensor is back online.</p>"
    vals = [v for _, v in trend if v is not None] + ([composite] if composite else [])
    mx = max(vals + [50]) if vals else 50
    tbars = ""
    for label, v in trend + [("Now", composite)]:
        if v is None:
            tbars += f"<td class='tb'><div class='tsc'>&mdash;</div><div class='bcol gap' style='height:5px'></div><div class='tl'>{label}</div></td>"
        else:
            h = int(110 * v / mx); cls = "bcol now" if label == "Now" else "bcol"
            tbars += f"<td class='tb'><div class='tsc'>{v}</div><div class='{cls}' style='height:{h}px'></div><div class='tl'>{label}</div></td>"
    return f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
    @page {{ size: A4; margin: 15mm 14mm; }}
    body{{font-family:'Helvetica','Arial',sans-serif;color:#1b2620;font-size:12px;line-height:1.55}}
    .head{{border-bottom:3px solid #1f7a3d;padding-bottom:9px;margin-bottom:12px}}
    .head h1{{margin:0;color:#15592c;font-size:21px}} .head .sub{{color:#637067;font-size:11px}}
    .muted{{color:#8a948c}}
    .top{{display:flex;gap:14px;margin-bottom:6px}}
    .scorebox{{flex:0 0 150px;text-align:center;border:1px solid #e3e9e2;border-radius:10px;padding:12px}}
    .big{{font-size:46px;font-weight:800;color:{g[1]};line-height:1}}
    .grade{{display:inline-block;color:#fff;background:{g[1]};padding:3px 12px;border-radius:20px;font-weight:800;font-size:11px;margin-top:6px}}
    .bottom{{flex:1;background:#f2f8f3;border:1px solid #dcebe0;border-radius:10px;padding:12px 14px;font-size:13px}}
    .bottom h3{{margin:0 0 5px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#15592c}}
    h2{{color:#15592c;font-size:13px;text-transform:uppercase;letter-spacing:.04em;margin:16px 0 8px;border-bottom:1px solid #e3e9e2;padding-bottom:4px}}
    table{{width:100%;border-collapse:collapse}} td,th{{padding:7px 8px;border-bottom:1px solid #eef1ee;text-align:left;vertical-align:top}}
    th{{font-size:10px;text-transform:uppercase;color:#637067}} .pts{{text-align:right;font-weight:800}}
    .exp{{color:#8a948c;font-size:10.5px;margin-top:2px}}
    .pill{{display:inline-block;padding:2px 9px;border-radius:20px;font-weight:800;font-size:10.5px;white-space:nowrap}}
    .act{{border:1px solid #e3e9e2;border-left:4px solid #1f7a3d;border-radius:8px;padding:9px 12px;margin-bottom:8px}}
    .acttop{{display:flex;align-items:center;gap:8px;margin-bottom:3px}}
    .anum{{flex:0 0 20px;height:20px;border-radius:50%;background:#1f7a3d;color:#fff;text-align:center;font-weight:800;font-size:11px;line-height:20px}}
    .atitle{{font-weight:800;font-size:13px;flex:1}}
    .gain{{color:#15592c;background:#e7f5ec;border-radius:20px;padding:2px 9px;font-size:10.5px;font-weight:800}}
    .urg{{color:#fff;background:#c0392b;border-radius:20px;padding:2px 9px;font-size:10.5px;font-weight:800}}
    .awhy,.ado{{font-size:12px;margin-top:2px}} .awhy{{color:#4a5650}}
    ul.well{{margin:4px 0;padding-left:18px}} ul.well li{{margin-bottom:2px;font-size:12px}}
    .trend{{width:100%}} .tb{{text-align:center;vertical-align:bottom;border:none}}
    .bcol{{width:58%;margin:0 auto;background:#3aa55e;border-radius:4px 4px 0 0}}
    .bcol.now{{background:#15592c}} .bcol.gap{{background:#dbe2db}}
    .tsc{{font-size:11px;font-weight:700}} .tl{{font-size:10px;color:#637067;margin-top:3px}}
    .note{{background:#fff8e6;border-left:3px solid #c9851b;padding:8px 11px;font-size:10.5px;color:#5b4a16;margin-top:8px}}
    </style></head><body>
    <div class='head'><h1>&#127820; AgriPulse Farm Health Report</h1>
      <div class='sub'>Davao, Philippines &middot; Weekly report for coop leaders &middot; {generated}</div></div>
    <div class='top'>
      <div class='scorebox'><div class='big'>{score_txt}</div><div class='muted' style='font-size:11px'>out of 100</div><div class='grade'>{g[0]}</div></div>
      <div class='bottom'><h3>The bottom line</h3>{bl}</div>
    </div>
    <h2>Recommended actions this week</h2>{act_html}
    <h2>Farm at a glance</h2>
    <table><tr><th>What we measure</th><th>Right now</th><th>Status</th></tr>{glance}</table>
    <h2>What's going well</h2>{well_html}
    <h2>Score trend (last 5 weeks)</h2>
    <table class='trend'><tr>{tbars}</tr></table>
    <div class='note'>How to read this: the score is a weighted average of soil and crop-health checks
      (pH 18%, nutrients 18%, moisture 14%, soil temp 12%, Panama 13%, Sigatoka 13%, sensors 12%).
      A higher score means healthier growing conditions. Checks with no recent reading are skipped.
      Soil readings (nutrients, moisture, temperature, Panama risk) need the soil sensor online.
      NPK and weather are not yet included. Auto-generated from live sensor data.</div>
    </body></html>"""


def summary_line(composite, f):
    g = grade(composite)[0]
    weak = sorted([(d["weight"] * (100 - d["score"]) / 100, LABELS[k])
                   for k, d in f.items() if d["score"] is not None and d["score"] < 80], reverse=True)
    if f["ec"]["score"] is None:
        return f"Farm health is {g} ({composite}/100). Top priority: bring the soil sensor back online."
    top = ", ".join(n for _, n in weak[:2]) if weak else "no major gaps"
    return f"Farm health is {g} ({composite if composite is not None else 'n/a'}/100). Focus: {top}."


def send_email(pdf_bytes, composite, summary, generated):
    score = composite if composite is not None else "n/a"
    html = (f"<p>Hi team,</p><p>Here is this week's AgriPulse Farm Health Report (PDF attached) - "
            f"a plain-language snapshot of the farm with recommended actions.</p>"
            f"<p><b>Score: {score}/100.</b> {summary}</p>"
            f"<p style='color:#637067;font-size:12px'>Generated {generated}. Sent automatically every Friday.</p>")
    payload = {"from": MAIL_FROM, "to": MAIL_TO,
               "subject": f"AgriPulse Farm Health Report - {score}/100 ({generated})",
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
