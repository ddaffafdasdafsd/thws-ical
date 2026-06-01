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

def decompress(events_gz: str) -> list:
    if not events_gz:
        return []
    if events_gz.startswith("gz:"):
        try:
            return json.loads(gzip.decompress(base64.b64decode(events_gz[3:])).decode("utf-8"))
        except Exception as e:
            print(f"    decompress error: {e}")
            return []
    try:
        return json.loads(events_gz)
    except Exception:
        return []

def unescape_html(s: str) -> str:
    return (s or "").replace("&amp;","&").replace("&lt;","<")\
                    .replace("&gt;",">").replace("&quot;",'"').replace("&#39;","'")

def ics_escape(s: str) -> str:
    s = unescape_html(s)
    return s.replace("\\","\\\\").replace(",","\\,").replace(";","\\;").replace("\n","\\n")

def to_ics_dt(date_str: str, time_str: str) -> str:
    """Wandelt Datum + Zeit in ICS-Format. Behandelt '8:15' und '08:15' korrekt."""
    d = (date_str or "").split("T")[0].replace("-", "")  # YYYYMMDD
    parts = (time_str or "0:00").split(":")
    hh = parts[0].strip().zfill(2)   # "8" → "08", "08" → "08"
    mm = (parts[1].strip() if len(parts) > 1 else "00").zfill(2)
    return f"{d}T{hh}{mm}00"

def fold_line(line: str) -> str:
    if len(line) <= 75:
        return line
    parts = [line[:75]]
    rest  = line[75:]
    while rest:
        parts.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(parts)

def matches_filter(gruppe: str, ev_name: str, faecher_filter: list) -> bool:
    if not faecher_filter:
        return True
    name = unescape_html(ev_name or "")
    for key in faecher_filter:
        parts = key.split("::")
        if len(parts) >= 2 and parts[0].strip() == gruppe and parts[1].strip() == name:
            return True
    return False

def build_ics(events: list, label: str) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//THWS Stundenplan//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(label)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Berlin",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200","TZOFFSETTO:+0100","TZNAME:CET",
        "DTSTART:19701025T030000","RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100","TZOFFSETTO:+0200","TZNAME:CEST",
        "DTSTART:19700329T020000","RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
    ]

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for ev in events:
        date   = str(ev.get("date")  or "")
        start  = str(ev.get("start") or "0:00")
        end    = str(ev.get("ende")  or ev.get("end") or "")
        name   = unescape_html(str(ev.get("name")  or "Veranstaltung"))
        room   = unescape_html(str(ev.get("room")  or ""))
        prof   = unescape_html(str(ev.get("prof")  or ""))
        status = str(ev.get("status") or "normal").lower()
        gruppe = str(ev.get("_gruppe") or ev.get("group") or "")

        if not date or not end:
            continue

        # Validierung: Datum und Zeit müssen sinnvoll sein
        dtstart = to_ics_dt(date, start)
        dtend   = to_ics_dt(date, end)

        if len(dtstart) != 15 or len(dtend) != 15:
            print(f"  ⚠ Ungültige Zeit übersprungen: {name} {date} {start}-{end}")
            continue

        cancelled = status in ("entfall", "cancelled")
        summary   = f"ENTFALL: {name}" if cancelled else name
        uid       = f"{date[:10]}-{start}-{name[:30].replace(' ','-')}-{gruppe}@thws-ical"

        desc_parts = []
        if prof:      desc_parts.append(f"Dozent: {prof}")
        if gruppe:    desc_parts.append(f"Gruppe: {gruppe}")
        if cancelled: desc_parts.append("Entfall / Abgesagt")
        if status == "online": desc_parts.append("Online-Veranstaltung")
        if status == "tutor":  desc_parts.append("Tutorium")

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;TZID=Europe/Berlin:{dtstart}",
            f"DTEND;TZID=Europe/Berlin:{dtend}",
            f"SUMMARY:{ics_escape(summary)}",
            f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        ]
        if room:           lines.append(f"LOCATION:{ics_escape(room)}")
        if desc_parts:     lines.append(f"DESCRIPTION:{chr(92)+'n'.join(desc_parts)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(l) for l in lines) + "\r\n"

def main():
    print(f"=== THWS ICS Generator — {datetime.now(timezone.utc).isoformat()} ===")

    r = requests.get(f"{SUPABASE_URL}/rest/v1/ics_subscriptions?select=*",
                     headers=HEADERS, timeout=15)
    subs = r.json() if r.status_code == 200 else []
    print(f"Abos: {len(subs)}")
    if not subs:
        print("Keine Abos — fertig.")
        return

    alle_gruppen = set()
    for s in subs:
        for g in (s.get("gruppen") or "").split(","):
            g = g.strip()
            if g: alle_gruppen.add(g)

    cache: dict = {}
    for gruppe in alle_gruppen:
        r2 = requests.get(
            f"{SUPABASE_URL}/rest/v1/global_stundenplan_cache"
            f"?gruppe=eq.{gruppe}&select=events_gz",
            headers=HEADERS, timeout=15)
        if r2.status_code == 200 and r2.json():
            events = decompress(r2.json()[0].get("events_gz", ""))
            cache[gruppe] = events
            # Debug: erste Event ausgeben
            if events:
                ev = events[0]
                print(f"  Cache {gruppe}: {len(events)} Events, Beispiel: {ev.get('name')} {ev.get('date')} {ev.get('start')}-{ev.get('ende')}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = []

    for sub in subs:
        token   = sub.get("token", "")
        label   = sub.get("label") or "THWS Stundenplan"
        gruppen = [g.strip() for g in (sub.get("gruppen") or "").split(",") if g.strip()]
        faecher_filter = []
        try:
            faecher_filter = json.loads(sub.get("faecher") or "[]")
        except Exception:
            pass

        if not token or not gruppen:
            continue

        all_events: list = []
        seen: set        = set()
        for gruppe in gruppen:
            for ev in cache.get(gruppe, []):
                uid = f"{ev.get('date')}-{ev.get('start')}-{ev.get('name')}"
                if uid in seen:
                    continue
                seen.add(uid)
                if not matches_filter(gruppe, ev.get("name", ""), faecher_filter):
                    continue
                all_events.append({**ev, "_gruppe": gruppe})

        ics_content = build_ics(all_events, label)
        out_path    = OUTPUT_DIR / f"{token}.ics"
        out_path.write_text(ics_content, encoding="utf-8")
        print(f"  ✓ {token[:12]}…  {len(all_events)} Events  ({', '.join(gruppen)})")
        updated.append(token)

    for token in updated:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/ics_subscriptions?token=eq.{token}",
            headers={**HEADERS, "Prefer": "return=minimal"},
            json={"last_generated": now_iso},
            timeout=10)

    print(f"\n✅ Fertig — {len(updated)} ICS-Dateien generiert.")

if __name__ == "__main__":
    main()   
