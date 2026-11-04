"""
Simulated reservation hardware for one bay. Stands in for an OCPP charge point.

The bay enforces its own hold: while held, it only starts a session for the
reserved driver. It reports occupancy and sessions to the Ecurious server as
HMAC-signed events.
"""
import hashlib
import hmac
import json
import urllib.request

from ev_reservation import ChargerOffline

HW_SECRET = b"demo-secret"  # one secret per bay in production


def sign(body: bytes) -> str:
    return hmac.new(HW_SECRET, body, hashlib.sha256).hexdigest()


def post_event(base_url: str, event: dict) -> None:
    body = json.dumps(event).encode()
    req = urllib.request.Request(f"{base_url}/api/hw/events", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Signature": sign(body)})
    urllib.request.urlopen(req, timeout=5).close()


class Bay:
    def __init__(self, connector_id: str):
        self.connector_id = connector_id
        self.emit = None              # delivers this bay's events to the server
        self.online = True
        self.held_res = None          # reservation the bay is holding
        self.held_driver = None       # the only driver it will start a session for
        self.occupied = False         # bay sensor: a non-EV is standing in the bay
        self.in_session = False

    # Commands from the server (what OCPP ReserveNow / CancelReservation do)
    def reserve_now(self, res_id, expires_at, driver_id) -> bool:
        if not self.online:
            raise ChargerOffline(self.connector_id)
        if self.held_res or self.in_session or self.occupied:
            return False
        self.held_res, self.held_driver = res_id, driver_id
        return True

    def cancel_reservation(self, res_id) -> None:
        if self.held_res == res_id:
            if not self.online:
                raise ChargerOffline(self.connector_id)
            self.held_res = self.held_driver = None

    def is_held_for(self, res_id) -> bool:
        return self.held_res == res_id

    # Physical events (plug and sensor)
    def set_occupied(self, occupied: bool) -> None:
        self.occupied = occupied
        self._emit({"type": "occupancy", "occupied": occupied})

    def start_session(self, id_tag: str) -> bool:
        if self.held_driver and id_tag != self.held_driver:
            return False              # enforced here, not left to the app
        self.in_session = True
        self._emit({"type": "session_start", "id_tag": id_tag})
        return True

    def stop_session(self) -> None:
        self.in_session = False
        self.held_res = self.held_driver = None
        self._emit({"type": "session_stop"})

    def read_plate(self, plate: str) -> None:
        self._emit({"type": "plate_read", "plate": plate})

    def _emit(self, event: dict) -> None:
        if self.emit and self.online:
            self.emit({**event, "connector_id": self.connector_id})
