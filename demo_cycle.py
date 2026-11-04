"""
Full cycle against a running Ecurious server:
cheapest reservable offer -> reserve at the quoted price -> arrive (plate read) -> charge -> release.

Run `python3 server.py` first, then `python3 demo_cycle.py [base_url]`.
"""
import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
DRIVER, PLATE = "driver-7", "B-AB 1234"
KWH, MIN = 30, 25
OFFERS = f"/api/offers?lat=48.1374&lng=11.5755&radius_km=2&kwh={KWH}&minutes={MIN}&sort=cheapest"


def call(method, path, body=None, headers=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Content-Type": "application/json", "X-Driver": DRIVER, **(headers or {})},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def say(n, title, result, detail=""):
    print(f"[{n}] {title:<22} {result:<10} {detail}")


def eur(cents):
    return f"€{int(cents) / 100:.2f}"


def bay_status(offers, cid):
    return next(b["status"] for o in offers for b in o["bays"] if b["connector_id"] == cid)


_, found = call("GET", OFFERS)
offer = next(o for o in found["offers"] if o["reservable"])
bay = next(b["connector_id"] for b in offer["bays"] if b["reservable"])
q = offer["quote"]
say(1, "cheapest reservable", "ok",
    f"{offer['name']} · {eur(q['energy_ct_per_kwh'])}/kWh · all-in {eur(q['total_cents'])} "
    f"for {KWH} kWh / {MIN} min")

code, res = call("POST", "/api/reservations", {"connector_id": bay, "plate": PLATE, "quote": q},
                 {"Idempotency-Key": str(uuid.uuid4())})
assert code == 201, (code, res)
say(2, "reserve at quoted price", "held", f"{bay} · price locked at {eur(res['locked_total_cents'])}")

call("POST", f"/sim/{bay}/plate", {"plate": PLATE})         # camera reads the plate at the bay
_, r = call("GET", f"/api/reservations/{res['id']}")
assert r["state"] == "arrived" and r["checked_in_by"] == "lpr", r
say(3, "arrive (plate read)", r["state"], f"checked in by {r['checked_in_by']}")

code, _ = call("POST", f"/sim/{bay}/session", {"id_tag": DRIVER})   # bay starts for the holder
_, r = call("GET", f"/api/reservations/{res['id']}")
assert code == 200 and r["state"] == "charging", (code, r)
say(4, "charge", r["state"], f"{bay} delivering power to {PLATE}")

call("POST", f"/sim/{bay}/stop")                            # car unplugged, session ends
_, r = call("GET", f"/api/reservations/{res['id']}")
_, found = call("GET", OFFERS)
status = bay_status(found["offers"], bay)
assert r["state"] == "done" and status == "free", (r, status)
say(5, "release", r["state"], f"{bay} is {status} for the next driver")
print(f"cycle complete · quoted {eur(res['locked_total_cents'])} for {KWH} kWh; "
      "the final bill follows the energy the charger meters")
