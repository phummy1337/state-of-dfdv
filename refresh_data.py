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
from zoneinfo import ZoneInfo

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

# Staking yield. DFDV's SEC filings discuss staking reward revenue but publish no
# headline yield percentage, and there is no XBRL tag for one - so there is nothing to
# read out of the most recent filing automatically. Set this to the figure the company
# reports and it is used verbatim and cited; leave it None and the page falls back to
# the yield implied by the dfdvSOL exchange rate, labelled as derived rather than filed.
REPORTED_STAKING_YIELD_PCT = None

ETF_MGMT_FEE = 0.0020  # BSOL headline sponsor fee, 20bps
ETF_STAKING_FEE = 0.06  # BSOL's cut of staking rewards after the launch waiver
PERP_TAKER_FEE = 0.00045  # Hyperliquid taker fee, charged once at entry
BSOL_TICKER = "BSOL"
BSOL_INCEPTION = "2025-10-28"
# ProShares Ultra Solana - 2x daily levered SOL. Listed 2025-07-15.
SLON_TICKER = "SLON"
SLON_INCEPTION = "2025-07-15"
# SLON's gross expense ratio. Recorded for disclosure only: SLON is a real traded fund,
# so its market price is already net of fees and no adjustment is applied (or wanted).
SLON_EXPENSE_RATIO = 2.14

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

# SEC's fair-access policy requires a declared User-Agent carrying a contact address;
# requests without one get a 403 from both data.sec.gov and www.sec.gov/Archives.
# Same string the crypto-treasury-dashboard scraper uses.
EDGAR_UA = {"User-Agent": "state-of-dfdv pete@defidevcorp.com"}
DFDV_CIK = "0001805526"
FILINGS_KEPT = 60

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


BEEHIIV_HOME = "https://defidevcorp.beehiiv.com/"


def beehiiv_posts(limit: int = 12) -> list[dict]:
    """Recent newsletter posts. beehiiv exposes no RSS on this publication, but its
    homepage embeds the post records as raw JSON, which is what we read."""
    html = _get(BEEHIIV_HOME, PLAIN_UA, timeout=45).decode("utf-8", "ignore")
    out = []
    for m in re.finditer(r'\{"id":"[0-9a-f-]{36}","publication_id":"[0-9a-f-]{36}"', html):
        start = m.start()
        depth = 0
        for i in range(start, min(len(html), start + 60000)):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        rec = json.loads(html[start:i + 1])
                    except ValueError:
                        rec = None
                    break
        else:
            rec = None
        if not rec or rec.get("status") != "published" or not rec.get("slug"):
            continue
        date = (rec.get("override_scheduled_at") or rec.get("created_at") or "")[:10]
        out.append({
            "title": rec.get("web_title"),
            "subtitle": rec.get("web_subtitle"),
            "slug": rec["slug"],
            "url": f"{BEEHIIV_HOME}p/{rec['slug']}",
            "date": date or None,
            "reading_time": rec.get("estimated_reading_time"),
        })
    # De-duplicate on slug, newest first.
    seen, uniq = set(), []
    for p in sorted(out, key=lambda r: r["date"] or "", reverse=True):
        if p["slug"] in seen:
            continue
        seen.add(p["slug"])
        uniq.append(p)
    return uniq[:limit]


FORM_LABELS = {
    "10-K": "Annual report", "10-Q": "Quarterly report", "8-K": "Current report",
    "4": "Insider transaction", "3": "Initial insider statement", "5": "Annual insider statement",
    "S-1": "Registration statement", "S-1/A": "Registration statement (amended)",
    "S-3": "Shelf registration", "S-3/A": "Shelf registration (amended)",
    "424B5": "Prospectus supplement", "424B3": "Prospectus supplement",
    "DEF 14A": "Proxy statement", "DEF 14C": "Information statement",
    "PRE 14C": "Information statement (preliminary)", "SC 13D": "Beneficial ownership",
    "SC 13G": "Beneficial ownership", "EFFECT": "Registration effective",
    "CORRESP": "Correspondence", "UPLOAD": "SEC staff letter", "144": "Proposed sale",
}


def sec_filings(cik: str = DFDV_CIK, keep: int = FILINGS_KEPT) -> dict:
    """Recent EDGAR filings: date, form, what it is, and links to the document."""
    data = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", EDGAR_UA)
    recent = data.get("filings", {}).get("recent", {})
    n = len(recent.get("form", []))
    bare = str(int(cik))
    out = []
    for i in range(min(n, keep)):
        acc = recent["accessionNumber"][i]
        acc_flat = acc.replace("-", "")
        doc = recent["primaryDocument"][i] or ""
        form = recent["form"][i]
        desc = (recent.get("primaryDocDescription") or [""] * n)[i] or ""
        items = (recent.get("items") or [""] * n)[i] or ""
        base = f"https://www.sec.gov/Archives/edgar/data/{bare}/{acc_flat}"
        out.append({
            "date": recent["filingDate"][i],
            "report_date": (recent.get("reportDate") or [""] * n)[i] or None,
            "form": form,
            # primaryDocDescription often just repeats the form; prefer a plain-English label.
            "label": FORM_LABELS.get(form) or (desc if desc and desc != form else "Filing"),
            "items": items,
            "url": f"{base}/{doc}" if doc else f"{base}/{acc}-index.htm",
            "index_url": f"{base}/{acc}-index.htm",
        })
    latest = {}
    for f in out:
        latest.setdefault(f["form"], f)
    return {
        "entity": data.get("name"),
        "cik": cik,
        "filings": out,
        "latest_by_form": {k: latest[k] for k in ("10-K", "10-Q", "8-K") if k in latest},
    }


def coinbase_sol_at_us_close(start_iso: str) -> dict[str, float]:
    """{iso date: SOL/USD at the 4pm ET equity close}, from Coinbase hourly candles.

    This exists because of a real bug. DFDV's API publishes one SOL price per day on a
    UTC clock, so comparing it against a 4pm ET equity close mixes timestamps roughly a
    day apart. Regressed on that series a plain 1x spot SOL ETF (BSOL) showed a beta of
    0.26 and a correlation of 0.23 - both impossible for a fund that holds the asset -
    and every return comparison and beta on this page inherited the error. Sampling SOL
    at the instant the equity market closes fixes it.

    Coinbase candle buckets are labelled by their start, so the price *at* 16:00 ET is
    the close of the bucket starting 15:00 ET.
    """
    out: dict[str, float] = {}
    et_zone = ZoneInfo("America/New_York")
    cursor = dt.datetime.fromisoformat(start_iso).replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    step = dt.timedelta(hours=300)
    guard = 0
    while cursor < now and guard < 120:
        guard += 1
        end = min(cursor + step, now)
        url = ("https://api.exchange.coinbase.com/products/SOL-USD/candles?granularity=3600"
               f"&start={cursor.strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        try:
            rows = get_json(url, {"User-Agent": "state-of-dfdv"}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            warn(f"Coinbase candles failed near {cursor:%Y-%m-%d} ({exc})")
            cursor = end
            continue
        for c in rows:
            et = dt.datetime.fromtimestamp(c[0], dt.timezone.utc).astimezone(et_zone)
            if et.hour == 15:
                out[et.date().isoformat()] = float(c[4])
        cursor = end
        time.sleep(0.16)  # stay inside Coinbase's public rate limit
    return out


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
    slon = nasdaq_ohlcv(SLON_TICKER, "etf", SLON_INCEPTION)
    short_rows = nasdaq_short_interest("DFDV")
    summary = nasdaq_summary("DFDV")
    holders = nasdaq_holders("DFDV")

    print("Fetching Coinbase SOL at the US close...")
    try:
        sol_et = coinbase_sol_at_us_close(TREASURY_START)
        if len(sol_et) < 200:
            warn(f"only {len(sol_et)} ET-close SOL prints; falling back to the daily UTC series")
            sol_et = {}
    except Exception as exc:  # noqa: BLE001
        warn(f"Coinbase SOL fetch failed ({exc}); falling back to the daily UTC series")
        sol_et = {}

    print("Fetching Hyperliquid SOL funding...")
    funding = hyperliquid_funding(TREASURY_START)

    print("Fetching SEC filings...")
    try:
        filings = sec_filings()
    except Exception as exc:  # noqa: BLE001 - the page degrades to no filings list
        warn(f"SEC filings fetch failed ({exc}); filings section will be empty")
        filings = {"entity": None, "cik": DFDV_CIK, "filings": [], "latest_by_form": {}}

    print("Fetching beehiiv posts...")
    try:
        posts = beehiiv_posts()
        if not posts:
            warn("beehiiv returned no published posts; the marquee will be empty")
    except Exception as exc:  # noqa: BLE001 - the marquee degrades to nothing
        warn(f"beehiiv fetch failed ({exc}); the marquee will be empty")
        posts = []

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
        # Prefer the 4pm ET print so SOL and the equity close are the same instant; the
        # daily UTC series is only a fallback when Coinbase is unavailable.
        if d in sol_et:
            return sol_et[d]
        i = bisect_right(sol_keys, d) - 1
        return sol_px[sol_keys[i]] if i >= 0 else None

    latest_report = filings["latest_by_form"].get("10-Q") or filings["latest_by_form"].get("10-K")
    if REPORTED_STAKING_YIELD_PCT is not None:
        staking_apy = REPORTED_STAKING_YIELD_PCT
        staking_source = (f"per {latest_report['form']}, {latest_report['date']}"
                          if latest_report else "as reported")
        staking_source_url = latest_report["url"] if latest_report else None
    else:
        staking_apy = staking_apy_from_dfdvsol(dfdvsol_rate)
        if staking_apy is None:
            staking_apy = 6.5
            warn("could not derive staking APY from dfdvSOL rate; using 6.5% default")
        staking_source = "dfdvSOL-implied; not in filings"
        staking_source_url = latest_report["url"] if latest_report else None
    # An ETF keeps the staking yield net of the manager's cut of rewards.
    etf_daily_yield = (staking_apy / 100.0) * (1 - ETF_STAKING_FEE) / 365.0
    etf_daily_fee = ETF_MGMT_FEE / 365.0

    dates: list[str] = []
    s_dfdv, s_sol, s_etf, s_bsol, s_perp = [], [], [], [], []
    s_lev, s_sps, s_mnav = [], [], []
    # DFDV's close and its NAV per share (SPS x SOL price), both in dollars.
    s_price, s_navps = [], []

    etf_idx = perp_idx = 100.0
    base_dfdv = px[trading_days[0]]["close"]
    base_sol = sol_on(trading_days[0])
    bsol_base = bsol_anchor = None
    slon_base = slon_anchor = None
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

        if d in slon and slon_base is None:
            # 2x wrapper on the same asset, so anchor it to SOL spot at listing.
            slon_base, slon_anchor = slon[d]["close"], s_sol[-1]
        if d in bsol and bsol_base is None:
            # BSOL only lists from 2025-10-28. Splice it in at the synthetic ETF's index
            # level that day so the two lines are directly comparable from there on.
            bsol_base, bsol_anchor = bsol[d]["close"], s_etf[-1]

    s_bsol = [
        (round(bsol[d]["close"] / bsol_base * bsol_anchor, 4) if d in bsol else None)
        for d in dates
    ] if bsol_base else [None] * len(dates)

    s_slon = [
        (round(slon[d]["close"] / slon_base * slon_anchor, 4) if d in slon else None)
        for d in dates
    ] if slon_base else [None] * len(dates)

    # The expected-performance band, built from DFDV's own historical leverage.
    #
    # Each edge chains `beta x SOL return` at a constant beta taken from the observed
    # distribution of DFDV's levered SOL exposure (SOL NAV / market cap): the 25th and
    # 75th percentile for the edges, the median for the centre line. The band therefore
    # reads "where a constant beta-times-SOL exposure would have put DFDV, across the
    # range of leverage DFDV has actually run".
    #
    # There is deliberately no SOL-per-share term. Adding one would double-count, because
    # beta is measured on SOL NAV, which already grows when debt or issuance buys more
    # SOL. The cost of leaving it out is that this is a leverage benchmark and not a fair
    # value: DFDV above the band can reflect treasury growth rather than richness.
    lev_obs = [v for v in s_lev if v]
    beta_lo = percentile(lev_obs, 0.25) if lev_obs else 1.0
    beta_mid = percentile(lev_obs, 0.50) if lev_obs else 1.0
    beta_hi = percentile(lev_obs, 0.75) if lev_obs else 1.0

    def beta_path(beta):
        idx, out = 100.0, [100.0]
        for i in range(1, len(s_sol)):
            prev = s_sol[i - 1]
            r = (s_sol[i] / prev - 1.0) if prev else 0.0
            idx *= max(1 + beta * r, 1e-6)
            out.append(round(idx, 4))
        return out

    path_lo, path_mid, path_hi = beta_path(beta_lo), beta_path(beta_mid), beta_path(beta_hi)
    # A higher beta ends lower when SOL falls, so the two edges cross; fill between the
    # pointwise extremes rather than assuming one is always above the other.
    band_lo = [round(min(x, y), 4) for x, y in zip(path_lo, path_hi)]
    band_hi = [round(max(x, y), 4) for x, y in zip(path_lo, path_hi)]

    from math import log
    band_pos = []
    for a_, lo_, hi_ in zip(s_dfdv, band_lo, band_hi):
        try:
            span = log(hi_) - log(lo_)
            band_pos.append(round((log(a_) - log(lo_)) / span, 4) if abs(span) > 1e-9 else None)
        except ValueError:
            band_pos.append(None)

    # Realised beta to SOL: OLS slope of daily returns on SOL's daily returns.
    def realised_beta(series, window=None):
        rd, rs = [], []
        rng = range(1, len(series)) if window is None else range(max(1, len(series) - window), len(series))
        for i in rng:
            if not series[i] or not series[i - 1] or not s_sol[i] or not s_sol[i - 1]:
                continue
            rd.append(series[i] / series[i - 1] - 1.0)
            rs.append(s_sol[i] / s_sol[i - 1] - 1.0)
        n = len(rs)
        if n < 20:
            return None
        ms, md = sum(rs) / n, sum(rd) / n
        var = sum((x - ms) ** 2 for x in rs)
        if var <= 0:
            return None
        return sum((rs[i] - ms) * (rd[i] - md) for i in range(n)) / var

    beta_stats = {
        "all": realised_beta(s_dfdv),
        "d90": realised_beta(s_dfdv, 90),
        "d30": realised_beta(s_dfdv, 30),
        # Sanity check: a 1x spot SOL ETF must regress near 1.0 against the SOL series.
        # If this drifts far from 1, the two price series are misaligned in time again.
        "bsol_check": realised_beta(s_bsol),
    }

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
        ("slon", s_slon), ("band_lo", band_lo), ("band_hi", band_hi), ("band_mid", path_mid),
    ):
        row = {k: index_ret(series, n) for k, n in windows}
        row["ytd"] = index_ret(series, since=ytd_anchor)
        row["treasury"] = index_ret(series, since=dates[0])
        compare[label] = row
    # BSOL only exists post-launch; report it from its own inception.
    for key, series in (("bsol", s_bsol), ("slon", s_slon)):
        vals = [v for v in series if v is not None]
        if not vals:
            continue
        compare[key] = {k: index_ret(series, n) for k, n in windows}
        compare[key]["ytd"] = index_ret(series, since=ytd_anchor)
        compare[key]["treasury"] = None
        compare[key]["since_launch"] = pct_change(vals[-1], vals[0])

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

    net_debt = nav_now.get("net_debt_amount")
    cash = nav_now.get("total_cash_including_eloc")

    # Derive the headline off the same series the charts use, not DFDV's API price
    # fields. Their `price` lagged the tape by two sessions ($5.28 against a $5.38
    # close) and their `price_change_percent` disagreed with it, so the hero card and
    # the return tiles told different stories about the same day.
    last_close = closes[last_d]
    shares_now_hd = step_lookup(shares_steps, last_d) or dfdv_now["shares_outstanding"]
    holdings_now = step_lookup(holdings_steps, last_d) or dfdv_now["sol_count"]
    sol_px_now = sol_on(last_d) or dfdv_now["sol_price"]
    mktcap = last_close * shares_now_hd
    sol_nav_now = holdings_now * sol_px_now
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
            "sol_price_basis": ("Coinbase SOL-USD at the 4pm ET close" if sol_et
                                else "daily UTC close (fallback - returns will be time-misaligned)"),
            "slon_expense_ratio_pct": SLON_EXPENSE_RATIO,
            "bsol_inception": BSOL_INCEPTION,
        "slon_inception": SLON_INCEPTION,
        },
        "headline": {
            "price": round(last_close, 4),
            "change_pct": returns["1d"],
            "market_cap": round(mktcap),
            "enterprise_value": round(mktcap + (net_debt or 0)),
            "shares_outstanding": shares_now_hd,
            "float_shares": float_used,
            "float_source": float_source or "shares outstanding (float unavailable)",
            "insider_pct": sa.get("sharesInsiders"),
            "institutional_pct": holders["institutional_ownership_pct"],
            "volume": dfdv_now.get("volume"),
            "avg_volume_30d": round(avg30),
            "avg_volume_90d": round(avg90),
            "week52_high_low": summary.get("FiftTwoWeekHighLow"),
            "analyst_target": money(summary.get("OneYrTarget")),
            "sol_price": round(sol_px_now, 4),
            "sol_count": holdings_now,
            "sol_nav": round(sol_nav_now),
            "sps": round(holdings_now / shares_now_hd, 8),
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
            "staking_apy_source": staking_source,
            "staking_apy_source_url": staking_source_url,
            "latest_report": latest_report,
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
        "band_betas": {"lo": round(beta_lo, 3), "mid": round(beta_mid, 3), "hi": round(beta_hi, 3)},
        "beta_stats": beta_stats,
        "series": {
            "dates": dates,
            "dfdv": s_dfdv,
            "sol": s_sol,
            "etf": s_etf,
            "bsol": s_bsol,
            "perp": s_perp,
            "slon": s_slon,        # 2x levered SOL ETF, spliced onto SOL spot at listing
            "price": s_price,      # DFDV close, dollars
            "band_lo": band_lo,    # constant-beta SOL path, lower edge
            "band_hi": band_hi,    # constant-beta SOL path, upper edge
            "band_mid": path_mid,  # constant-beta SOL path at median leverage
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
        "filings": filings,
        "posts": posts,
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
    print(f"  price ${h['price']:.2f} ({h['change_pct']:+.1f}%)  mktcap ${h['market_cap']/1e6:.1f}M  lev {h['leverage']}x  mNAV {h['mnav']:.3f}")
    print(f"  SOL {h['sol_count']:,} (${h['sol_nav']/1e6:.1f}M)  SPS {h['sps']:.5f}  staking {h['staking_apy_pct']}%")
    print(f"  short {h['short']['shares']:,} = {h['short']['pct_float']}% float, {h['short']['days_to_cover']} d2c")
    print("  returns: " + "  ".join(f"{k}={v:.1f}%" for k, v in r.items() if v is not None))
    print(f"  series: {len(data['series']['dates'])} days {data['series']['dates'][0]} -> {data['series']['dates'][-1]}")
    print(f"  holders: {len(data['ownership']['holders'])}, inst {data['ownership']['institutional_ownership_pct']}%")
    c = data["compare"]
    print("  since treasury: " + "  ".join(
        f"{k}={c[k]['treasury']:.0f}%" for k in ("dfdv", "sol", "etf", "perp")
        if c[k].get("treasury") is not None))
    print(f"  band position: {data['band_position']:.2f}  leverage {data['leverage_stats']}")
    b = data["beta_stats"]
    print(f"  beta to SOL: 30d={b['d30']:.2f}  90d={b['d90']:.2f}  all={b['all']:.2f}"
          f"   BSOL sanity (want ~1.0)={b['bsol_check'] if b['bsol_check'] is None else round(b['bsol_check'], 2)}")
    print(f"  SOL basis: {data['methodology']['sol_price_basis']}")
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
