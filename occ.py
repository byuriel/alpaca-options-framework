"""
OCC option symbol construction and parsing.

Kept dependency-free (stdlib only) so it can be unit-tested and reused
without importing any broker SDK.

Alpaca OCC format (no space padding — the WebSocket rejects spaces):
    {root}{YYMMDD}{C|P}{strike * 1000:08d}
Example: SPY260513C00560000
"""

import datetime
import re
from typing import Optional, Tuple

_OCC_RE = re.compile(r'^([A-Z]+)(\d{6})([CP])(\d{8})$')


def build_occ_symbol(underlying: str, expiry: datetime.date, side: str, strike: float) -> str:
    yymmdd     = expiry.strftime("%y%m%d")
    cp         = "C" if side.upper() == "CALL" else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{yymmdd}{cp}{strike_int:08d}"


def parse_occ_symbol(symbol: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Parse an OCC symbol like SPY260520C00740000.
    Returns (side, strike) or (None, None) on failure.
    """
    m = _OCC_RE.match(symbol)
    if not m:
        return None, None
    side   = "call" if m.group(3) == "C" else "put"
    strike = int(m.group(4)) / 1000.0
    return side, strike


def parse_occ_expiry(symbol: str) -> Optional[datetime.date]:
    """Expiry date encoded in an OCC symbol, or None on failure."""
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(2), "%y%m%d").date()
    except ValueError:
        return None
