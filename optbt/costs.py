"""Statutory + broker charges for one executed options order."""
from __future__ import annotations

from datetime import date

from .instruments import InstrumentSpec


def leg_costs(spec: InstrumentSpec, trade_date: date, price: float, qty: int, is_buy: bool) -> float:
    """Total charges (Rs) for a single fill.

    brokerage: flat per order (spec.brokerage_per_order, 0 unless you set it), optionally capped
        at a % of turnover
    STT: sell side only, on premium turnover (rate depends on trade date)
    exchange transaction charge, IPFT, SEBI fee: both sides, on premium turnover
    stamp duty: buy side only
    GST: on brokerage + exchange charge + SEBI fee + IPFT
    """
    turnover = price * qty
    brokerage = spec.brokerage_per_order
    if spec.brokerage_cap_pct is not None:
        brokerage = min(brokerage, turnover * spec.brokerage_cap_pct)
    stt = 0.0 if is_buy else turnover * spec.stt_rate_on(trade_date)
    txn = turnover * spec.txn_rate_on(trade_date)
    ipft = turnover * spec.ipft_rate
    sebi = turnover * spec.sebi_rate
    stamp = turnover * spec.stamp_rate if is_buy else 0.0
    gst = spec.gst_rate * (brokerage + txn + sebi + ipft)
    return brokerage + stt + txn + ipft + sebi + stamp + gst
