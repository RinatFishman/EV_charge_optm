"""
All-in price quotes for public charging, and offer sorting.

Rules this module enforces:
- A quote contains only the components the tariff declares. Nothing is added.
- Conditional fees (e.g. idle fees) are listed separately and never added to the total.
- Money is Decimal, in cents. The total is rounded once, half-up, to whole cents.
- A quote is bound to the tariff version it came from, and it expires.
- Energy is metered at the charger. A quote is an estimate for the planned kWh and time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

ZERO = Decimal(0)


@dataclass(frozen=True)
class Tariff:
    operator: str
    version: str                          # the operator's tariff version id
    energy_ct_per_kwh: Decimal            # Decimal("49") = 0.49 EUR per kWh
    session_fee_ct: Decimal = ZERO
    time_ct_per_min: Decimal = ZERO
    idle_ct_per_min: Decimal = ZERO       # conditional: applies only after charging stops
    idle_after_min: int = 0


@dataclass
class Quote:
    operator: str
    tariff_version: str
    lines: list                           # disclosed components, exact cents
    conditional: list                     # fees that may apply later, never in the total
    total_exact: Decimal
    total_cents: int                      # total_exact rounded once, half-up
    valid_until: datetime


def quote(t: Tariff, kwh: Decimal, minutes: int, now: datetime,
          valid_for: timedelta = timedelta(minutes=15)) -> Quote:
    lines = [("energy", kwh * t.energy_ct_per_kwh)]
    if t.session_fee_ct:
        lines.append(("session fee", t.session_fee_ct))
    if t.time_ct_per_min:
        lines.append(("time", minutes * t.time_ct_per_min))
    conditional = []
    if t.idle_ct_per_min:
        conditional.append(f"idle fee {t.idle_ct_per_min} ct/min after {t.idle_after_min} min")
    total = sum((v for _, v in lines), ZERO)
    return Quote(t.operator, t.version, lines, conditional, total,
                 int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP)), now + valid_for)


def still_valid(q: Quote, current: Tariff, now: datetime) -> bool:
    """Expired, or the operator changed the tariff: the driver re-confirms. We never bill silently."""
    return now <= q.valid_until and current.version == q.tariff_version


@dataclass
class Offer:
    station: str
    bay: str
    tariff: Tariff
    distance_m: int
    carbon_g_per_kwh: Optional[float]     # grid carbon intensity now; None = unknown
    available: bool = True


def sort_offers(offers: list, mode: str, kwh: Decimal, minutes: int, now: datetime) -> list:
    """mode: 'cheapest' (for this session's kWh and minutes), 'closest', or 'greenest'."""
    def cost(o):
        return quote(o.tariff, kwh, minutes, now).total_exact
    keys = {
        "cheapest": lambda o: (cost(o), o.distance_m),
        "closest": lambda o: (o.distance_m, cost(o)),
        "greenest": lambda o: (o.carbon_g_per_kwh is None, o.carbon_g_per_kwh or 0, cost(o)),
    }
    return sorted(offers, key=lambda o: (not o.available, *keys[mode](o), o.station))
