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

None of these send CORS headers, so the browser cannot call them directly. That is
why the data is fetched server-side into `data.json` rather than live from the page.

## Methodology

The page has a "Methodology & sources" panel that documents every construction.
The three that involve modelling choices:

**Expected-performance band.** Both edges chain daily returns from 2025-04-11, the
first SOL purchase. The floor is `L[t-1] × SOL return[t]` — pure levered SOL beta,
where `L` is SOL NAV ÷ market cap. The ceiling adds the SPS-growth term from
[The SOL Boost](https://defidevcorp.beehiiv.com/p/the-sol-boost):
`L[t-1] × SOL return[t] + SPS growth[t]`. The truth is between them, because SPS
growth funded by debt raises assets and liabilities together and is not accretive
to equity at the moment it happens. Separating accretive from non-accretive SPS
growth would need a daily net-debt series DFDV does not publish, so the page shows
the range instead of picking a point inside it.

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
