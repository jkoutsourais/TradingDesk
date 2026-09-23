"""Canonical desk symbols and their spelling at each source.

Canonical forms: US listings with a dot for share classes ("BRK.B"), futures roots with a
leading slash ("/GC"), and index or macro symbols in Yahoo form ("^VIX", "DX-Y.NYB"),
since Yahoo is their only source.
"""


def is_future(symbol: str) -> bool:
    return symbol.startswith("/")


def to_yahoo(symbol: str) -> str:
    if is_future(symbol):
        return f"{symbol[1:]}=F"  # continuous front month, e.g. GC=F
    if symbol.startswith("^") or symbol.endswith(".NYB"):
        return symbol
    return symbol.replace(".", "-")


def to_tastytrade(symbol: str) -> str:
    """Equity or ETF symbol as tastytrade and DXLink spell it; futures resolve separately."""
    return symbol.replace(".", "/")


def from_tastytrade(symbol: str) -> str:
    return symbol.replace("/", ".") if not is_future(symbol) else symbol
