import os
import json
import gzip
import base64
from pathlib import Path
from datetime import datetime, timezone
import requests

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OUTPUT_DIR   = Path("ical")

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

def decompress(events_gz):
    if not events_gz:
        return []
    if events_gz.startswith("gz:"):
        try:
            return json.loads(gzip.decompress(base64.b64decode(events_gz[3:])).decode("utf-8"))
        except Exception as e:
            print("    decompress error:", e)
            return []
    try:
        return json.loads(events_gz)
    except Exception:
        return []

def unescape(s):
    return (s or "").replace("&amp;","&").replace("&lt;","<").replace("&gt;",">").replace("&quot;",'"').replace("&#39;","'")

def ics_esc(s):
    s = unescape(s)
    return s.replace("\\","\\\\").replace(",","\\,").replace(";","\\;").replace("\n","\\n")

def to_dt(date_str, time_str):
    # Datum: "2026-06-03T00:00:00" → "20260603"
    d = str(date_str or "").split("T")[0].replace("-", "")
    # Zeit: "8:15" oder "08:15" → "081500"
    t = str(time_str or "0:00").strip()
    parts = t.split(":")
    hh = parts[0].strip().zfill(2)          # "8" → "08", "08" → "08"
    mm = parts[1].strip()[:2].zfill(2) if len(parts) > 1 else "00"
    return "%sT%s%s00" % (d, hh, mm)        # "20260603T081500"

def fold(line):
    if len(line) <= 75:
        return line
    out = [line[:75]]
    rest = line[75:]
    while rest:
        out.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(out)

def matches(gruppe, ev_name, faecher_filter):
    if not faecher_filter:
        return True
    name = unescape(ev_name or "")
    for key in faecher_filter:
        parts = key.split("::")
        if len(parts) >= 2 and parts[0].strip() == gruppe and parts[1].strip() == name:
            return True
    return False

def build_ics(events, label):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//THWS Stundenplan//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + ics_esc(label),
        "X-WR-TIMEZONE:Europe/Berlin",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Berlin",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
    ]

    for ev in events:
        date   = str(ev.get("date")  or "")
        start  = str(ev.get("start") or "0:00")
        ende   = str(ev.get("ende")  or ev.get("end") or "")
        name   = unescape(str(ev.get("name")  or "Veranstaltung"))
        room   = unescape(str(ev.get("room")  or ""))
        prof   = unescape(str(ev.get("prof")  or ""))
        status = str(ev.get("status") or "normal").lower()
        gruppe = str(ev.get("_gruppe") or ev.get("group") or "")

        if not date or not ende:
            continue

        dtstart = to_dt(date, start)
        dtend   = to_dt(date, ende)

        # Validierung: muss genau 15 Zeichen haben (YYYYMMDDTHHMMSS)
        if len(dtstart) != 15 or len(dtend) != 15:
            print("  ⚠ Ungültig übersprungen:", name, date, start, ende,
                  "→", dtstart, dtend)
            continue

        cancelled = status in ("entfall", "cancelled")
        summary   = ("ENTFALL: " + name) if cancelled else name
        uid       = "%s-%s-%s-%s@thws" % (
            date[:10], start.replace(":",""), name[:20].replace(" ","-"), gruppe)

        desc = []
        if prof:      desc.append("Dozent: " + prof)
        if gruppe:    desc.append("Gruppe: " + gruppe)
        if cancelled: desc.append("Entfall")
        if status == "online": desc.append("Online")
        if status == "tutor":  desc.append("Tutorium")

        lines.append("BEGIN:VEVENT")
        lines.append("UID:" + uid)
        lines.append("DTSTAMP:" + now)
        lines.append("DTSTART;TZID=Europe/Berlin:" + dtstart)
        lines.append("DTEND;TZID=Europe/Berlin:" + dtend)
        lines.append("SUMMARY:" + ics_esc(summary))
        lines.append("STATUS:" + ("CANCELLED" if cancelled else "CONFIRMED"))
        if room:
            lines.append("LOCATION:" + ics_esc(room))
        if desc:
            lines.append("DESCRIPTION:" + "\\n".join(desc))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main():
    print("=== THWS ICS Generator", datetime.now(timezone.utc).isoformat(), "===")

    r = requests.get(
        SUPABASE_URL + "/rest/v1/ics_subscriptions?select=*",
        headers=HEADERS, timeout=15)
    subs = r.json() if r.status_code == 200 else []
    print("Abos:", len(subs))
    if not subs:
        print("Keine Abos."); return

    # Alle benötigten Gruppen sammeln
    alle_gruppen = set()
    for s in subs:
        for g in (s.get("gruppen") or "").split(","):
            g = g.strip()
            if g: alle_gruppen.add(g)

    # Cache laden
    cache = {}
    for gruppe in alle_gruppen:
        r2 = requests.get(
            SUPABASE_URL + "/rest/v1/global_stundenplan_cache"
            + "?gruppe=eq." + gruppe + "&select=events_gz",
            headers=HEADERS, timeout=15)
        if r2.status_code == 200 and r2.json():
            evs = decompress(r2.json()[0].get("events_gz", ""))
            cache[gruppe] = evs
            if evs:
                e = evs[0]
                print("  Cache %s: %d Events, z.B. %s %s %s-%s" % (
                    gruppe, len(evs),
                    e.get("name","?"), e.get("date","?"),
                    e.get("start","?"), e.get("ende","?")))

    OUTPUT_DIR.mkdir(exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = []

    for sub in subs:
        token   = sub.get("token", "")
        label   = sub.get("label") or "THWS Stundenplan"
        gruppen = [g.strip() for g in (sub.get("gruppen") or "").split(",") if g.strip()]
        try:
            faecher_filter = json.loads(sub.get("faecher") or "[]")
        except Exception:
            faecher_filter = []

        if not token or not gruppen:
            continue

        all_events = []
        seen = set()
        for gruppe in gruppen:
            for ev in cache.get(gruppe, []):
                uid = "%s-%s-%s" % (ev.get("date"), ev.get("start"), ev.get("name"))
                if uid in seen: continue
                seen.add(uid)
                if not matches(gruppe, ev.get("name", ""), faecher_filter):
                    continue
                ev2 = dict(ev)
                ev2["_gruppe"] = gruppe
                all_events.append(ev2)

        ics = build_ics(all_events, label)
        path = OUTPUT_DIR / (token + ".ics")
        path.write_text(ics, encoding="utf-8")
        print("  ✓ %s… %d Events (%s)" % (token[:12], len(all_events), ", ".join(gruppen)))
        updated.append(token)

    for token in updated:
        requests.patch(
            SUPABASE_URL + "/rest/v1/ics_subscriptions?token=eq." + token,
            headers=dict(HEADERS, **{"Prefer": "return=minimal"}),
            json={"last_generated": now_iso},
            timeout=10)

    print("\n✅ Fertig —", len(updated), "ICS-Dateien generiert.")

if __name__ == "__main__":
    main()
