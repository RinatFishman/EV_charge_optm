"""
Charge point data from GoingElectric.

Live: set GE_API_KEY and stations come from the GoingElectric API. The parser reads
the fields we need (coordinates, chargepoints, fault_report, result). Confirm those
names against a real response with your key before relying on them.

Demo (no key, or the API is unreachable): a bundled sample in the same shape.
The sample names are prefixed "Demo" so they are never mistaken for real stations.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request

GE_URL = "https://api.goingelectric.de/chargepoints/"
CACHE_S = 600
# GoingElectric station id -> Ecurious connector ids that physically sit at that station
ECURIOUS_LINKS = {"ge-1001": ["siteA-1", "siteA-2"], "ge-1002": ["siteB-1"]}

# Demo stations placed around Marienplatz, Munich. Positions are approximate and the
# names are not real locations; real Munich stations come from GoingElectric with a key.
SAMPLE = [
    {"id": "ge-1001", "name": "Demo Central Garage", "address": "Munich city centre, level P2",
     "coordinates": {"lat": 48.1374, "lng": 11.5755},
     "chargepoints": [{"type": "CCS2", "power": 150, "count": 2}]},
    {"id": "ge-1002", "name": "Demo East Car Park", "address": "Munich city centre, row C",
     "coordinates": {"lat": 48.1404, "lng": 11.5805},
     "chargepoints": [{"type": "Type 2", "power": 22, "count": 2}]},
    {"id": "ge-1003", "name": "Demo Supermarket Lot", "address": "Munich city centre, north side",
     "coordinates": {"lat": 48.1344, "lng": 11.5825},
     "chargepoints": [{"type": "CCS2", "power": 50, "count": 2}],
     "fault_report": {"created": 1761472800, "description": "Connector 2 out of service"}},
    {"id": "ge-1004", "name": "Demo Hotel", "address": "Munich city centre, hotel forecourt",
     "coordinates": {"lat": 48.1422, "lng": 11.5695},
     "chargepoints": [{"type": "Type 2", "power": 11, "count": 4}], "free": True},
]

_cache = {}


def _metres(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _normalise(raw):
    fault = raw.get("fault_report")
    return {
        "id": str(raw["id"]), "name": raw["name"], "address": raw.get("address", ""),
        "lat": float(raw["coordinates"]["lat"]), "lng": float(raw["coordinates"]["lng"]),
        "connectors": [{"type": c["type"], "kw": c["power"], "count": c.get("count", 1)}
                       for c in raw.get("chargepoints", [])],
        "fault": ({"since": fault.get("created"), "description": fault.get("description", "")}
                  if fault else None),
        "free": bool(raw.get("free", False)),
    }


def _fetch_live(lat, lng, radius_km, key):
    q = urllib.parse.urlencode({"key": key, "lat": lat, "lng": lng, "radius": radius_km})
    with urllib.request.urlopen(f"{GE_URL}?{q}", timeout=8) as r:
        return json.load(r).get("result", [])


def load_stations(lat, lng, radius_km):
    """Returns (source, stations), nearest first. source is 'live' or 'sample'."""
    key = os.environ.get("GE_API_KEY")
    ck = (round(lat, 3), round(lng, 3), radius_km)
    if ck not in _cache or time.time() - _cache[ck][0] > CACHE_S:
        source, raw = "sample", SAMPLE
        if key:
            try:
                source, raw = "live", _fetch_live(lat, lng, radius_km, key)
            except Exception:                 # API down or unexpected reply: use the sample
                pass
        _cache[ck] = (time.time(), source, [_normalise(r) for r in raw])
    _, source, everything = _cache[ck]
    nearby = [{**s, "distance_m": round(_metres(lat, lng, s["lat"], s["lng"]))}
              for s in everything]
    nearby = [s for s in nearby if s["distance_m"] <= radius_km * 1000]
    return source, sorted(nearby, key=lambda s: s["distance_m"])
