"""
Market Feed — real market data for the trading pipeline.

Provides:
  get_price(ticker)          → live quote via yfinance (free, no API key)
  get_macro_snapshot()       → real VIX + approximate fed funds rate
  get_news_headlines(ticker) → RSS headlines from Yahoo Finance (no key)

All functions return safe fallback values on failure so the pipeline
always continues even if data is temporarily unavailable.

Install: pip install yfinance feedparser
"""

import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Optional imports — fail gracefully if not installed
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

try:
    import feedparser
    _FEEDPARSER_AVAILABLE = True
except ImportError:
    _FEEDPARSER_AVAILABLE = False

# Simple in-memory cache to avoid hammering APIs on repeated calls
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL_SEC = 60  # 1-minute cache


def _cached(key: str, ttl: int = _CACHE_TTL_SEC):
    """Decorator that caches function results for ttl seconds."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            now = time.time()
            if key in _CACHE:
                ts, val = _CACHE[key]
                if now - ts < ttl:
                    return val
            result = fn(*args, **kwargs)
            _CACHE[key] = (now, result)
            return result
        return wrapper
    return decorator


# ── Price data ────────────────────────────────────────────────────────────────

def get_price(ticker: str) -> dict:
    """
    Fetch live quote for a ticker using yfinance.

    Returns:
        {
          "ticker": str,
          "price": float,        # last/current price
          "prev_close": float,
          "change_pct": float,   # % change from prev close
          "volume": int,
          "adv_30d_usd": float,  # approx 30-day avg daily dollar volume
          "market_cap_usd": float,
          "source": "yfinance" | "fallback",
          "timestamp": str (ISO),
        }
    """
    if not _YF_AVAILABLE:
        return _price_fallback(ticker, reason="yfinance not installed")

    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info  # faster than .info — doesn't load full profile

        price      = float(getattr(info, "last_price",         0) or 0)
        prev_close = float(getattr(info, "previous_close",     price) or price)
        volume     = int(  getattr(info, "three_month_average_volume", 0) or 0)
        mkt_cap    = float(getattr(info, "market_cap",         0) or 0)

        # Approximate 30-day ADV in USD
        adv_30d = price * volume if price > 0 and volume > 0 else 85_000_000

        change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

        return {
            "ticker":        ticker.upper(),
            "price":         round(price, 4),
            "prev_close":    round(prev_close, 4),
            "change_pct":    round(change_pct, 3),
            "volume":        volume,
            "adv_30d_usd":   round(adv_30d, 0),
            "market_cap_usd": round(mkt_cap, 0),
            "source":        "yfinance",
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return _price_fallback(ticker, reason=str(exc))


def _price_fallback(ticker: str, reason: str = "") -> dict:
    """Safe fallback prices for common tickers when yfinance fails."""
    DEFAULTS = {
        "AAPL": 189.50, "NVDA": 875.00, "SPY": 502.00,
        "MSFT": 415.00, "TSLA": 265.00, "META": 510.00,
        "AMZN": 185.00, "GOOGL": 175.00, "QQQ": 440.00,
    }
    price = DEFAULTS.get(ticker.upper(), 100.0)
    if reason:
        print(f"  [market_feed] Price fallback for {ticker}: {reason}")
    return {
        "ticker":        ticker.upper(),
        "price":         price,
        "prev_close":    price,
        "change_pct":    0.0,
        "volume":        0,
        "adv_30d_usd":   85_000_000,
        "market_cap_usd": 0,
        "source":        "fallback",
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }


# ── Macro snapshot ────────────────────────────────────────────────────────────

def get_macro_snapshot() -> dict:
    """
    Fetch real macro context:
      - VIX from yfinance (^VIX)
      - Fed Funds Rate approximated from ^IRX (13-week T-bill yield)

    Returns:
        {
          "vix": float,
          "fed_rate": float,
          "regime": "RISK_ON" | "RISK_OFF" | "NEUTRAL",
          "source": "yfinance" | "fallback",
          "timestamp": str,
        }
    """
    if not _YF_AVAILABLE:
        return _macro_fallback("yfinance not installed")

    try:
        vix_info = yf.Ticker("^VIX").fast_info
        vix = float(getattr(vix_info, "last_price", 18.4) or 18.4)

        irx_info = yf.Ticker("^IRX").fast_info
        fed_rate = float(getattr(irx_info, "last_price", 5.25) or 5.25)

        # Regime heuristic
        if vix < 15:
            regime = "RISK_ON"
        elif vix > 25:
            regime = "RISK_OFF"
        else:
            regime = "LATE_CYCLE"

        return {
            "vix":       round(vix, 2),
            "fed_rate":  round(fed_rate, 3),
            "regime":    regime,
            "source":    "yfinance",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return _macro_fallback(str(exc))


def _macro_fallback(reason: str = "") -> dict:
    if reason:
        print(f"  [market_feed] Macro fallback: {reason}")
    return {
        "vix": 18.4, "fed_rate": 5.25, "regime": "LATE_CYCLE",
        "source": "fallback", "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── News headlines ────────────────────────────────────────────────────────────

def get_news_headlines(ticker: str, max_items: int = 5) -> list[dict]:
    """
    Fetch recent news headlines for a ticker via Yahoo Finance RSS.
    No API key required.

    Returns list of {"headline": str, "source": str, "url": str, "published": str}
    """
    if not _FEEDPARSER_AVAILABLE:
        return _headlines_fallback(ticker, reason="feedparser not installed")

    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            items.append({
                "headline":  entry.get("title", "No title"),
                "source":    entry.get("source", {}).get("title", "Yahoo Finance") if hasattr(entry, "source") else "Yahoo Finance",
                "url":       entry.get("link", ""),
                "published": entry.get("published", ""),
            })
        if items:
            return items
        return _headlines_fallback(ticker, reason="Empty RSS feed")
    except Exception as exc:
        return _headlines_fallback(ticker, reason=str(exc))


def _headlines_fallback(ticker: str, reason: str = "") -> list[dict]:
    if reason:
        print(f"  [market_feed] Headlines fallback for {ticker}: {reason}")
    return [
        {
            "headline": f"{ticker.upper()} — no live headline available (fallback mode)",
            "source":   "fallback",
            "url":      "",
            "published": datetime.now(timezone.utc).isoformat(),
        }
    ]


# ── Composite snapshot ────────────────────────────────────────────────────────

def get_live_event(ticker: str) -> dict:
    """
    Build a complete market event dict for the pipeline bus, using real data.
    Pulls price, macro context, and top headline.

    Returns the event dict format expected by orchestrator.py / bus state.
    """
    price_data = get_price(ticker)
    headlines  = get_news_headlines(ticker, max_items=3)
    macro      = get_macro_snapshot()

    # Pick the freshest headline
    top_headline = headlines[0]["headline"] if headlines else f"{ticker} market update"
    top_source   = headlines[0]["source"]   if headlines else "Unknown"

    event = {
        "id":        f"LIVE-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}",
        "headline":  top_headline,
        "ticker":    ticker.upper(),
        "source":    top_source,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Enriched fields passed to agents via bus
        "live_price":    price_data["price"],
        "change_pct":    price_data["change_pct"],
        "adv_30d_usd":   price_data["adv_30d_usd"],
        "all_headlines": [h["headline"] for h in headlines],
        "macro":         macro,
    }

    return event


def get_live_market_context(ticker: str) -> dict:
    """
    Return the full context dict to seed the pipeline bus with real data.
    Replaces the hardcoded values in orchestrator.py.
    """
    price_data = get_price(ticker)
    macro      = get_macro_snapshot()

    return {
        "macro_context": {
            "fed_rate": macro["fed_rate"],
            "vix":      macro["vix"],
            "regime":   macro["regime"],
        },
        "market_conditions": {
            "volatility":    "elevated" if macro["vix"] > 20 else "normal",
            "spread_bps":    max(2.0, macro["vix"] / 5),   # rough vol-to-spread heuristic
            "adv_30d_usd":   price_data["adv_30d_usd"],
            "live_price":    price_data["price"],
            "change_pct":    price_data["change_pct"],
        },
    }


if __name__ == "__main__":
    # Quick test
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"\n=== Live Market Data for {ticker} ===")
    print(json.dumps(get_price(ticker), indent=2))
    print("\n=== Macro Snapshot ===")
    print(json.dumps(get_macro_snapshot(), indent=2))
    print("\n=== Headlines ===")
    for h in get_news_headlines(ticker):
        print(f"  • {h['headline']}")
