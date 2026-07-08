"""
CME equity-index futures contract math - ES / MES.

Everything here is deterministic arithmetic on dates and prices; no I/O, no
market data. This module is part of the shared spec that the C# NinjaScript
port must reproduce (see ninjatrader/), so keep it dependency-free and exact.

Contract facts (CME):
  ES  - E-mini S&P 500:  $50 / index point,  tick 0.25  -> $12.50 / tick
  MES - Micro E-mini:    $5  / index point,  tick 0.25  -> $1.25  / tick
  Quarterly cycle H(Mar) M(Jun) U(Sep) Z(Dec); expiry = third Friday of the
  contract month, 09:30 ET AM settlement.

Roll policy (backtest continuity + live front-month selection):
  Liquidity migrates to the next quarterly ~8 calendar days before expiry
  ("roll Thursday", the Thursday of the week before expiration week). We
  define roll_date = expiry - 8 days and treat the NEW contract as front
  from roll_date (inclusive). Live trading should confirm by volume, but
  this rule matches the observed crossover within a day.

Back-adjustment policy (repo rule, stated once here): back-adjusted series
may be used ONLY for returns/indicator continuity. All absolute levels -
stops, targets, fills - are computed on UNADJUSTED front-month prices.
"""

import datetime
from dataclasses import dataclass
from typing import List, Tuple

TICK_SIZE = 0.25          # both ES and MES

@dataclass(frozen=True)
class ContractSpec:
    root:        str       # "ES" | "MES"
    point_value: float     # $ per index point per contract
    tick_value:  float     # $ per tick per contract
    tick_size:   float = TICK_SIZE

ES  = ContractSpec(root="ES",  point_value=50.0, tick_value=12.50)
MES = ContractSpec(root="MES", point_value=5.0,  tick_value=1.25)

SPECS = {"ES": ES, "MES": MES}

MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
QUARTERLY_MONTHS = (3, 6, 9, 12)

ROLL_DAYS_BEFORE_EXPIRY = 8


def third_friday(year: int, month: int) -> datetime.date:
    """Expiration Friday for an equity-index quarterly."""
    d = datetime.date(year, month, 1)
    # weekday(): Mon=0 .. Fri=4
    first_friday = d + datetime.timedelta(days=(4 - d.weekday()) % 7)
    return first_friday + datetime.timedelta(days=14)


def expiry_date(year: int, month: int) -> datetime.date:
    if month not in QUARTERLY_MONTHS:
        raise ValueError(f"not a quarterly month: {month}")
    return third_friday(year, month)


def roll_date(year: int, month: int) -> datetime.date:
    """First session on which the (year, month) contract's SUCCESSOR is
    treated as front month."""
    return expiry_date(year, month) - datetime.timedelta(days=ROLL_DAYS_BEFORE_EXPIRY)


def front_month(d: datetime.date) -> Tuple[int, int]:
    """(year, month) of the front contract on date `d` under the roll rule:
    a contract is front from its predecessor's roll_date (inclusive) until
    the day before its own roll_date."""
    for year in (d.year, d.year + 1):
        for month in QUARTERLY_MONTHS:
            if d < roll_date(year, month):
                return (year, month)
    raise RuntimeError("front_month: exhausted search window")  # unreachable


def contract_code(root: str, year: int, month: int) -> str:
    """Exchange-style code, single-digit year: ESH6, MESM6."""
    return f"{root}{MONTH_CODES[month]}{year % 10}"


def nt_instrument_name(root: str, year: int, month: int) -> str:
    """NinjaTrader 8 instrument name: 'ES 03-26', 'MES 06-26'."""
    return f"{root} {month:02d}-{year % 100:02d}"


def front_contract_code(root: str, d: datetime.date) -> str:
    y, m = front_month(d)
    return contract_code(root, y, m)


def round_to_tick(price: float, tick: float = TICK_SIZE) -> float:
    """Round to the nearest tick. Uses integer arithmetic so 0.1-style float
    error can never produce an off-grid price."""
    return round(round(price / tick) * tick, 2)


def ticks_between(a: float, b: float, tick: float = TICK_SIZE) -> int:
    """|a - b| expressed in whole ticks (values are rounded to grid first)."""
    return int(round(abs(a - b) / tick))


def dollar_risk(stop_ticks: int, spec: ContractSpec, contracts: int) -> float:
    """The sizing identity: risk in dollars for a stop `stop_ticks` away."""
    return stop_ticks * spec.tick_value * contracts


def roll_schedule(start: datetime.date, end: datetime.date,
                  root: str = "ES") -> List[dict]:
    """Front-month segments covering [start, end] - the backtest splicing
    table. Each row: contract code, first/last session as front, expiry."""
    rows = []
    d = start
    while d <= end:
        y, m = front_month(d)
        seg_start = d
        # front until the day before this contract's roll date
        seg_end = min(roll_date(y, m) - datetime.timedelta(days=1), end)
        rows.append({
            "code":       contract_code(root, y, m),
            "nt_name":    nt_instrument_name(root, y, m),
            "front_from": seg_start,
            "front_to":   seg_end,
            "expiry":     expiry_date(y, m),
        })
        d = seg_end + datetime.timedelta(days=1)
    return rows
