"""Price data loading.

Two sources, one output format:
  - yfinance (live)  : load_prices("SPY", start="2000-01-01")
  - local CSV (files): load_prices(csv_path="data/orcl-1995-2014.txt")

Output is always a DataFrame with a DatetimeIndex and lowercase columns
(open, high, low, close, volume). If an adjusted close is available it
replaces `close`, so returns include dividends/splits (total return).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    if "adj_close" in df.columns:
        df["close"] = df["adj_close"]
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["close"]).sort_index()
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df


def from_yfinance(symbol: str, start: str, end: str | None = None) -> pd.DataFrame:
    import yfinance as yf

    df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol!r}")
    return _normalize(df)


def from_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    date_col = "date" if "date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col])
    return _normalize(df.set_index(date_col))


def load_prices(
    symbol: str = "SPY",
    start: str = "2000-01-01",
    end: str | None = None,
    csv_path: str | None = None,
) -> pd.DataFrame:
    """CSV wins if given; otherwise pull live via yfinance."""
    if csv_path:
        return from_csv(csv_path)
    return from_yfinance(symbol, start, end)
