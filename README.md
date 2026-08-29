# State of DFDV

A single-page dashboard on DeFi Development Corp (Nasdaq: DFDV) for current and
prospective investors: headline performance, treasury and leverage, market
structure, a performance comparison against the other ways to hold SOL, and an
expected-performance band derived from DFDV's own leverage.

## Files

| File | What it is |
|---|---|
| `index.html` | The whole dashboard. Self-contained — no build step, no dependencies, charts are hand-rolled SVG. |
| `data.json` | Everything the page renders. Built by the refresh script. |
| `refresh_data.py` | Fetches every source and writes `data.json`. Stdlib only (plus `certifi` on macOS). |
| `.github/workflows/refresh.yml` | Runs the refresh on a schedule and commits the result. |

## Run it locally

`index.html` fetches `data.json`, which browsers block on `file://` URLs, so serve
the folder:

```bash
cd ~/state-of-dfdv && python3 -m http.server 4173
```

Then open <http://localhost:4173>.

## Refresh the data

```bash
python3 refresh_data.py --dry-run
```

That fetches everything and prints a summary without writing. Drop `--dry-run` to
write `data.json`. It takes about a minute, most of it paging Hyperliquid funding
history.

```bash
python3 -m pip install certifi
```

Only needed on macOS, where the system Python otherwise fails TLS verification.

## Where the data comes from

No API keys are required for any of it.

- **`defidevcorp.com/api/dashboard/*`** — DFDV's own public metrics API, the same
  feed behind dfdv.com. Supplies daily SOL price and holdings, share count
  history, debt tranches, options open interest, and the dfdvSOL exchange rate.
- **`api.nasdaq.com`** — DFDV and BSOL daily OHLCV, FINRA semi-monthly short
  interest, days to cover, and 13F institutional holders.
- **`api.hyperliquid.xyz`** — hourly realised SOL perpetual funding, paged back to
  the treasury start date.
- **`stockanalysis.com`** — public float and insider ownership percentage.
- **`data.sec.gov` / `www.sec.gov/Archives`** — the EDGAR filing feed for CIK
  1805526 (the Latest Filings section). SEC's fair-access policy requires a
  declared `User-Agent` carrying a contact address; requests without one get a
  403. `EDGAR_UA` in `refresh_data.py` holds it — change the address there if you
  would rather use a role account than a personal one.

### Staking yield

DFDV's filings discuss staking reward revenue but publish **no headline yield
percentage**, and there is no XBRL tag for one, so nothing can be read out of the
latest filing automatically. `REPORTED_STAKING_YIELD_PCT` at the top of
`refresh_data.py` is the override: set it to the figure the company reports and the
tile uses it verbatim and cites the filing it came from. Left as `None`, the tile
falls back to the yield implied by the dfdvSOL exchange rate and says so on its face
rather than implying the number was filed.

None of these send CORS headers, so the browser cannot call them directly. That is
why the data is fetched server-side into `data.json` rather than live from the page.

## Methodology

The page has a "Methodology & sources" panel that documents every construction.
The three that involve modelling choices:

**Return decomposition.** NAV per share is `SPS × SOL price` and mNAV is
`price ÷ NAV per share`, so the share price satisfies an exact identity:

```
price = mNAV × SOL per share × SOL price
```

The return over any window therefore factors into three multiplicative drivers with
nothing estimated. Since the first purchase: `0.955× × 9.071× × 0.0785× = 0.6807×`
against an actual `0.6807×` — a 0.007% residual, which is rounding.

> An earlier version of this page chained `leverage × SOL return + SPS growth`,
> following the additive framing in
> [The SOL Boost](https://defidevcorp.beehiiv.com/p/the-sol-boost). That was wrong.
> Levered SOL exposure is `SOL NAV ÷ market cap`, which already rises when debt buys
> more SOL — so adding the SPS-growth term counts the same debt-funded SOL twice.
> The identity above needs no leverage assumption at all.

**Valuation band.** `NAV per share × the 25th and 75th percentile of mNAV over the
trailing 180 trading days` — where DFDV would trade if the market paid the multiple
it has lately been paying. Drawn in dollars per share, not as an indexed return, so
changing the chart's range pans and zooms it rather than redrawing it.

**On "leverage".** Two numbers travel under that word, and neither is a price
multiplier on its own. `SOL NAV ÷ market cap` (~1.59×) is SOL exposure per dollar of
market value. `SOL NAV ÷ (SOL NAV − net debt)` (~5.18×) is the true gearing on book
equity. Which one moves the share price depends on whether the market re-rates —
hold mNAV fixed and the elasticity to SOL is exactly 1.0, since price is linear in
the SOL price at a fixed multiple. That is why the page decomposes the return rather
than asserting a multiplier.

**Synthetic SOL ETF.** SOL spot compounded with the staking yield implied by the
dfdvSOL exchange rate over the trailing 90 days, less a 0.20% sponsor fee and a 6%
manager cut of staking rewards — BSOL's published post-waiver schedule. It spans
the whole window because no spot SOL ETF existed before 2025-10-28. Actual BSOL is
spliced in from its launch at the synthetic line's level that day; the two have
tracked within about a point, which is the check that the model behaves.

**Theoretical perp.** A 1× long SOL perpetual rebalanced daily to constant
notional, paying each day's realised Hyperliquid funding, less a one-off 4.5bp
taker fee at entry.

Inception is the **2025-04-04** close of $0.5714 — the day the board adopted the
treasury policy, three days before it was announced. DFDV's own `sse_return` uses
the same base, so this dashboard and dfdv.com agree.

### A note on the leverage definition

dfdv.com displays "Levered SOL exposure 1.62×" with the caption
`1.2 × Debt / Market Cap`, but that formula does not produce that number from the
API's own fields. The figure is SOL NAV ÷ market cap. This dashboard computes it
that way and labels it as such, and separately shows Debt ÷ Market Cap, which is
the definition used in The SOL Boost. The two measure different things — exposure
versus borrowing — and are currently about 1.60× and 1.30×.

## Deploying

The repo is arranged for GitHub Pages serving from the repository root.

1. Create the remote and push:
   ```bash
   gh repo create state-of-dfdv --public --source=. --remote=origin --push
   ```
2. In the repository settings, enable Pages with source "Deploy from a branch",
   branch `main`, folder `/ (root)`.
3. For a custom domain, add a `CNAME` file containing the hostname and point a
   DNS CNAME at `<user>.github.io`.

`.github/workflows/refresh.yml` reruns the fetch on a schedule and commits
`data.json` when it changes. It needs no secrets. Give the workflow write access
under Settings → Actions → General → Workflow permissions.

## Disclaimer

Not investment advice. This is an assembled view of public data, not an offer or
recommendation, and it may differ from DFDV's SEC filings, which govern. The
synthetic ETF, perpetual and expected-band series are illustrative models, not
forecasts.
