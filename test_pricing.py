"""Pricing engine tests. Operators and tariffs are demo values, not real prices."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from pricing import Offer, Tariff, quote, sort_offers, still_valid

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)

A = Tariff("Demo Operator A", "a-7", D(49))                                # energy only
B = Tariff("Demo Operator B", "b-3", D(39), session_fee_ct=D(100), time_ct_per_min=D(5))
C = Tariff("Demo Operator C", "c-1", D(50), idle_ct_per_min=D(30), idle_after_min=10)

OFFERS = [
    Offer("Market Hall", "siteA-1", A, 0, 210.0),
    Offer("Station Car Park", "siteB-1", B, 480, None),        # carbon unknown
    Offer("Supermarket Lot", "ge-1003", C, 580, 95.0),
]


def test_quote_contains_only_declared_components():
    q = quote(A, D(30), 20, NOW)
    assert q.lines == [("energy", D(1470))] and q.total_cents == 1470 and q.conditional == []


def test_cheapest_depends_on_this_session_length():
    short = sort_offers(OFFERS, "cheapest", D(30), 20, NOW)
    long = sort_offers(OFFERS, "cheapest", D(30), 60, NOW)
    assert short[0].tariff.operator == "Demo Operator B"      # 1370 for 20 min
    assert long[0].tariff.operator == "Demo Operator A"       # 1470 for 60 min, B is 1570


def test_conditional_fees_are_disclosed_but_not_added():
    q = quote(C, D(30), 20, NOW)
    assert q.total_cents == 1500 and "idle fee" in q.conditional[0]


def test_rounding_happens_once_at_the_end():
    t = Tariff("Demo Operator X", "x-1", D("33.33"))
    q = quote(t, D("1.5"), 0, NOW)                            # 49.995 ct
    assert q.total_exact == D("49.995") and q.total_cents == 50


def test_quote_expires_and_tariff_changes_force_reconfirmation():
    q = quote(A, D(30), 20, NOW)
    assert still_valid(q, A, NOW + timedelta(minutes=5))
    assert not still_valid(q, A, NOW + timedelta(minutes=16))
    assert not still_valid(q, Tariff("Demo Operator A", "a-8", D(52)), NOW)


def test_greenest_ranks_by_carbon_and_puts_unknown_last():
    order = [o.station for o in sort_offers(OFFERS, "greenest", D(30), 20, NOW)]
    assert order == ["Supermarket Lot", "Market Hall", "Station Car Park"]


def test_closest_ranks_by_distance():
    order = [o.station for o in sort_offers(OFFERS, "closest", D(30), 20, NOW)]
    assert order == ["Market Hall", "Station Car Park", "Supermarket Lot"]


def test_unavailable_offers_never_lead():
    offers = [Offer("Closed", "x", A, 0, None, available=False)] + OFFERS
    assert sort_offers(offers, "closest", D(30), 20, NOW)[-1].station == "Closed"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print("\n30 kWh session, all-in totals (EUR):")
    for minutes in (20, 60):
        print(f"  {minutes} min:")
        for o in sort_offers(OFFERS, "cheapest", D(30), minutes, NOW):
            q = quote(o.tariff, D(30), minutes, NOW)
            extra = f"  + {q.conditional[0]}" if q.conditional else ""
            print(f"    {o.tariff.operator:<18} {q.total_cents / 100:>7.2f}{extra}")
    print("all pricing tests passed")
