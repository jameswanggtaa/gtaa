# MOAT + Nasdaq-100 Dip-Buying Strategy - Complete Specification
**Version:** 1.0  
**Date:** 2026-09-19  
**File:** MOAT_NDX_STRATEGY_SPEC.md

---

## 1. Strategy Overview

A buy-and-hold dip-buying strategy targeting high-quality "MOAT" stocks from the
Nasdaq-100 universe. The strategy buys great businesses at a discount (dip from
52-week high), accumulates in tranches as price falls further, holds while the
stock is in an uptrend (above 9-month SMA), and sells only when a confirmed
downtrend emerges. Cash earns money market rates between deployments.

---

## 2. Universe & Filters

### 2.1 Base Universe
- **Before 2012-01-01:** Nasdaq-100 (`NDX Index`) constituents only
  (MOAT index `MWMFTR Index` not yet available)
- **From 2012-01-01 onward:** Intersection of:
  - Morningstar Wide Moat Focus Index (`MWMFTR Index`)
  - Nasdaq-100 (`NDX Index`)

### 2.2 Hard Filters (applied monthly, all must pass)
| Filter | Threshold |
|--------|-----------|
| Market cap | ≥ $10 billion |
| Stock price | ≥ $10 (stocks ≥ $100 flagged as "preferred") |

### 2.3 Primary Screen - Option B: Quality + Growth Composite
Applied after hard filters. Selects **top 40 stocks** by composite score.

**Eligibility thresholds (all must pass):**
| Metric | Bloomberg Field | Minimum |
|--------|----------------|---------|
| Revenue growth TTM | `SALES_GROWTH` | > 8% |
| Gross margin | `GROSS_MARGIN` | > 40% |
| Return on Equity | `RETURN_ON_EQUITY` | > 15% |
| Debt / Equity | `TOT_DEBT_TO_TOT_EQY` | < 1.5 |

**Composite ranking score (higher = better):**
```
Score = 0.30 × Revenue_Growth_TTM
      + 0.30 × Gross_Margin
      + 0.20 × ROE
      + 0.20 × EPS_Growth_TTM
```
→ Rank all eligible stocks by Score descending, take **top 40**.

### 2.4 Secondary Screen - Option C: Analyst Consensus
Used as a **secondary strategy** for comparison. Selects **top 40 stocks**.

**Eligibility thresholds (all must pass):**
| Metric | Bloomberg Field | Threshold |
|--------|----------------|-----------|
| Consensus rating | `TOT_ANALYST_REC` | ≤ 2.0 (1=Strong Buy) |
| Forward EPS growth | `BEST_EPS_GROWTH` | > 10% |

→ Rank by consensus rating ascending (lower = stronger buy), take **top 40**.

### 2.5 Universe Refresh
- Refreshed **monthly** (first trading day of each month)
- Stocks that drop out of the universe mid-hold are **not force-sold**
  (existing positions held until sell rule triggers)
- New qualifying stocks added to watchlist immediately

---

## 3. Capital Structure

| Item | Value |
|------|-------|
| Starting capital | $500,000 |
| Annual injection | +$50,000 on January 1st each year |
| Idle cash return | Fed Funds rate (`FEDL01 Index`) daily accrual |
| Per-stock cost cap | $20,000 per dip cycle (see Section 5) |
| Sold proceeds | Recycled immediately into available cash pool |

---

## 4. Peak Definition

- **52-week rolling high:** Highest closing price over the trailing **252 trading days**
- Recalculated daily for every stock in the universe
- Used as the reference point for all dip % calculations

---

## 5. Buy Rules - Dip Tranches

### 5.1 Tranche Schedule
Triggered when `(current_price / 52_week_high - 1)` crosses each threshold:

| Dip from 52-wk High | Tranche Amount | Cumulative Spent |
|---------------------|---------------|-----------------|
| ≥ -20% | $2,000 | $2,000 |
| ≥ -30% | $3,000 | $5,000 |
| ≥ -40% | $5,000 | $10,000 |
| ≥ -50% | $10,000 | $20,000 |

### 5.2 Tranche Rules
- Each tranche fires **exactly once per dip cycle**
- Once a tranche fires, it does not re-fire even if price dips further and recovers
  back through the same threshold
- If cash available < tranche amount, buy as much as cash allows (partial tranche)
- Tranches are only triggered if the **Higher-Low Rule** is satisfied (Section 6)

### 5.3 Per-Stock Cost Cap
- **$20,000 maximum cost basis per dip cycle**
- The cap is **per cycle**, not per stock lifetime:
  - Cycle 1: spend up to $20K → shares held
  - Stock makes new 52-week high → cycle resets
  - Cycle 2: can spend up to $20K again (even while still holding cycle 1 shares)
  - Total cost basis across cycles can exceed $20K while holding
- After a full sell and 30-day cooldown, the stock re-enters with a **fresh $20K cap**

---

## 6. Higher-Low Rule (Uptrend Confirmation)

Only buy dip tranches if the current dip is part of an uptrend (higher lows).

### 6.1 Definitions
- **Dip low:** The lowest closing price reached during a dip cycle
- **Previous dip low:** The dip low from the most recently completed dip cycle
- **Dip cycle reset:** Triggered when price makes a new 52-week high

### 6.2 Rule
- On each new dip cycle, compare the **current dip low** to the **previous dip low**
- **Buy condition:** current dip low > previous dip low (higher low = uptrend intact)
- **Skip condition:** current dip low ≤ previous dip low (lower low = potential downtrend)
- If skipped, **no tranches fire** for that entire dip cycle
- Previous dip low memory resets when the dip cycle resets (new 52-week high)

### 6.3 Example
```
Peak $100 → dips to $80 (-20%) → buy $2K  [previous dip low = $80]
Recovers to $110 (new 52-wk high) → RESET  [new peak = $110]
Dips to $88 (-20% from $110) → $88 > $80 → BUY $2K ✅  [new dip low = $88]
Recovers to $95 → dips to $76 → $76 < $80 → SKIP ❌
```

### 6.4 First Dip
- The very first dip cycle for a stock has no previous dip low
- **Always buy** the first qualifying dip (no prior low to compare against)

---

## 7. Sell Rules

### 7.1 Primary Sell Trigger
Sell the **entire position** when **both** conditions are true simultaneously:
1. Closing price is **below the 9-month (189-day) SMA**
2. Price has been continuously below the 9M SMA for **≥ 30 consecutive calendar days**

### 7.2 Minimum Hold Period
- Sell rule does **not** activate until **30 calendar days** after the first tranche purchase
- Prevents immediate stop-out on volatile entries

### 7.3 Tax Efficiency Flag
- Positions held < 12 months at time of sell are flagged as "short-term" in the trade log
- Positions held ≥ 12 months flagged as "long-term"
- This is informational only - not a hard trading rule

### 7.4 Post-Sell Cooldown
- **30 calendar days** cooldown after selling before the stock can be re-bought
- Avoids wash-sale rule issues
- During cooldown, stock is removed from the active watchlist

### 7.5 Re-entry After Sell
- After cooldown expires, stock re-enters the watchlist
- **Fresh $20K cap** for the new holding period
- **Dip history fully reset** (no memory of previous dip lows)
- Must re-qualify through the monthly universe screen

---

## 8. Hold Rules

- Hold while closing price ≥ 9-month (189-day) SMA
- **No profit-taking rule** - let winners run
- **No trimming** - position grows freely above $20K market value
- **No rebalancing** - once in, hold until sell rule triggers

---

## 9. Benchmarks

| Benchmark | Bloomberg Ticker | Description |
|-----------|-----------------|-------------|
| Primary | `RLUSG Index` | Russell 1000 Growth Total Return (dividends reinvested) |
| Secondary | `XNDX Index` | Nasdaq-100 Total Return Index |

$500K scaled to benchmark index level on day 1. Annual $50K injections added
to benchmark on the same dates as strategy injections.

---

## 10. Backtest Parameters

| Parameter | Value |
|-----------|-------|
| Full run | 15 years (or from 2010-01-01 to present) |
| Performance slices | 15yr, 10yr, 5yr, 1yr (from end date backward) |
| Price data | Daily adjusted close (`PX_LAST`, split + dividend adjusted) |
| SMA calculation | Simple moving average, 189 trading days |
| 52-week high | Rolling 252 trading-day maximum |
| Execution | End-of-day close price on signal date (no look-ahead) |
| Transaction cost | Configurable via `--cost-bps` (default: 0) |

---

## 11. Output Files

| File | Contents |
|------|----------|
| `moat_portfolio_daily.csv` | Daily NAV, cash, MM interest accrued, benchmark values |
| `moat_trades.csv` | Every tranche buy/sell: date, ticker, action, dip%, shares, price, cost, P&L, hold days, tax flag |
| `moat_positions.csv` | Current open positions: cost basis, market value, unrealized P&L, cycle #, tranches fired |
| `moat_universe_monthly.csv` | Monthly universe snapshots with Option B and C scores |
| `moat_report.txt` | Full performance report with 15/10/5/1yr slices |
| `moat_chart.png` | 4-panel chart: NAV vs benchmarks, cumulative return, drawdown, # positions |

---

## 12. Bloomberg Data Fields Required

| Field | Bloomberg Name | Usage |
|-------|---------------|-------|
| Daily close price | `PX_LAST` | All price calculations |
| 52-week high | Computed from rolling `PX_LAST` | Dip % trigger |
| Market cap | `CUR_MKT_CAP` | Hard filter |
| Revenue growth TTM | `SALES_GROWTH` | Option B screen |
| Gross margin | `GROSS_MARGIN` | Option B screen |
| Return on equity | `RETURN_ON_EQUITY` | Option B screen |
| Debt/Equity | `TOT_DEBT_TO_TOT_EQY` | Option B screen |
| EPS growth TTM | `IS_EPS` (computed) or `TRAIL_12M_EPS_AGWTH` | Option B screen |
| Analyst consensus | `TOT_ANALYST_REC` | Option C screen |
| Forward EPS growth | `BEST_EPS_GROWTH` | Option C screen |
| Fed Funds rate | `FEDL01 Index` | Cash MM accrual |
| NDX-100 weights | `INDX_MWEIGHT` on `NDX Index` | Universe |
| MOAT index weights | `INDX_MWEIGHT` on `MWMFTR Index` | Universe (post-2012) |
| Russell 1000 Gr TR | `PX_LAST` on `RLUSG Index` | Primary benchmark |
| NDX-100 TR | `PX_LAST` on `XNDX Index` | Secondary benchmark |

---

## 13. Key Design Decisions & Rationale

| Decision | Rationale |
|----------|-----------|
| 52-week high as peak | Widely used, objective, avoids all-time-high recency bias |
| Higher-low rule | Ensures we only buy dips within uptrends, not falling knives |
| 9-month SMA (189-day) | Longer than 200-day to reduce whipsaws; captures major trend |
| 30-day confirmation before sell | Avoids selling on brief volatility spikes below SMA |
| 30-day minimum hold | Prevents immediate stop-out on volatile entries |
| 30-day cooldown after sell | Wash-sale rule compliance |
| Cap resets per cycle | Allows compounding across multiple dip cycles in uptrending stocks |
| Option B primary screen | Directly measures moat strength (gross margin + ROE) + growth |
| Option C secondary screen | Forward-looking analyst consensus as alternative signal |
| RLUSG as primary benchmark | Most relevant peer: large-cap US growth, total return |

---

## 14. Open Items / Future Enhancements

- [ ] Test alternative peak definitions (all-time high, 6-month high)
- [ ] Add position-level stop-loss (e.g., -60% from cost basis)
- [ ] Test with transaction costs (5 bps, 10 bps)
- [ ] Add sector concentration limits (e.g., max 30% in any one GICS sector)
- [ ] Explore dividend reinvestment within positions
- [ ] Test 6-month and 12-month SMA as alternative trend filters
- [ ] Add volatility filter (exclude stocks with 30-day realized vol > 60%)
