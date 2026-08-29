#!/usr/bin/env python3
"""Build data.json for the State of DFDV dashboard.

Every number on the dashboard comes from one of four public sources:

  1. defidevcorp.com/api/dashboard/*  DFDV's own metrics API (SOL price, holdings,
                                      share count, debt tranches, options OI, dfdvSOL rate)
  2. api.nasdaq.com                   DFDV/BSOL OHLCV, short interest, institutional holders
  3. api.hyperliquid.xyz              hourly SOL perp funding, for the theoretical perp line
  4. stockanalysis.com                public float (falls back to shares outstanding)

Nothing here needs an API key.

    python3 refresh_data.py --dry-run     # fetch + print a summary, write nothing
    python3 refresh_data.py               # write data.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from bisect import bisect_right

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi is only needed on macOS
    SSL_CTX = ssl.create_default_context()

HERE = __file__.rsplit("/", 1)[0]
OUT = f"{HERE}/data.json"

# ---------------------------------------------------------------------------
# Methodology constants. These are the only judgement calls in the file; they are
# surfaced verbatim in the dashboard's methodology panel so nothing is hidden.
# ---------------------------------------------------------------------------

# Board adopted the digital-asset treasury policy on 2025-04-04 (announced 04-07).
# DFDV's own API measures `sse_return` off the 04-04 close, so we match it exactly.
INCEPTION = "2025-04-04"
# First SOL purchase settled 2025-04-11; that is where the treasury series begins and
# therefore where any leverage/SPS-based comparison can honestly start.
TREASURY_START = "2025-04-11"

ETF_MGMT_FEE = 0.0020  # BSOL headline sponsor fee, 20bps
ETF_STAKING_FEE = 0.06  # BSOL's cut of staking rewards after the launch waiver
PERP_TAKER_FEE = 0.00045  # Hyperliquid taker fee, charged once at entry
BSOL_TICKER = "BSOL"
BSOL_INCEPTION = "2025-10-28"

DFDV_API = "https://defidevcorp.com/api/dashboard"
NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
PLAIN_UA = {"User-Agent": NASDAQ_HEADERS["User-Agent"]}

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  ! {msg}", file=sys.stderr)


def _get(url: str, headers=None, tries=3, timeout=30) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or PLAIN_UA)
            return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX).read()
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            last = exc
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def get_json(url: str, headers=None, **kw):
    return json.loads(_get(url, headers, **kw).decode("utf-8", "ignore"))


def dfdv_api(path: str):
    """GET one of DFDV's dashboard endpoints and unwrap the {success, data} envelope."""
    payload = get_json(f"{DFDV_API}/{path}")
    if not payload.get("success"):
        raise RuntimeError(f"DFDV API returned success=false for {path}")
    return payload["data"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def money(s) -> float | None:
    """'$4.98' / '1,234' / 4.98 -> float."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).replace("$", "").replace(",", "").replace("%", "").strip()
    if s in ("", "N/A", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def step_lookup(steps: list[tuple[str, float]], date: str):
    """Value of a step function (sorted [(date, value)]) as of `date`."""
    keys = [d for d, _ in steps]
    i = bisect_right(keys, date) - 1
    return steps[i][1] if i >= 0 else None


def daterange(start: str, end: str):
    d = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def nasdaq_ohlcv(symbol: str, assetclass: str, fromdate: str) -> dict[str, dict]:
    """{iso date: {close, open, high, low, volume}} from Nasdaq, split-adjusted."""
    today = dt.date.today().isoformat()
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/historical?assetclass={assetclass}"
        f"&limit=9999&fromdate={fromdate}&todate={today}"
    )
    data = get_json(url, NASDAQ_HEADERS).get("data") or {}
    rows = (data.get("tradesTable") or {}).get("rows") or []
    out: dict[str, dict] = {}
    for r in rows:
        iso = dt.datetime.strptime(r["date"], "%m/%d/%Y").date().isoformat()
        close = money(r.get("close"))
        if close is None:
            continue
        out[iso] = {
            "close": close,
            "open": money(r.get("open")),
            "high": money(r.get("high")),
            "low": money(r.get("low")),
            "volume": int(money(r.get("volume")) or 0),
        }
    return dict(sorted(out.items()))


def nasdaq_short_interest(symbol: str) -> list[dict]:
    url = f"https://api.nasdaq.com/api/quote/{symbol}/short-interest?assetClass=stocks"
    rows = (get_json(url, NASDAQ_HEADERS)["data"]["shortInterestTable"] or {}).get("rows") or []
    out = []
    for r in rows:
        out.append(
            {
                "date": dt.datetime.strptime(r["settlementDate"], "%m/%d/%Y").date().isoformat(),
                "shares": int(money(r["interest"]) or 0),
                "avg_daily_volume": int(money(r.get("avgDailyShareVolume")) or 0),
                "days_to_cover": money(r.get("daysToCover")),
            }
        )
    return sorted(out, key=lambda r: r["date"])


def nasdaq_summary(symbol: str) -> dict:
    url = f"https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=stocks"
    sd = (get_json(url, NASDAQ_HEADERS).get("data") or {}).get("summaryData") or {}
    return {k: v.get("value") for k, v in sd.items() if isinstance(v, dict)}


def nasdaq_holders(symbol: str, limit: int = 25) -> dict:
    url = (
        f"https://api.nasdaq.com/api/company/{symbol}/institutional-holdings"
        f"?limit={limit}&type=TOTAL&sortColumn=marketValue&sortOrder=DESC"
    )
    data = get_json(url, NASDAQ_HEADERS).get("data") or {}
    summary = data.get("ownershipSummary") or {}
    rows = ((data.get("holdingsTransactions") or {}).get("table") or {}).get("rows") or []
    def iso(d):
        """Nasdaq returns holder dates as M/D/YYYY; normalise so the page can format them."""
        try:
            return dt.datetime.strptime(str(d).strip(), "%m/%d/%Y").date().isoformat()
        except (ValueError, TypeError):
            return None

    holders = []
    for r in rows:
        holders.append(
            {
                "name": (r.get("ownerName") or "").title(),
                "date": iso(r.get("date")),
                "shares": int(money(r.get("sharesHeld")) or 0),
                "change": int(money(r.get("sharesChange")) or 0),
                "change_pct": money(r.get("sharesChangePCT")),
                "value_usd": int((money(r.get("marketValue")) or 0) * 1000),
            }
        )
    active = {}
    for key in ("activePositions", "newSoldOutPositions"):
        for r in (data.get(key) or {}).get("rows") or []:
            active[r["positions"]] = {
                "holders": int(money(r.get("holders")) or 0),
                "shares": int(money(r.get("shares")) or 0),
            }
    # Holders report on different dates; the bulk share the latest quarter-end.
    dates_seen = [h["date"] for h in holders if h["date"]]
    return {
        "as_of": max(dates_seen) if dates_seen else None,
        "institutional_ownership_pct": money((summary.get("SharesOutstandingPCT") or {}).get("value")),
        "total_value_musd": money((summary.get("TotalHoldingsValue") or {}).get("value")),
        "positions": active,
        "holders": holders,
    }


def hyperliquid_funding(start_iso: str) -> dict[str, float]:
    """{iso date: summed hourly funding rate for that day} for the SOL perp."""
    start_ms = int(dt.datetime.fromisoformat(start_iso).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    daily: dict[str, float] = {}
    cursor = start_ms
    hour = 3_600_000
    guard = 0
    while cursor < now_ms and guard < 200:
        guard += 1
        body = json.dumps(
            {"type": "fundingHistory", "coin": "SOL", "startTime": cursor, "endTime": min(cursor + 500 * hour, now_ms)}
        ).encode()
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=body,
            headers={"Content-Type": "application/json", **PLAIN_UA},
        )
        try:
            rows = json.loads(urllib.request.urlopen(req, timeout=30, context=SSL_CTX).read())
        except Exception as exc:  # noqa: BLE001
            warn(f"Hyperliquid funding page failed at {cursor}: {exc}")
            break
        if not rows:
            cursor += 500 * hour
            continue
        for r in rows:
            iso = dt.datetime.fromtimestamp(r["time"] / 1000, dt.timezone.utc).date().isoformat()
            daily[iso] = daily.get(iso, 0.0) + float(r["fundingRate"])
        cursor = rows[-1]["time"] + hour
        time.sleep(0.12)
    return daily


def stockanalysis_stats(symbol: str) -> dict:
    """Public float and insider ownership, parsed out of stockanalysis.com's inline JSON.

    The page embeds rows like {id:"float",title:"Float",value:"24.33M",hover:"24,333,804"};
    the `hover` field carries the unrounded number, so prefer it over `value`.
    """
    out: dict = {}
    try:
        html = _get(f"https://stockanalysis.com/stocks/{symbol.lower()}/statistics/").decode("utf-8", "ignore")
        for key in ("float", "sharesInsiders", "sharesInstitutions"):
            m = re.search(rf'id:"{key}",title:"[^"]*",value:"([^"]*)",hover:"([^"]*)"', html)
            if m:
                out[key] = money(m.group(2)) or money(m.group(1))
        if out.get("float"):
            out["source"] = "stockanalysis.com"
    except Exception as exc:  # noqa: BLE001
        warn(f"float lookup failed ({exc}); falling back to shares outstanding")
    return out


# ---------------------------------------------------------------------------
# Series construction
# ---------------------------------------------------------------------------


def staking_apy_from_dfdvsol(rate_rows: list[dict], window_days: int = 90) -> float | None:
    """Annualised SOL staking yield implied by the dfdvSOL exchange rate."""
    rows = sorted(
        ((r["epoch_date"], float(r["dfdvsol_sol_rate"])) for r in rate_rows if r.get("dfdvsol_sol_rate")),
        key=lambda r: r[0],
    )
    if len(rows) < 2:
        return None
    end_d, end_r = rows[-1]
    cutoff = (dt.date.fromisoformat(end_d) - dt.timedelta(days=window_days)).isoformat()
    older = [r for r in rows if r[0] <= cutoff] or [rows[0]]
    start_d, start_r = older[-1]
    days = (dt.date.fromisoformat(end_d) - dt.date.fromisoformat(start_d)).days
    if days <= 0 or start_r <= 0:
        return None
    return ((end_r / start_r) ** (365.0 / days) - 1.0) * 100.0


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def build(dry: bool = False) -> dict:
    print("Fetching DFDV metrics API...")
    dfdv_now = dfdv_api("dfdv")
    nav_now = dfdv_api("nav")
    sol_now = dfdv_api("sol")
    history = sorted(dfdv_api("history"), key=lambda r: r["date"])
    shares_rows = sorted(dfdv_api("shares"), key=lambda r: r["date"])
    debt_rows = dfdv_api("debt")
    options_rows = dfdv_api("options")
    dfdvsol_rate = dfdv_api("dfdvsol/rate")

    print("Fetching Nasdaq (prices, short interest, holders)...")
    px = nasdaq_ohlcv("DFDV", "stocks", "2025-03-01")
    bsol = nasdaq_ohlcv(BSOL_TICKER, "etf", BSOL_INCEPTION)
    short_rows = nasdaq_short_interest("DFDV")
    summary = nasdaq_summary("DFDV")
    holders = nasdaq_holders("DFDV")

    print("Fetching Hyperliquid SOL funding...")
    funding = hyperliquid_funding(TREASURY_START)

    sa = stockanalysis_stats("DFDV")
    float_shares = int(sa["float"]) if sa.get("float") else None
    float_source = sa.get("source", "")
    if not float_shares:
        warn("public float unavailable; % of float falls back to % of shares outstanding")

    # --- daily inputs, forward-filled onto the trading calendar -------------
    sol_px = {r["date"]: r["sol_price"] for r in history if r.get("sol_price")}
    holdings_steps = [(r["date"], r["total_sol_holdings"]) for r in history if r.get("total_sol_holdings")]
    shares_steps = [(r["date"], r["shares_outstanding"]) for r in shares_rows]

    trading_days = [d for d in px if d >= TREASURY_START]
    if not trading_days:
        raise RuntimeError("no DFDV trading days after treasury start")

    # SOL trades every day; carry the last known print onto each trading day.
    sol_keys = sorted(sol_px)

    def sol_on(d: str):
        i = bisect_right(sol_keys, d) - 1
        return sol_px[sol_keys[i]] if i >= 0 else None

    staking_apy = staking_apy_from_dfdvsol(dfdvsol_rate)
    if staking_apy is None:
        staking_apy = 6.5
        warn("could not derive staking APY from dfdvSOL rate; using 6.5% default")
    # An ETF keeps the staking yield net of the manager's cut of rewards.
    etf_daily_yield = (staking_apy / 100.0) * (1 - ETF_STAKING_FEE) / 365.0
    etf_daily_fee = ETF_MGMT_FEE / 365.0

    dates: list[str] = []
    s_dfdv, s_sol, s_etf, s_bsol, s_perp = [], [], [], [], []
    s_lev, s_sps, s_mnav = [], [], []
    # Price decomposition. DFDV's share price satisfies an exact identity:
    #
    #     price = mNAV x SOL-per-share x SOL price
    #
    # because NAV per share is SPS x SOL price and mNAV is price / NAV per share. So the
    # return over any window factors cleanly into three multiplicative drivers - the SOL
    # market, treasury growth, and the multiple the market pays - with no modelling and no
    # leverage assumption. `navps` is the NAV-per-share leg (SPS x SOL price) in dollars.
    s_price, s_navps = [], []

    etf_idx = perp_idx = 100.0
    base_dfdv = px[trading_days[0]]["close"]
    base_sol = sol_on(trading_days[0])
    bsol_base = bsol_anchor = None
    prev = None  # (sol_price, sps, leverage)

    for d in trading_days:
        close = px[d]["close"]
        sp = sol_on(d)
        holdings = step_lookup(holdings_steps, d) or 0.0
        shares = step_lookup(shares_steps, d)
        if not sp or not shares:
            continue
        mktcap = close * shares
        sol_nav = holdings * sp
        sps = holdings / shares
        lev = sol_nav / mktcap if mktcap else 0.0
        navps = sol_nav / shares

        if prev is not None:
            p_sol, p_sps, p_lev = prev
            sol_r = sp / p_sol - 1.0
            f = funding.get(d, 0.0)

            etf_idx *= (1 + sol_r) * (1 + etf_daily_yield) * (1 - etf_daily_fee)
            perp_idx *= 1 + sol_r - f
        else:
            perp_idx *= 1 - PERP_TAKER_FEE  # one entry fee on the theoretical perp

        prev = (sp, sps, lev)

        dates.append(d)
        s_dfdv.append(round(close / base_dfdv * 100, 4))
        s_sol.append(round(sp / base_sol * 100, 4))
        s_etf.append(round(etf_idx, 4))
        s_perp.append(round(perp_idx, 4))
        s_price.append(round(close, 4))
        s_navps.append(round(navps, 4))
        s_lev.append(round(lev, 4))
        s_sps.append(round(sps, 8))
        s_mnav.append(round(mktcap / sol_nav, 4) if sol_nav else None)

        if d in bsol and bsol_base is None:
            # BSOL only lists from 2025-10-28. Splice it in at the synthetic ETF's index
            # level that day so the two lines are directly comparable from there on.
            bsol_base, bsol_anchor = bsol[d]["close"], s_etf[-1]

    s_bsol = [
        (round(bsol[d]["close"] / bsol_base * bsol_anchor, 4) if d in bsol else None)
        for d in dates
    ] if bsol_base else [None] * len(dates)

    # The expected-value band, in dollars per share rather than as an indexed return.
    #
    # Expected price = NAV per share x the multiple the market has recently been paying.
    # The edges take the 25th and 75th percentile of mNAV over a trailing 180 trading
    # days, so the band answers "where would DFDV trade if it were valued the way it has
    # lately been valued, given the treasury it now holds". Because both the band and the
    # price are absolute dollar levels, changing the chart's time range only pans and
    # zooms - it never redraws the band, which a rebased band would.
    BAND_WINDOW = 180
    band_lo, band_hi, band_pos = [], [], []
    for i, navps in enumerate(s_navps):
        window = [m for m in s_mnav[max(0, i - BAND_WINDOW + 1):i + 1] if m]
        if not window or not navps:
            band_lo.append(None)
            band_hi.append(None)
            band_pos.append(None)
            continue
        lo_m, hi_m = percentile(window, 0.25), percentile(window, 0.75)
        lo, hi = navps * lo_m, navps * hi_m
        band_lo.append(round(lo, 4))
        band_hi.append(round(hi, 4))
        # Where the actual price sits across the band: 0 = at the cheap edge, 1 = at the
        # rich edge. Equivalent to where today's mNAV falls in its own trailing range.
        band_pos.append(round((s_price[i] - lo) / (hi - lo), 4) if hi > lo else None)

    # Exact multiplicative decomposition of the return since the treasury began.
    def ratio(series):
        return (series[-1] / series[0]) if series and series[0] else None

    decomposition = {
        "sol": ratio(s_sol),
        "sps": ratio(s_sps),
        "mnav": ratio(s_mnav),
        "dfdv": ratio(s_dfdv),
        "product": None,
    }
    if all(decomposition[k] for k in ("sol", "sps", "mnav")):
        decomposition["product"] = decomposition["sol"] * decomposition["sps"] * decomposition["mnav"]
        decomposition["residual_pct"] = abs(decomposition["product"] - decomposition["dfdv"]) / decomposition["dfdv"] * 100

    lev_vals = [v for v in s_lev if v]
    leverage_stats = {
        "current": s_lev[-1] if s_lev else None,
        "min": min(lev_vals) if lev_vals else None,
        "max": max(lev_vals) if lev_vals else None,
        "median": percentile(lev_vals, 0.5) if lev_vals else None,
        "p25": percentile(lev_vals, 0.25) if lev_vals else None,
        "p75": percentile(lev_vals, 0.75) if lev_vals else None,
        "avg_90d": round(sum(s_lev[-90:]) / len(s_lev[-90:]), 4) if s_lev else None,
    }

    # --- headline returns off the DFDV close series ------------------------
    closes = {d: v["close"] for d, v in px.items()}
    keys = sorted(closes)
    last_d = keys[-1]
    last = closes[last_d]

    def ret_days(n: int):
        target = (dt.date.fromisoformat(last_d) - dt.timedelta(days=n)).isoformat()
        i = bisect_right(keys, target) - 1
        return pct_change(last, closes[keys[i]]) if i >= 0 else None

    def ret_from(date: str):
        i = bisect_right(keys, date) - 1
        return pct_change(last, closes[keys[i]]) if i >= 0 else None

    ytd_anchor = f"{dt.date.fromisoformat(last_d).year - 1}-12-31"
    returns = {
        "1d": pct_change(last, closes[keys[-2]]) if len(keys) > 1 else None,
        "1w": ret_days(7),
        "1m": ret_days(30),
        "3m": ret_days(91),
        "6m": ret_days(182),
        "ytd": ret_from(ytd_anchor),
        "1y": ret_days(365),
        "inception": ret_from(INCEPTION),
    }

    def index_ret(series: list, n_days: int | None = None, since: str | None = None):
        """Same windows, computed on one of the comparison index series."""
        if not series or series[-1] is None:
            return None
        if since:
            target = since
        else:
            target = (dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=n_days or 0)).isoformat()
        i = bisect_right(dates, target) - 1
        if i < 0:
            i = 0
        base = series[i]
        return pct_change(series[-1], base) if base else None

    windows = [("1d", 1), ("1w", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365)]
    compare = {}
    for label, series in (
        ("dfdv", s_dfdv), ("sol", s_sol), ("etf", s_etf), ("perp", s_perp),
        ("navps", s_navps), ("sps", s_sps), ("mnav", s_mnav),
    ):
        row = {k: index_ret(series, n) for k, n in windows}
        row["ytd"] = index_ret(series, since=ytd_anchor)
        row["treasury"] = index_ret(series, since=dates[0])
        compare[label] = row
    # BSOL only exists post-launch; report it from its own inception.
    bsol_vals = [(d, v) for d, v in zip(dates, s_bsol) if v is not None]
    if bsol_vals:
        compare["bsol"] = {k: index_ret(s_bsol, n) for k, n in windows}
        compare["bsol"]["ytd"] = index_ret(s_bsol, since=ytd_anchor)
        compare["bsol"]["treasury"] = None
        compare["bsol"]["since_launch"] = pct_change(bsol_vals[-1][1], bsol_vals[0][1])

    # --- short interest ----------------------------------------------------
    vols = sorted((d, v["volume"]) for d, v in px.items() if v["volume"])
    shares_now = step_lookup(shares_steps, last_d)
    float_used = float_shares or shares_now
    # Only the current float is published. For the history, hold the insider/locked-up
    # fraction constant and let float track the share count, so the trend is comparable.
    float_ratio = (float_used / shares_now) if shares_now else 1.0
    short_hist = []
    for r in short_rows:
        so = step_lookup(shares_steps, r["date"])
        short_hist.append(
            {
                "date": r["date"],
                "shares": r["shares"],
                "days_to_cover": r["days_to_cover"],
                "pct_float": round(r["shares"] / (so * float_ratio) * 100, 2) if so else None,
                "pct_shares_out": round(r["shares"] / so * 100, 2) if so else None,
            }
        )
    si_last = short_hist[-1] if short_hist else {}
    si_prev = short_hist[-2] if len(short_hist) > 1 else {}

    # --- options -----------------------------------------------------------
    opt = next((r for r in options_rows if r.get("symbol") == "DFDV"), {})
    opt_peers = [
        {
            "symbol": r.get("symbol"),
            "iv": r.get("implied_volatility"),
            "put_call_ratio": r.get("put_call_ratio"),
            "oi_musd": r.get("total_open_interest_millions"),
        }
        for r in options_rows
        if r.get("symbol") in ("DFDV", "MSTR")
    ]

    # --- debt --------------------------------------------------------------
    debt = [
        {
            "name": r.get("name"),
            "issue_date": r.get("issue_date"),
            "maturity_date": r.get("maturity_date"),
            "coupon_rate": r.get("coupon_rate"),
            "notional": r.get("notional_amount"),
            "conversion_price": r.get("conversion_price"),
        }
        for r in debt_rows
    ]
    total_debt = sum((r["notional"] or 0) for r in debt)

    mktcap = dfdv_now["market_cap"]
    sol_nav_now = dfdv_now["sol_nav"]
    net_debt = nav_now.get("net_debt_amount")
    cash = nav_now.get("total_cash_including_eloc")

    avg30 = sum(v for _, v in vols[-30:]) / max(len(vols[-30:]), 1)
    avg90 = sum(v for _, v in vols[-90:]) / max(len(vols[-90:]), 1)

    data = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": {
            "price_date": last_d,
            "dfdv_api": dfdv_now.get("updated_at"),
            "short_interest": si_last.get("date"),
            "holders": holders["as_of"],
        },
        "methodology": {
            "inception": INCEPTION,
            "treasury_start": TREASURY_START,
            "etf_mgmt_fee_pct": ETF_MGMT_FEE * 100,
            "etf_staking_fee_pct": ETF_STAKING_FEE * 100,
            "staking_apy_pct": round(staking_apy, 2),
            "perp_taker_fee_bps": PERP_TAKER_FEE * 10000,
            "bsol_inception": BSOL_INCEPTION,
        },
        "headline": {
            "price": dfdv_now["price"],
            "change_pct": dfdv_now.get("price_change_percent"),
            "market_cap": mktcap,
            "enterprise_value": dfdv_now.get("enterprise_value"),
            "shares_outstanding": dfdv_now.get("shares_outstanding"),
            "float_shares": float_used,
            "float_source": float_source or "shares outstanding (float unavailable)",
            "insider_pct": sa.get("sharesInsiders"),
            "institutional_pct": holders["institutional_ownership_pct"],
            "volume": dfdv_now.get("volume"),
            "avg_volume_30d": round(avg30),
            "avg_volume_90d": round(avg90),
            "week52_high_low": summary.get("FiftTwoWeekHighLow"),
            "analyst_target": money(summary.get("OneYrTarget")),
            "sol_price": dfdv_now["sol_price"],
            "sol_count": dfdv_now["sol_count"],
            "sol_nav": sol_nav_now,
            "sps": round(dfdv_now["sol_count"] / dfdv_now["shares_outstanding"], 8),
            "nav_per_share": nav_now.get("headline_nav_per_share"),
            "net_nav_per_share": nav_now.get("adjusted_nav_per_share"),
            "mnav": nav_now.get("headline_mnav"),
            "mnav_fully_diluted": nav_now.get("fully_diluted_mnav"),
            "leverage": round(sol_nav_now / mktcap, 4) if mktcap else None,
            "debt_to_mktcap": round(total_debt / mktcap, 4) if mktcap else None,
            "net_debt": net_debt,
            "total_debt": total_debt,
            "cash": cash,
            "implied_volatility": dfdv_now.get("implied_volatility"),
            "historic_volatility": dfdv_now.get("historic_volatility"),
            "options_oi_musd": opt.get("total_open_interest_millions"),
            "put_call_ratio": opt.get("put_call_ratio"),
            "options_avg_duration_days": opt.get("avg_duration_days"),
            "staking_apy_pct": round(staking_apy, 2),
            "sol_gain_ytd": sol_now.get("sol_gain_ytd"),
            "sol_gain_3m": sol_now.get("sol_gain_3m"),
            "short": {
                "shares": si_last.get("shares"),
                "date": si_last.get("date"),
                "pct_float": si_last.get("pct_float"),
                "pct_shares_out": si_last.get("pct_shares_out"),
                "days_to_cover": si_last.get("days_to_cover"),
                "prev_shares": si_prev.get("shares"),
                "change_pct": pct_change(si_last.get("shares"), si_prev.get("shares")),
            },
        },
        "returns": returns,
        "compare": compare,
        "leverage_stats": leverage_stats,
        "band_position": band_pos[-1] if band_pos else None,
        "decomposition": decomposition,
        "series": {
            "dates": dates,
            "dfdv": s_dfdv,
            "sol": s_sol,
            "etf": s_etf,
            "bsol": s_bsol,
            "perp": s_perp,
            "price": s_price,      # DFDV close, dollars
            "navps": s_navps,      # NAV per share = SPS x SOL price, dollars
            "band_lo": band_lo,    # NAV/share x trailing-180d mNAV p25, dollars
            "band_hi": band_hi,    # NAV/share x trailing-180d mNAV p75, dollars
            "band_pos": band_pos,
            "leverage": s_lev,
            "sps": s_sps,
            "mnav": s_mnav,
            "nav_per_share": s_navps,
        },
        "short_history": short_hist,
        "options_peers": opt_peers,
        "debt": debt,
        "ownership": holders,
        "warnings": WARNINGS,
    }
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch and summarise, write nothing")
    args = ap.parse_args()

    data = build(dry=args.dry_run)
    h = data["headline"]
    r = data["returns"]
    print("\n--- State of DFDV ---")
    print(f"  price ${h['price']:.2f}  mktcap ${h['market_cap']/1e6:.1f}M  lev {h['leverage']}x  mNAV {h['mnav']:.3f}")
    print(f"  SOL {h['sol_count']:,} (${h['sol_nav']/1e6:.1f}M)  SPS {h['sps']:.5f}  staking {h['staking_apy_pct']}%")
    print(f"  short {h['short']['shares']:,} = {h['short']['pct_float']}% float, {h['short']['days_to_cover']} d2c")
    print("  returns: " + "  ".join(f"{k}={v:.1f}%" for k, v in r.items() if v is not None))
    print(f"  series: {len(data['series']['dates'])} days {data['series']['dates'][0]} -> {data['series']['dates'][-1]}")
    print(f"  holders: {len(data['ownership']['holders'])}, inst {data['ownership']['institutional_ownership_pct']}%")
    c = data["compare"]
    print("  since treasury: " + "  ".join(
        f"{k}={c[k]['treasury']:.0f}%" for k in ("dfdv", "sol", "etf", "perp")
        if c[k].get("treasury") is not None))
    dc = data["decomposition"]
    print(f"  decomposition: SOL {dc['sol']:.3f}x  x  SPS {dc['sps']:.3f}x  x  mNAV {dc['mnav']:.4f}x"
          f"  =  {dc['product']:.4f}x   (actual {dc['dfdv']:.4f}x, residual {dc['residual_pct']:.3f}%)")
    print(f"  band position: {data['band_position']:.2f}  leverage {data['leverage_stats']}")
    if data["warnings"]:
        print(f"  warnings: {len(data['warnings'])}")

    if args.dry_run:
        print("\n(dry run - data.json not written)")
        return 0
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
