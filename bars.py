"""Ověřené svíčky z yfinance (M2.1): správný interval, čerstvá data, volitelně jen uzavřené svíčky.

Dřív Scout počítal signál z čehokoli, co yfinance vrátil. Když Yahoo místo hodinových svíček poslalo
denní nebo včerejší data, vznikl falešný pohyb nebo objem. Teď takový případ skončí BarsError.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("TradingBot.Bars")

INTERVALS = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}


class BarsError(Exception):
    """Svíčky nejdou použít (špatný interval, stará data, málo dat)."""


def _ts(x) -> datetime:
    """Čas svíčky jako aware datetime v UTC (yfinance vrací čas burzy, denní svíčky bez času)."""
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        t = t.tz_localize(timezone.utc)
    return t.to_pydatetime().astimezone(timezone.utc)


def median_step(df: pd.DataFrame) -> Optional[timedelta]:
    """Medián odstupu sousedních svíček. Medián, ne poslední dvojice: noc, víkend nebo halt nejsou chyba intervalu."""
    if df is None or len(df) < 3:
        return None
    steps = pd.Series(df.index).diff().dropna()
    return pd.Timedelta(steps.median()).to_pytimedelta()


def assert_interval(df: pd.DataFrame, interval: timedelta, tol: float = 0.1) -> None:
    """BarsError, pokud medián kroku neodpovídá intervalu (např. Yahoo poslalo denní svíčky místo hodinových)."""
    step = median_step(df)
    if step is None:
        raise BarsError(f"málo svíček ({0 if df is None else len(df)})")
    if abs(step - interval) > interval * tol:
        raise BarsError(f"interval {step} místo {interval}")


def drop_incomplete(df: pd.DataFrame, interval: timedelta, now: Optional[datetime] = None) -> pd.DataFrame:
    """Zahodí poslední svíčku, pokud její perioda ještě neskončila (start + interval > now)."""
    if df is None or df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    if _ts(df.index[-1]) + interval > now:
        return df.iloc[:-1]
    return df


def bar_age(df: pd.DataFrame, now: Optional[datetime] = None) -> timedelta:
    """Jak dávno začala poslední svíčka."""
    now = now or datetime.now(timezone.utc)
    return now - _ts(df.index[-1])


def assert_fresh(df: pd.DataFrame, max_age: timedelta, now: Optional[datetime] = None) -> None:
    """BarsError, pokud poslední svíčka začala dřív než před max_age."""
    if df is None or df.empty:
        raise BarsError("žádné svíčky")
    age = bar_age(df, now)
    if age > max_age:
        raise BarsError(f"stará data: poslední svíčka před {age}")


def validate(df: pd.DataFrame, interval: str, max_age: Optional[timedelta] = None,
             closed_only: bool = False, now: Optional[datetime] = None) -> pd.DataFrame:
    """Ověří tabulku svíček a vrátí ji (při closed_only bez rozjeté poslední svíčky). Jinak BarsError."""
    step = INTERVALS[interval]
    if df is None or df.empty:
        raise BarsError("žádné svíčky")
    assert_interval(df, step)
    if max_age is not None:
        assert_fresh(df, max_age, now)
    if closed_only:
        df = drop_incomplete(df, step, now)
        if df.empty:
            raise BarsError("žádná uzavřená svíčka")
    return df


def fetch_bars(ticker: str, interval: str, period: str, max_age: Optional[timedelta] = None,
               closed_only: bool = True, yf_ticker=None, now: Optional[datetime] = None) -> pd.DataFrame:
    """Stáhne svíčky z yfinance a ověří je (validate). yf_ticker = už vytvořený yf.Ticker, ať se nestahuje dvakrát."""
    if yf_ticker is None:
        import yfinance as yf
        yf_ticker = yf.Ticker(ticker)
    df = yf_ticker.history(period=period, interval=interval)
    try:
        return validate(df, interval, max_age=max_age, closed_only=closed_only, now=now)
    except BarsError as e:
        raise BarsError(f"{ticker} {interval}: {e}") from None
