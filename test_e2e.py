"""End-to-end tests: app -> Ecurious server over HTTP -> bays (signed events over HTTP)."""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from hardware import Bay, post_event
from server import SITES, Ecurious, make_server

T0 = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
HERE = "lat=48.1374&lng=11.5755&radius_m=3000"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


def system():
    clock = Clock()
    bays = {s["connector_id"]: Bay(s["connector_id"]) for s in SITES}
    app = Ecurious(bays, clock=clock)
    httpd = make_server(app, 0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    for bay in bays.values():                 # bays report over signed HTTP, as real hardware would
        bay.emit = lambda ev: post_event(base, ev)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app, base, bays, clock, httpd


def call(base, method, path, driver=None, key=None, body=None, sig=None):
    headers = {"Content-Type": "application/json"}
    if driver:
        headers["X-Driver"] = driver
    if key:
        headers["Idempotency-Key"] = key
    if sig:
        headers["X-Signature"] = sig
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_full_journey_with_relocation():
    app, base, bays, clock, httpd = system()
    try:
        s, chargers = call(base, "GET", f"/api/chargers?{HERE}")
        assert s == 200 and any(c["connector_id"] == "siteA-1" and c["reservable"] for c in chargers)
        s, res = call(base, "POST", "/api/reservations", "driver-7", "k1", {"connector_id": "siteA-1"})
        assert s == 201 and res["state"] == "confirmed" and bays["siteA-1"].held_res == res["id"]
        _, again = call(base, "POST", "/api/reservations", "driver-7", "k1", {"connector_id": "siteA-1"})
        assert again["id"] == res["id"]                                  # retry: same hold
        s, _ = call(base, "POST", "/api/reservations", "driver-8", "k2", {"connector_id": "siteA-1"})
        assert s == 409                                                   # bay already held
        bays["siteA-1"].set_occupied(True)                                # a car parks in the bay
        _, old = call(base, "GET", f"/api/reservations/{res['id']}", "driver-7")
        assert old["state"] == "blocked" and old["successor"]             # moved before arrival
        _, new = call(base, "GET", f"/api/reservations/{old['successor']}", "driver-7")
        assert new["connector_id"] == "siteA-2" and bays["siteA-2"].held_res == new["id"]
        s, arrived = call(base, "POST", f"/api/reservations/{new['id']}/arrive", "driver-7",
                          body={"connector_id": "siteA-2"})
        assert s == 200 and arrived["state"] == "arrived"
        assert bays["siteA-2"].start_session("driver-7")
        _, live = call(base, "GET", f"/api/reservations/{new['id']}", "driver-7")
        assert live["state"] == "charging"
        bays["siteA-2"].stop_session()
        _, done = call(base, "GET", f"/api/reservations/{new['id']}", "driver-7")
        assert done["state"] == "done"
    finally:
        httpd.shutdown()


def test_no_shows_are_charged_and_repeat_offenders_must_prepay():
    app, base, bays, clock, httpd = system()
    try:
        for i in range(2):
            _, r = call(base, "POST", "/api/reservations", "driver-8", f"n{i}", {"connector_id": "siteA-2"})
            clock.advance(25)
            app.tick()
            _, v = call(base, "GET", f"/api/reservations/{r['id']}", "driver-8")
            assert v["state"] == "expired" and v["no_show_fee_cents"] == 300
            assert bays["siteA-2"].held_res is None                      # bay is free again
        s, _ = call(base, "POST", "/api/reservations", "driver-8", "n9", {"connector_id": "siteA-2"})
        assert s == 402                                                   # two strikes: prepay
        _, late = call(base, "POST", "/api/reservations", "driver-9", "late", {"connector_id": "siteB-1"})
        clock.advance(25)
        s, err = call(base, "POST", f"/api/reservations/{late['id']}/arrive", "driver-9",
                      body={"connector_id": "siteB-1"})
        assert s == 410 and err["error"] == "hold_expired"
        assert app.fees[late["id"]] == 300                                # late counts as a no-show
    finally:
        httpd.shutdown()


def test_hardware_enforces_the_hold_and_offline_bays_never_confirm():
    app, base, bays, clock, httpd = system()
    try:
        _, r = call(base, "POST", "/api/reservations", "driver-9", "h1", {"connector_id": "siteB-1"})
        assert bays["siteB-1"].start_session("walk-in") is False          # bay refuses a stranger
        bays["siteA-1"].online = False
        s, _ = call(base, "POST", "/api/reservations", "driver-10", "h2", {"connector_id": "siteA-1"})
        assert s == 503 and len(app.svc.reservations) == 1                # nothing held, nothing confirmed
        _, c = call(base, "GET", f"/api/chargers?{HERE}")
        assert next(x for x in c if x["connector_id"] == "siteA-1")["status"] == "offline"
        assert bays["siteB-1"].start_session("driver-9")
        _, live = call(base, "GET", f"/api/reservations/{r['id']}", "driver-9")
        assert live["state"] == "charging"
    finally:
        httpd.shutdown()


def test_forged_hardware_events_are_rejected():
    app, base, bays, clock, httpd = system()
    try:
        ev = {"type": "occupancy", "connector_id": "siteA-1", "occupied": True}
        s, _ = call(base, "POST", "/api/hw/events", body=ev, sig="forged")
        assert s == 401
        _, c = call(base, "GET", f"/api/chargers?{HERE}")
        assert all(x["status"] != "blocked" for x in c)
    finally:
        httpd.shutdown()


def test_lpr_checks_in_the_reserved_car_and_flags_the_wrong_one():
    app, base, bays, clock, httpd = system()
    try:
        _, r = call(base, "POST", "/api/reservations", "driver-7", "p1",
                    {"connector_id": "siteA-1", "plate": "B-AB 1234"})
        bays["siteA-1"].read_plate("XY-99 001")                     # another car pulls in first
        _, old = call(base, "GET", f"/api/reservations/{r['id']}", "driver-7")
        assert old["state"] == "blocked" and old["successor"]        # wrong car blocks the bay
        bays["siteA-2"].read_plate("b-ab1234")                       # our car, different formatting
        _, new = call(base, "GET", f"/api/reservations/{old['successor']}", "driver-7")
        assert new["state"] == "arrived" and new["checked_in_by"] == "lpr"
        assert any(a["event"] == "wrong_vehicle" for a in app.alerts)
    finally:
        httpd.shutdown()


def test_stations_merge_goingelectric_data_with_live_bay_status():
    app, base, bays, clock, httpd = system()
    try:
        bays["siteA-2"].online = False
        s, body = call(base, "GET", "/api/stations?lat=48.1374&lng=11.5755&radius_km=2")
        assert s == 200 and body["source"] == "sample"
        market = next(x for x in body["stations"] if x["id"] == "ge-1001")
        assert market["reservable"] and [b["status"] for b in market["bays"]] == ["free", "offline"]
        assert any(x["fault"] for x in body["stations"])             # fault reports come through
    finally:
        httpd.shutdown()


OFFERS = "/api/offers?lat=48.1374&lng=11.5755&radius_km=2&kwh=30&minutes=25&sort="


def test_offers_put_reservable_cheapest_first_and_unpriced_last():
    app, base, bays, clock, httpd = system()
    try:
        _, body = call(base, "GET", OFFERS + "cheapest")
        order = [o["station_id"] for o in body["offers"]]
        assert order == ["ge-1002", "ge-1001", "ge-1003", "ge-1004"]   # 13.95, 14.70, then no free bay
        assert body["offers"][-1]["quote"] is None                      # no published price, last
    finally:
        httpd.shutdown()


def test_greenest_and_closest_reorder_the_same_offers():
    app, base, bays, clock, httpd = system()
    try:
        _, green = call(base, "GET", OFFERS + "greenest")
        assert [o["station_id"] for o in green["offers"]][:2] == ["ge-1001", "ge-1002"]  # 210 g, then unknown
        _, close = call(base, "GET", OFFERS + "closest")
        assert [o["station_id"] for o in close["offers"]][0] == "ge-1001"
    finally:
        httpd.shutdown()


def test_reservation_holds_the_quoted_price_and_a_repriced_quote_must_be_confirmed_again():
    app, base, bays, clock, httpd = system()
    try:
        _, body = call(base, "GET", OFFERS + "cheapest")
        q = next(o for o in body["offers"] if o["station_id"] == "ge-1002")["quote"]
        call(base, "POST", "/sim/tariff/ge-1002", body={"version": "b-4", "energy_ct": 45})
        s, err = call(base, "POST", "/api/reservations", "driver-7", "q1",
                      {"connector_id": "siteB-1", "plate": "B-AB 1234", "quote": q})
        assert s == 409 and err["error"] == "quote_changed" and err["quote"]["tariff_version"] == "b-4"
        assert bays["siteB-1"].held_res is None                        # nothing held at the old price
        s, res = call(base, "POST", "/api/reservations", "driver-7", "q2",
                      {"connector_id": "siteB-1", "plate": "B-AB 1234", "quote": err["quote"]})
        assert s == 201 and res["locked_total_cents"] == err["quote"]["total_cents"] == 1575
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print("all end-to-end tests passed")
