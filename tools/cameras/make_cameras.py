#!/usr/bin/env python3
# Builds cameras.json for ZMD Car: every speed camera in Iraq from OpenStreetMap, plus the
# hand-kept official list (radar-monitored expressways with their lane limits, known points).
#
#   from the PC's extract (no extra packages):
#     python make_cameras.py --osm-json cameras-osm.json --official cameras-official.json --out cameras.json
#   on GitHub (weekly), straight from Geofabrik's file (needs: pip install osmium):
#     python make_cameras.py --pbf iraq-latest.osm.pbf --official cameras-official.json --out cameras.json
#
# The app keeps the file on the screen, so warnings work with no internet, and fetches a newer
# one once a day when online.
import argparse
import datetime
import json
import math
import re

CARDINAL = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def parse_speed(v):
    """'100' -> 100, '60 mph' -> 97, 'none'/'signals'/unknown -> None."""
    if v is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(v))
    if not m:
        return None
    n = float(m.group(1))
    if "mph" in str(v).lower():
        n *= 1.609
    n = int(round(n))
    return n if 5 <= n <= 160 else None


def parse_dirs(v):
    """Compass directions the camera watches: '90', '90;270', 'NE', '45-90'. 'forward'/'backward' -> []."""
    if not v:
        return []
    out = []
    for part in re.split(r"[;,]", str(v)):
        p = part.strip().upper()
        if not p:
            continue
        if p in CARDINAL:
            out.append(CARDINAL[p])
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", p)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            if b < a:
                b += 360
            out.append(round(((a + b) / 2) % 360, 1))
            continue
        m = re.fullmatch(r"-?\d+(?:\.\d+)?", p)
        if m:
            out.append(round(float(p) % 360, 1))
    return out


def from_osm_json(path):
    cams = []
    for c in json.load(open(path, encoding="utf-8")):
        cams.append({
            "id": c["id"], "lat": round(c["lat"], 6), "lon": round(c["lon"], 6),
            "type": "speed" if c.get("kind") == "speed" else "traffic",
            "speed": parse_speed(c.get("maxspeed")), "dirs": parse_dirs(c.get("direction")),
            "name": c.get("name") or "", "src": "osm",
        })
    return cams


def from_pbf(path):
    import osmium  # pip install osmium

    cams = []

    class Handler(osmium.SimpleHandler):
        def node(self, n):
            t = n.tags
            hw = t.get("highway")
            speed_cam = hw == "speed_camera" or "speed_camera" in t or t.get("enforcement") == "speed_camera"
            traffic_cam = t.get("man_made") == "surveillance" and (
                t.get("surveillance") == "traffic" or t.get("surveillance:type", "").upper() == "ALPR"
                or t.get("surveillance:zone") == "traffic")
            if not (speed_cam or traffic_cam):
                return
            cams.append({
                "id": "osm-%d" % n.id, "lat": round(n.location.lat, 6), "lon": round(n.location.lon, 6),
                "type": "speed" if speed_cam else "traffic",
                "speed": parse_speed(t.get("maxspeed")), "dirs": parse_dirs(t.get("direction")),
                "name": t.get("name", ""), "src": "osm",
            })

    Handler().apply_file(path, locations=False)
    return cams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm-json")
    ap.add_argument("--pbf")
    ap.add_argument("--official", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", default=datetime.date.today().isoformat())
    a = ap.parse_args()

    cams = from_pbf(a.pbf) if a.pbf else from_osm_json(a.osm_json)
    official = json.load(open(a.official, encoding="utf-8"))
    for c in official.get("cameras", []):
        c = dict(c)
        c.setdefault("type", "speed")
        c.setdefault("dirs", [])
        c.setdefault("name", "")
        c["src"] = "official"
        cams.append(c)

    # Two cameras within 15 m (the same pole mapped twice): keep the one that knows more.
    cams.sort(key=lambda c: (c["src"] != "official", c["speed"] is None, -len(c["dirs"])))
    kept = []
    for c in cams:
        dup = False
        for k in kept:
            dy = (c["lat"] - k["lat"]) * 110540
            dx = (c["lon"] - k["lon"]) * 111320 * math.cos(math.radians(c["lat"]))
            if dx * dx + dy * dy < 15 * 15 and set(c["dirs"]) == set(k["dirs"]):
                dup = True
                break
        if not dup:
            kept.append(c)
    kept.sort(key=lambda c: (round(c["lat"], 2), c["lon"]))

    out = {
        "version": a.version,
        "sources": ["© OpenStreetMap contributors (ODbL)", "مديرية المرور العامة (إعلانات منشورة)"],
        "cameras": kept,
        "zones": official.get("zones", []),
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    speeds = sum(1 for c in kept if c["speed"])
    print(f"{a.out}: {len(kept)} cameras ({speeds} with a speed), {len(out['zones'])} zones, version {a.version}")


if __name__ == "__main__":
    main()
