"""
Ecurious server (prototype): the middle of app <-> server <-> reservation hardware.

App API (JSON; driver id in X-Driver; Idempotency-Key on POST /api/reservations):
  GET  /api/chargers?lat=&lng=&radius_m=&plug=
  POST /api/reservations                 {connector_id}
  GET  /api/reservations/{id}            follows `successor` after a bay is moved
  POST /api/reservations/{id}/arrive     {connector_id}  (bay code / QR)
Hardware API (HMAC-SHA256 of the raw body in X-Signature):
  POST /api/hw/events                    {type: occupancy|session_start|session_stop, connector_id, ...}
Demo only (remove in production): POST /sim/{connector_id}/{occupied|session|stop|online}
Serves the app at /.
"""
import hmac
import json
import math
import mimetypes
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ev_reservation import (BayUnavailable, ChargerOffline, Connector, HoldExpired,
                            InvalidState, ReservationService, ReservationViolation,
                            State, WrongBay)
from dataclasses import replace
from decimal import Decimal

from ge_data import ECURIOUS_LINKS, load_stations
from pricing import Offer, Tariff, quote, sort_offers
from hardware import Bay, sign

HERE = os.path.dirname(os.path.abspath(__file__))
NO_SHOW_FEE_CENTS = 300
PREPAY_AFTER_NO_SHOWS = 2
SITES = [  # demo catalogue
    {"connector_id": "siteA-1", "site": "Demo Central Garage", "lat": 48.1374, "lng": 11.5755, "plug": "CCS2", "kw": 150},
    {"connector_id": "siteA-2", "site": "Demo Central Garage", "lat": 48.1375, "lng": 11.5757, "plug": "CCS2", "kw": 150},
    {"connector_id": "siteB-1", "site": "Demo East Car Park", "lat": 48.1404, "lng": 11.5805, "plug": "Type2", "kw": 22},
]


# Demo feeds keyed by GoingElectric station id. Not real operator prices or real grid data.
TARIFF_FEED = {
    "ge-1001": Tariff("Demo Operator A", "a-7", Decimal(49)),
    "ge-1002": Tariff("Demo Operator B", "b-3", Decimal(39), session_fee_ct=Decimal(100),
                      time_ct_per_min=Decimal(5)),
    "ge-1003": Tariff("Demo Operator C", "c-1", Decimal(50), idle_ct_per_min=Decimal(30),
                      idle_after_min=10),
}
CARBON_FEED = {"ge-1001": 210.0, "ge-1002": None, "ge-1003": 95.0, "ge-1004": 180.0}


class PriceChanged(Exception): ...


def station_of(connector_id):
    return next((sid for sid, cids in ECURIOUS_LINKS.items() if connector_id in cids), None)


class NotFound(Exception): ...
class PrepayRequired(Exception): ...


def norm_plate(p: str) -> str:
    """'B-AB 1234' and 'bab1234' are the same plate."""
    return "".join(ch for ch in p.upper() if ch.isalnum())


def haversine_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


class _BayGateway:
    """Adapts the core's ChargerGateway to the bays (stands in for the OCPP link)."""
    def __init__(self, bays):
        self.bays = bays

    def reserve_now(self, connector_id, res_id, expires_at, driver_id):
        return self.bays[connector_id].reserve_now(res_id, expires_at, driver_id)

    def cancel_reservation(self, res_id):
        for bay in self.bays.values():
            bay.cancel_reservation(res_id)

    def is_held_for(self, connector_id, res_id):
        return self.bays[connector_id].is_held_for(res_id)


class Ecurious:
    def __init__(self, bays, clock=None):
        self.bays = bays
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.catalogue = {s["connector_id"]: s for s in SITES}
        self.svc = ReservationService(_BayGateway(bays),
                                      [Connector(s["connector_id"], s["plug"]) for s in SITES])
        self.successor, self.fees, self.noshows, self.accounted = {}, {}, {}, set()
        self.plates, self.checkin_by, self.alerts = {}, {}, []
        self.tariffs, self.price_lock = dict(TARIFF_FEED), {}

    def handle(self, method, path, query, headers, body: bytes):
        with self.lock:
            try:
                return self._route(method, path, query, headers, body)
            except BayUnavailable:
                return 409, {"error": "bay_unavailable"}
            except ChargerOffline:
                return 503, {"error": "charger_offline"}
            except WrongBay:
                return 409, {"error": "wrong_bay"}
            except InvalidState as e:
                return 409, {"error": "invalid_state", "state": str(e)}
            except HoldExpired:
                return 410, {"error": "hold_expired"}
            except PrepayRequired:
                return 402, {"error": "prepayment_required"}
            except NotFound:
                return 404, {"error": "not_found"}
            except PriceChanged as e:
                return 409, {"error": "quote_changed", "quote": e.args[0]}
            except (KeyError, ValueError, ArithmeticError):
                return 400, {"error": "bad_request"}

    def _route(self, method, path, query, headers, body):
        now, driver = self.clock(), headers.get("X-Driver", "")
        if method == "GET" and path.startswith("/static/"):
            full = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
            static_root = os.path.join(HERE, "static") + os.sep
            if not full.startswith(static_root) or not os.path.isfile(full):
                return 404, {"error": "no_route"}
            with open(full, "rb") as f:
                return 200, (f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")
        if method == "GET" and path == "/":
            with open(os.path.join(HERE, "app.html"), "rb") as f:
                return 200, f.read()
        if method == "GET" and path == "/api/chargers":
            return 200, self.chargers(float(query["lat"]), float(query["lng"]),
                                      float(query.get("radius_m", 5000)), query.get("plug"))
        if method == "POST" and path == "/api/hw/events":
            if not hmac.compare_digest(headers.get("X-Signature", ""), sign(body)):
                return 401, {"error": "bad_signature"}
            self.hw_event(json.loads(body), now)
            return 202, {}
        if method == "GET" and path == "/api/stations":
            return 200, self.stations(float(query["lat"]), float(query["lng"]),
                                      float(query.get("radius_km", 5)))
        if method == "GET" and path == "/api/offers":
            return 200, self.offers(float(query["lat"]), float(query["lng"]),
                                    float(query.get("radius_km", 5)), query.get("kwh", "30"),
                                    query.get("minutes", "25"), query.get("sort", "cheapest"), now)
        if method == "POST" and path == "/api/reservations":
            data = json.loads(body)
            return self.reserve(driver, data["connector_id"], headers.get("Idempotency-Key", ""),
                                now, data.get("plate"), data.get("quote"))
        m = re.fullmatch(r"/api/reservations/([\w-]+)(/arrive)?", path)
        if m and method == "GET" and not m.group(2):
            return 200, self.view(self._get(m.group(1), driver))
        if m and method == "POST" and m.group(2):
            return self.arrive(driver, m.group(1), json.loads(body)["connector_id"], now)
        if method == "POST" and path.startswith("/sim/tariff/"):   # demo: operator reprices a station
            sid, data = path.split("/")[3], json.loads(body or b"{}")
            self.tariffs[sid] = replace(self.tariffs[sid], version=data["version"],
                                        energy_ct_per_kwh=Decimal(str(data["energy_ct"])))
            return 200, {}
        if method == "POST" and path.startswith("/sim/"):
            _, _, cid, action = path.split("/")
            bay, data = self.bays[cid], json.loads(body or b"{}")
            if action == "occupied":
                bay.set_occupied(data["occupied"])
            elif action == "session":
                ok = bay.start_session(data["id_tag"])
                return (200 if ok else 409), {"accepted": ok}
            elif action == "plate":
                bay.read_plate(data["plate"])
            elif action == "stop":
                bay.stop_session()
            elif action == "online":
                bay.online = data["online"]
            return 200, {}
        return 404, {"error": "no_route"}

    # ---- app-facing -------------------------------------------------------

    def chargers(self, lat, lng, radius_m, plug):
        out = []
        for cid, s in self.catalogue.items():
            dist = haversine_m(lat, lng, s["lat"], s["lng"])
            if dist > radius_m or (plug and s["plug"] != plug):
                continue
            c, bay = self.svc.connectors[cid], self.bays[cid]
            status = ("offline" if not bay.online else "in_use" if c.in_session
                      else "blocked" if c.occupied_by_non_ev else "reserved" if cid in self.svc.holds
                      else "free")
            out.append({"connector_id": cid, "site": s["site"], "plug": s["plug"], "kw": s["kw"],
                        "distance_m": round(dist), "status": status, "reservable": status == "free"})
        return sorted(out, key=lambda x: x["distance_m"])

    def offers(self, lat, lng, radius_km, kwh, minutes, mode, now):
        kwh_d, mins = Decimal(str(kwh)), int(minutes)
        source, stations = load_stations(lat, lng, radius_km)
        rows, priced = {}, []
        for st in stations:
            sid = st["id"]
            bays = [self._bay(cid) for cid in ECURIOUS_LINKS.get(sid, [])]
            row = {"station_id": sid, "name": st["name"], "address": st["address"],
                   "lat": st["lat"], "lng": st["lng"], "distance_m": st["distance_m"],
                   "connectors": st["connectors"], "fault": st["fault"], "bays": bays,
                   "reservable": any(b["reservable"] for b in bays),
                   "carbon_g_per_kwh": CARBON_FEED.get(sid), "quote": None}
            rows[sid] = row
            tariff = self.tariffs.get(sid)
            if tariff:
                row["quote"] = self._quote_view(quote(tariff, kwh_d, mins, now), tariff, kwh_d, mins)
                priced.append(Offer(sid, None, tariff, st["distance_m"],
                                    row["carbon_g_per_kwh"], row["reservable"]))
        order = [o.station for o in sort_offers(priced, mode, kwh_d, mins, now)]
        order += sorted((s for s in rows if s not in order), key=lambda s: rows[s]["distance_m"])
        return {"source": source, "mode": mode, "offers": [rows[s] for s in order]}

    def _quote_view(self, q, tariff, kwh, minutes):
        return {"operator": q.operator, "tariff_version": q.tariff_version,
                "energy_ct_per_kwh": str(tariff.energy_ct_per_kwh), "kwh": str(kwh),
                "minutes": minutes, "lines": [[k, str(v)] for k, v in q.lines],
                "conditional": q.conditional, "total_cents": q.total_cents,
                "valid_until": q.valid_until.isoformat()}

    def _check_price(self, connector_id, q, now):
        """The driver must confirm the price they were shown. A changed or expired quote is refused."""
        tariff = self.tariffs.get(station_of(connector_id))
        if tariff is None:
            return None
        kwh, mins = Decimal(str(q["kwh"])), int(q["minutes"])
        fresh = quote(tariff, kwh, mins, now)
        if (fresh.tariff_version != q["tariff_version"] or fresh.total_cents != int(q["total_cents"])
                or now > datetime.fromisoformat(q["valid_until"])):
            raise PriceChanged(self._quote_view(fresh, tariff, kwh, mins))
        return fresh

    def stations(self, lat, lng, radius_km):
        source, stations = load_stations(lat, lng, radius_km)
        for st in stations:
            st["bays"] = [self._bay(cid) for cid in ECURIOUS_LINKS.get(st["id"], [])]
            st["reservable"] = any(b["reservable"] for b in st["bays"])
        return {"source": source, "stations": stations}

    def _bay(self, cid):
        c, bay, s = self.svc.connectors[cid], self.bays[cid], self.catalogue[cid]
        status = ("offline" if not bay.online else "in_use" if c.in_session
                  else "blocked" if c.occupied_by_non_ev else "reserved" if cid in self.svc.holds
                  else "free")
        return {"connector_id": cid, "plug": s["plug"], "kw": s["kw"], "status": status,
                "reservable": status == "free"}

    def reserve(self, driver, connector_id, key, now, plate=None, quote_in=None):
        if not key:
            raise ValueError("Idempotency-Key required")
        if key in self.svc.by_request:            # retry: return what we already said
            res = self.svc.reservations[self.svc.by_request[key]]
            if res.driver_id != driver:
                raise NotFound(key)
            return 200, self.view(res)
        if self.noshows.get(driver, 0) >= PREPAY_AFTER_NO_SHOWS:
            raise PrepayRequired(driver)
        locked = self._check_price(connector_id, quote_in, now) if quote_in else None
        res = self.svc.request(key, driver, connector_id, now)
        if locked:
            self.price_lock[res.res_id] = locked
        if plate:
            self.plates[res.res_id] = norm_plate(plate)
        return 201, self.view(res)

    def arrive(self, driver, res_id, connector_id, now):
        res = self._get(res_id, driver)
        try:
            out = self.svc.check_in(res_id, connector_id, now)
        except HoldExpired:
            self._account(res)
            raise
        if out.state is State.ARRIVED:
            self.checkin_by[out.res_id] = "qr"
        if out.res_id != res_id:                  # the bay was blocked; we moved the driver
            self._moved(res_id, out.res_id)
        return 200, self.view(out)

    def view(self, res):
        return {"id": res.res_id, "state": res.state.value, "connector_id": res.connector_id,
                "hold_until": res.hold_until.isoformat(), "compensation": res.compensation,
                "successor": self.successor.get(res.res_id),
                "no_show_fee_cents": self.fees.get(res.res_id, 0),
                "checked_in_by": self.checkin_by.get(res.res_id),
                "locked_total_cents": (self.price_lock[res.res_id].total_cents
                                       if res.res_id in self.price_lock else None)}

    def _get(self, res_id, driver):
        res = self.svc.reservations.get(res_id)
        if res is None or res.driver_id != driver:
            raise NotFound(res_id)
        return res

    # ---- hardware-facing --------------------------------------------------

    def hw_event(self, ev, now):
        cid = ev["connector_id"]
        if ev["type"] == "occupancy":
            old = self.svc.holds.get(cid)
            new = self.svc.on_occupancy(cid, ev["occupied"], now)
            if new is not None and old and new.res_id != old:
                self._moved(old, new.res_id)
        elif ev["type"] == "session_start":
            try:
                self.svc.on_session_started(cid, ev["id_tag"], now)
            except ReservationViolation:
                pass                              # the core has already logged it
        elif ev["type"] == "plate_read":
            self._lpr(cid, norm_plate(ev["plate"]), now)
        elif ev["type"] == "session_stop":
            self.svc.on_session_stopped(cid, now)

    # ---- time and money ---------------------------------------------------

    def _moved(self, old, new):
        self.successor[old] = new
        if old in self.plates:                    # the plate follows the driver to the new bay
            self.plates[new] = self.plates[old]

    def _lpr(self, cid, plate, now):
        res_id = self.svc.holds.get(cid)
        if res_id is None:
            self.alerts.append({"connector_id": cid, "plate": plate, "event": "read_on_free_bay"})
            return
        expected = self.plates.get(res_id)
        if expected is None:
            return                                # no plate on file: this hold checks in by code
        if plate == expected:
            try:
                out = self.svc.check_in(res_id, cid, now)
            except HoldExpired:
                self._account(self.svc.reservations[res_id])
                return
            except InvalidState:
                return                            # already checked in
            if out.res_id != res_id:
                self._moved(res_id, out.res_id)
            else:
                self.checkin_by[res_id] = "lpr"
            return
        self.alerts.append({"connector_id": cid, "plate": plate, "event": "wrong_vehicle"})
        new = self.svc.on_occupancy(cid, True, now)   # a car that is not ours blocks the bay
        if new is not None and new.res_id != res_id:
            self._moved(res_id, new.res_id)

    def tick(self, now=None):
        with self.lock:
            self.svc.tick(now or self.clock())
            for res in list(self.svc.reservations.values()):
                if res.state is State.EXPIRED:
                    self._account(res)

    def _account(self, res):
        if res.res_id in self.accounted:
            return
        self.accounted.add(res.res_id)
        if not self.bays[res.connector_id].online:
            return                                # the charger failed: no fee, no strike
        self.noshows[res.driver_id] = self.noshows.get(res.driver_id, 0) + 1
        self.fees[res.res_id] = NO_SHOW_FEE_CENTS


def make_server(app, port=0):
    class Handler(BaseHTTPRequestHandler):
        def _do(self, method):
            u = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            code, obj = app.handle(method, u.path, query, self.headers, body)
            if isinstance(obj, tuple):
                data, ctype = obj
            elif isinstance(obj, bytes):
                data, ctype = obj, "text/html"
            else:
                data, ctype = json.dumps(obj).encode(), "application/json"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _ticker(app):
    while True:
        time.sleep(5)
        app.tick()


if __name__ == "__main__":
    bays = {s["connector_id"]: Bay(s["connector_id"]) for s in SITES}
    app = Ecurious(bays)
    for bay in bays.values():                    # demo: bays report straight into the server
        bay.emit = lambda ev: app.hw_event(ev, app.clock())
    threading.Thread(target=_ticker, args=(app,), daemon=True).start()
    httpd = make_server(app, 8080)
    print("Prototype on http://127.0.0.1:8080")
    httpd.serve_forever()
