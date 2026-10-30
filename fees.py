"""Robinhood options fee model (per contract, per side). Numbers live in config.FEES."""
import math
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import config


def _nearest_penny_or_zero(x: float) -> float:
    # Robinhood: fees < $0.01 round down to 0, otherwise round to the nearest penny
    if x < 0.01:
        return 0.0
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round_up_penny(x: float) -> float:
    return math.ceil(round(x * 100, 9)) / 100.0


def buy_fees(contracts: int) -> dict:
    f = config.FEES
    d = {
        "commission": round(f["commission_per_contract"] * contracts, 2),
        "orf_occ": round(f["orf_occ_per_contract"] * contracts, 2),
        "cat": _nearest_penny_or_zero(f["cat_per_contract"] * contracts),
    }
    d["total"] = round(sum(d.values()), 2)
    return d


def taf_active(on: date) -> bool:
    f = config.FEES
    return not (f["taf_paused_from"] <= on <= f["taf_paused_to"])


def sell_fees(contracts: int, price: float, on: date) -> dict:
    f = config.FEES
    principal = price * config.CONTRACT_MULTIPLIER * contracts
    d = {
        "commission": round(f["commission_per_contract"] * contracts, 2),
        "orf_occ": round(f["orf_occ_per_contract"] * contracts, 2),
        "cat": _nearest_penny_or_zero(f["cat_per_contract"] * contracts),
        "sec": _round_up_penny(principal * f["sec_fee_per_dollar_sold"]) if principal > 0 else 0.0,
        "taf": min(_nearest_penny_or_zero(f["taf_per_contract_sold"] * contracts), f["taf_max"]) if taf_active(on) else 0.0,
    }
    d["total"] = round(sum(d.values()), 2)
    return d
