#!/usr/bin/env python3
"""
AgriPulse Weekly Farm Health Score digest - board-friendly edition.
v2 (2026-08-12): multi-probe aware - pools all soil probes (0x01-0x04) for the
area score, adds a per-probe breakdown table and probe-aware actions.
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

# Soil probes on the Area 1 bus. Probe 0x01 telemetry arrives both as raw keys
# (moisture/ph/ec/temperature) and legacy aliases (water_SOIL/...); 2-4 use suffixes.
PROBES = [
    ("0x01", {"moisture": ["water_SOIL", "soil_moisture", "moisture"],
              "ph": ["ph", "ph_value"], "ec": ["conduct_SOIL", "ec"],
              "soil_temp": ["temp_SOIL", "temperature"]}),
    ("0x02", {"moisture": ["moisture2"], "ph": ["ph2"], "ec": ["ec2"],
              "soil_temp": ["temperature2"]}),
    ("0x03", {"moisture": ["moisture3"], "ph": ["ph3"], "ec": ["ec3"],
              "soil_temp": ["temperature3"]}),
    ("0x04", {"moisture": ["moisture4"], "ph": ["ph4"], "ec": ["ec4"],
              "soil_temp": ["temperature4"]}),
]

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
    """Pooled area series + per-probe series."""
    series = defaultdict(list)
    probe_series = {pid: defaultdict(list) for pid, _ in PROBES}
    latest_status, last_seen = {}, {}
    for row in rows:
        ts = row["reading_at"]
        data = row.get("data") or {}
        if isinstance(data, str):
            try: data = json.loads(data)
            except Exception: data = {}

        for pid, keymap in PROBES:
            for metric, keys in keymap.items():
                for key in keys:
                    if data.get(key) is not None:
                        probe_series[pid][metric].append((ts, data[key]))
                        series[metric].append((ts, data[key]))
                        last_seen[metric] = ts
                        break

        def put(metric, key):
            if data.get(key) is not None:
                series[metric].append((ts, data[key]))
                last_seen[metric] = ts
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
    return series, probe_series, latest_status, last_seen

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

def probe_snapshot(probe_series):
    """Latest values + freshness + Panama conditions per probe (same thresholds
    as the ThingsBoard rule chain: wet>70, temp 25-30, pH<5.5, EC<200)."""
    snaps = []
    for pid, _ in PROBES:
        ps = probe_series[pid]
        latest = {m: (ps[m][-1][1] if ps.get(m) else None) for m in ("moisture", "ph", "ec", "soil_temp")}
        last_ts = max((ps[m][-1][0] for m in ps if ps[m]), default=None)
        age = hours_since(last_ts)
        online = age is not None and age <= 48
        conds = []
        m, t = to_num(latest["moisture"]), to_num(latest["soil_temp"])
        p, e = to_num(latest["ph"]), to_num(latest["ec"])
        if m is not None and 70 < m <= 100: conds.append(f"very wet soil ({m:g}%)")
        if t is not None and 25 <= t <= 30: conds.append(f"peak TR4 temp ({t:g} C)")
        if p is not None and p < 5.5: conds.append(f"acidic soil (pH {p:g})")
        if e is not None and 0 < e < 200: conds.append(f"low nutrients ({e:g} uS/cm)")
        wk = {m2: pct_in_range(ps.get(m2, []), *rng)[0] for m2, rng in
              [("moisture", (30, 85)), ("ph", (5.0, 7.5)), ("ec", (200, 800)), ("soil_temp", (20, 32))]}
        scores = [s for s in wk.values() if s is not None]
        snaps.append(dict(pid=pid, latest=latest, online=online, age_h=age,
                          panama_n=len(conds), panama_conds=conds, in_range=wk,
                          avg_in_range=round(sum(scores) / len(scores)) if scores else None))
    return snaps

def compute(series, probe_snaps, latest_status, last_seen):
    f = {}
    s, n = pct_in_range(series["ph"], 5.0, 7.5); f["ph"] = dict(score=s, n=n)
    s, n = pct_in_range(series["ec"], 200, 800); f["ec"] = dict(score=s, n=n)
    s, n = pct_in_range(series["moisture"], 30, 85); f["moisture"] = dict(score=s, n=n)
    s, n = pct_in_range(series["soil_temp"], 20, 32); f["soil_temp"] = dict(score=s, n=n)
    s, n = pct_equal(series["panama"], "LOW"); f["panama"] = dict(score=s, n=n)
    s, n = pct_equal(series["sigatoka"], "LOW"); f["sigatoka"] = dict(score=s, n=n)
    probes_online = sum(1 for sn in probe_snaps if sn["online"])
    leaf_online = 1 if (series.get("sigatoka") and (hours_since(series["sigatoka"][-1][0]) or 1e9) <= 48) else 0
    of = len(probe_snaps) + 1
    sens_score = round(100 * (probes_online + leaf_online) / of)
    batt_latest = to_num(series["battery"][-1][1]) if series.get("battery") else None
    if batt_latest is not None and batt_latest < BATT_MIN:
        sens_score = max(0, sens_score - 40)
    f["sensors"] = dict(score=sens_score, n=probes_online + leaf_online, of=of, battery=batt_latest,
                        probes_online=probes_online)
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

def worst_probe_for(probe_snaps, metric, prefer="low"):
    best_pid, best_v = None, None
    for sn in probe_snaps:
        v = to_num(sn["latest"].get(metric))
        if v is None: continue
        if best_v is None or (prefer == "low" and v < best_v) or (prefer == "high" and v > best_v):
            best_pid, best_v = sn["pid"], v
    return best_pid, best_v

def plain_state(k, d, probe_snaps):
    latest, status = d.get("latest"), (d.get("status") or "")
    n = to_num(latest)
    if d["score"] is None:
        return "No reading - sensor offline."
    if k == "ph":
        pid, v = worst_probe_for(probe_snaps, "ph", "low")
        tail = f" (lowest at probe {pid})" if pid else ""
        if n is not None and n < 5.0: return f"pH {latest} - too acidic for best growth{tail}."
        if n is not None and n > 7.5: return f"pH {latest} - too alkaline."
        return f"pH {latest} - in the ideal range{tail}."
    if k == "ec":
        pid, v = worst_probe_for(probe_snaps, "ec", "low")
        tail = f" (lowest at probe {pid})" if pid else ""
        if "low" in status.lower(): return f"{latest} uS/cm - nutrients running low{tail}."
        if "high" in status.lower(): return f"{latest} uS/cm - soil too salty."
        return f"{latest} uS/cm - healthy nutrient level{tail}."
    if k == "moisture":
        pid, v = worst_probe_for(probe_snaps, "moisture", "high")
        tail = f" (wettest at probe {pid})" if pid else ""
        if n is not None and n < 30: return f"{latest}% - soil is dry."
        if n is not None and n > 85: return f"{latest}% - soil is waterlogged{tail}."
        return f"{latest}% - moisture is healthy{tail}."
    if k == "soil_temp":
        return f"{latest} C around the roots."
    if k == "panama":
        return "Currently LOW - no Panama warning." if str(latest).upper() == "LOW" else f"{latest} - Panama warning active."
    if k == "sigatoka":
        return "Currently LOW - leaves safe." if str(latest).upper() == "LOW" else f"{latest} - spray window open."
    if k == "sensors":
        b = d.get("battery")
        return (f"{d.get('probes_online', 0)} of {len(PROBES)} soil probes reporting"
                + (f", battery {b}V." if b is not None else "."))
    return status

def recommend(k, d, probe_snaps):
    latest, status = d.get("latest"), (d.get("status") or "")
    n = to_num(latest)
    if k == "ph":
        pid, v = worst_probe_for(probe_snaps, "ph", "low")
        where = f" Start where probe {pid} sits (pH {v:g})." if pid and v is not None else ""
        if n is not None and n < 5.0:
            return ("Lime the soil to fix acidity",
                    "Soil is too acidic, which locks up nutrients and stunts banana growth.",
                    f"Apply agricultural lime and re-test in 1-2 weeks.{where}")
        if n is not None and n > 7.5:
            return ("Lower soil pH",
                    "Soil is too alkaline, which limits nutrient uptake.",
                    "Apply elemental sulfur or organic matter (compost).")
        return ("Keep an eye on soil pH",
                "Acidity drifted out of the ideal band part of this period.",
                f"Recheck after the next readings; treat if it stays out of 5.0-7.5.{where}")
    if k == "ec":
        pid, v = worst_probe_for(probe_snaps, "ec", "low")
        where = f" Probe {pid} reads lowest ({v:g} uS/cm)." if pid and v is not None else ""
        if "high" in status.lower():
            return ("Flush salty soil",
                    "Nutrient/salt level is too high, which can burn roots.",
                    "Irrigate well to flush salts and pause fertilizer for now.")
        return ("Feed the soil",
                "Nutrient level is below the healthy range, so the crop is underfed.",
                f"Apply a balanced fertilizer and re-check the reading after.{where}")
    if k == "moisture":
        pid, v = worst_probe_for(probe_snaps, "moisture", "high")
        where = f" Wettest spot: probe {pid} ({v:g}%)." if pid and v is not None else ""
        if n is not None and n > 85:
            return ("Reduce watering / improve drainage",
                    "Soil is waterlogged, which stresses roots and raises Panama disease risk.",
                    f"Ease off irrigation and clear drainage canals.{where}")
        return ("Increase irrigation",
                "Soil has been drier than ideal, which stresses the plants.",
                "Add irrigation until moisture is back in the 30-85% range.")
    if k == "soil_temp":
        return ("Watch root-zone temperature",
                "Soil temperature has been outside the comfortable 20-32 C range.",
                "Mulch to buffer temperature; usually self-corrects with weather.")
    if k == "panama":
        hot = sorted(probe_snaps, key=lambda sn: -sn["panama_n"])
        worst = hot[0] if hot and hot[0]["panama_n"] else None
        where = f" Highest risk at probe {worst['pid']} ({worst['panama_n']}/4 conditions)." if worst else ""
        return ("Act on Panama disease risk",
                "Conditions favoured Panama disease (a fatal, soil-borne wilt) part of this period.",
                f"Inspect plants for yellowing/wilting, improve drainage, and do not move soil between blocks.{where}")
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

def build_actions(f, probe_snaps):
    actions = []
    offline = [sn["pid"] for sn in probe_snaps if not sn["online"]]
    if len(offline) == len(probe_snaps):
        actions.append(dict(
            title="Bring the soil probes back online",
            why="No soil probe is reporting, so there are no readings for soil nutrients, moisture, temperature, or Panama-disease risk - the farm's most important early warnings.",
            do="Check the RS485-LS node power/battery and confirm the probes appear again on the dashboard.",
            impact=None, urgent=True))
    elif offline:
        actions.append(dict(
            title=f"Check soil probe{'s' if len(offline) > 1 else ''} {', '.join(offline)}",
            why="One or more soil probes stopped reporting, so part of the field is unmonitored.",
            do="Check the probe wiring on the RS485 bus and the connector seals, then confirm readings return.",
            impact=None, urgent=True))
    gaps = []
    for k, d in f.items():
        if k == "sensors" or d["score"] is None or d["score"] >= 80:
            continue
        pts = round(d["weight"] * (100 - d["score"]) / 100, 1)
        t, why, do = recommend(k, d, probe_snaps)
        gaps.append((pts, t, why, do))
    gaps.sort(reverse=True)
    for pts, t, why, do in gaps:
        actions.append(dict(title=t, why=why, do=do, impact=pts, urgent=False))
    return actions

def probe_table_html(probe_snaps):
    rows = ""
    for sn in probe_snaps:
        L = sn["latest"]
        if not sn["online"]:
            state = "<span class='pill' style='color:#8a948c;background:#eef1ee'>Offline</span>"
        elif sn["panama_n"] >= 3 or (sn["avg_in_range"] is not None and sn["avg_in_range"] < 50):
            state = "<span class='pill' style='color:#c0392b;background:#fbecea'>Needs action</span>"
        elif sn["panama_n"] == 2 or (sn["avg_in_range"] is not None and sn["avg_in_range"] < 80):
            state = "<span class='pill' style='color:#b9770c;background:#fdf3e0'>Watch</span>"
        else:
            state = "<span class='pill' style='color:#2e9e54;background:#e7f5ec'>Good</span>"
        def cell(v, unit=""):
            return f"{v}{unit}" if v is not None else "&mdash;"
        pan = f"{sn['panama_n']}/4" + (f" ({'; '.join(sn['panama_conds'])})" if sn["panama_conds"] else " - none")
        rows += (f"<tr><td><b>Probe {sn['pid']}</b></td>"
                 f"<td>{cell(L['moisture'], '%')}</td><td>{cell(L['ph'])}</td>"
                 f"<td>{cell(L['ec'], ' uS/cm')}</td><td>{cell(L['soil_temp'], ' C')}</td>"
                 f"<td class='pan'>{pan}</td><td>{state}</td></tr>")
    return ("<table><tr><th>Probe</th><th>Moisture</th><th>pH</th><th>Nutrients</th>"
            "<th>Soil temp</th><th>Panama conditions</th><th>Status</th></tr>" + rows + "</table>"
            "<div class='exp' style='margin-top:4px'>Latest reading per probe. Panama conditions "
            "counts how many of the four disease-friendly conditions that spot currently meets "
            "(very wet soil, 25-30 C, acidic pH, low nutrients).</div>")

def render_html(composite, f, series, probe_snaps, last_seen, trend, generated):
    g = grade(composite)
    score_txt = composite if composite is not None else "&mdash;"
    good = [LABELS[k] for k, d in f.items() if d["score"] is not None and d["score"] >= 80 and k != "sensors"]
    n_online = sum(1 for sn in probe_snaps if sn["online"])
    bl = f"This week the farm scored <b>{score_txt}/100 ({g[0]})</b>. "
    bl += f"Soil is now watched by <b>{n_online} of {len(PROBES)} probes</b> across Area 1. "
    if good:
        bl += "Doing well: " + ", ".join(good[:3]).lower() + ". "
    if n_online == 0:
        bl += "But <b>all soil probes are offline</b> - getting them back online is the top priority. "
    else:
        bl += "See the recommended actions below to raise the score. "
    actions = build_actions(f, probe_snaps)
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
                   f"<td>{plain_state(k, d, probe_snaps)}</td>"
                   f"<td><span class='pill' style='color:{col};background:{bg}'>{lab}</span></td></tr>")
    well = [f"<li>{LABELS[k]} - {plain_state(k, f[k], probe_snaps).lower()}</li>" for k, d in f.items()
            if d["score"] is not None and d["score"] >= 80]
    well_html = ("<ul class='well'>" + "".join(well) + "</ul>") if well else "<p class='muted'>Full picture returns once the soil probes are back online.</p>"
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
.pan{{font-size:11px}}
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
<h2>Probe by probe (Area 1)</h2>{probe_table_html(probe_snaps)}
<h2>Farm at a glance</h2>
<table><tr><th>What we measure</th><th>Right now</th><th>Status</th></tr>{glance}</table>
<h2>What's going well</h2>{well_html}
<h2>Score trend (last 5 weeks)</h2>
<table class='trend'><tr>{tbars}</tr></table>
<div class='note'>How to read this: the score is a weighted average of soil and crop-health checks
(pH 18%, nutrients 18%, moisture 14%, soil temp 12%, Panama 13%, Sigatoka 13%, sensors 12%).
Soil checks now pool the readings from all {len(PROBES)} probes in Area 1, so one wet corner
shows up in both the score and the probe-by-probe table. A higher score means healthier growing
conditions. Checks with no recent reading are skipped. NPK and weather are not yet included.
Auto-generated from live sensor data.</div>
</body></html>"""

def summary_line(composite, f, probe_snaps):
    g = grade(composite)[0]
    offline = [sn["pid"] for sn in probe_snaps if not sn["online"]]
    weak = sorted([(d["weight"] * (100 - d["score"]) / 100, LABELS[k])
                   for k, d in f.items() if d["score"] is not None and d["score"] < 80], reverse=True)
    if len(offline) == len(probe_snaps):
        return f"Farm health is {g} ({composite}/100). Top priority: bring the soil probes back online."
    top = ", ".join(n for _, n in weak[:2]) if weak else "no major gaps"
    extra = f" Probe(s) {', '.join(offline)} offline." if offline else ""
    return f"Farm health is {g} ({composite if composite is not None else 'n/a'}/100). Focus: {top}.{extra}"

def send_email(pdf_bytes, composite, summary, generated):
    score = composite if composite is not None else "n/a"
    html = (f"<p>Hi team,</p><p>Here is this week's AgriPulse Farm Health Report (PDF attached) - "
            f"a plain-language snapshot of the farm with recommended actions, now covering all "
            f"{len(PROBES)} soil probes in Area 1.</p>"
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
    series, probe_series, latest_status, last_seen = collect(rows)
    probe_snaps = probe_snapshot(probe_series)
    composite, f = compute(series, probe_snaps, latest_status, last_seen)
    trend = weekly_trend(series)
    summary = summary_line(composite, f, probe_snaps)
    html = render_html(composite, f, series, probe_snaps, last_seen, trend, generated)
    pdf = HTML(string=html).write_pdf()
    print(f"PDF built ({len(pdf)} bytes). Composite={composite}. Probes online="
          f"{sum(1 for sn in probe_snaps if sn['online'])}/{len(PROBES)}.")
    send_email(pdf, composite, summary, generated)

if __name__ == "__main__":
    sys.exit(main())
