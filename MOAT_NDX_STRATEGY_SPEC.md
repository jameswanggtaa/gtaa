# MOAT + Nasdaq-100 Dip-Buying Strategy - Complete Specification
**Version:** 2.0  
**Date:** 2026-09-21  
**File:** MOAT_NDX_STRATEGY_SPEC.md

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-09-19 | Initial strategy specification |
| 2.0 | 2026-09-21 | **Option A:** % of NAV position sizing (replaces fixed $ tranches) |
| | | **Option B:** Core holding layer - always invested in uptrending stocks |
| | | **Option C:** Relaxed higher-low rule - only enforced below 9M SMA |
| | | **Option D:** Trailing stop sell (replaces 9M SMA sell rule) |

---

## 1. Strategy Overview

A buy-and-hold dip-buying strategy targeting high-quality "MOAT" stocks from the
Nasdaq-100 universe. The strategy maintains a **core holding** in every uptrending
quality stock (above 9M SMA), then **adds aggressively on dips** using % of NAV
sizing that grows with the portfolio. Positions are held until a **trailing stop**
fires, letting winners compound for years. Cash earns money market rates.

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
| Per-stock max allocation | 10% of current portfolio NAV |
| Per-cycle max cost | 7.5% of current portfolio NAV |
| Sold proceeds | Recycled immediately into available cash pool |

---

## 4. Peak Definition

- **52-week rolling high:** Highest closing price over the trailing **252 trading days**
- Recalculated daily for every stock in the universe
- Used as the reference point for all dip % calculations

---

## 5. Buy Rules

### 5.1 Core Holding Layer (Option B - always invested)

Any stock in the universe that is **above its 9-month (189-day) SMA** receives
a **base allocation of 2% of current portfolio NAV**, topped up every ~21 trading
days. This ensures the portfolio is always invested in the best uptrending stocks
even when they never dip enough to trigger a dip tranche.

- Core allocation target: **2% of NAV per stock**
- Checked and topped up every **21 trading days**
- Core buy fires immediately when a stock enters the universe above its 9M SMA
- Core position counts toward the 10% per-stock cap

### 5.2 Dip Tranche Schedule (Option A - % of NAV sizing)

Triggered when `(current_price / 52_week_high - 1)` crosses each threshold.
**Amounts are % of current portfolio NAV** - they grow as the portfolio grows.

| Dip from 52-wk High | % of NAV | Example at $1M NAV | Example at $2M NAV |
|---------------------|----------|-------------------|-------------------|
| ≥ -10% | 0.5% | $5,000 | $10,000 |
| ≥ -20% | 1.0% | $10,000 | $20,000 |
| ≥ -30% | 2.0% | $20,000 | $40,000 |
| ≥ -40% | 4.0% | $40,000 | $80,000 |
| ≥ -50% | 2.5% | $25,000 | $50,000 |

### 5.3 Tranche Rules
- Each tranche fires **exactly once per dip cycle**
- If cash available < tranche amount, buy as much as cash allows (partial tranche)
- Tranches only trigger if the **Higher-Low Rule** is satisfied (Section 6)

### 5.4 Per-Stock Caps
- **Maximum total allocation: 10% of NAV per stock** (core + dip tranches combined)
- **Maximum per dip cycle: 7.5% of NAV** (leaves room for a second cycle)
- Cap is **per cycle** - resets when stock makes a new 52-week high
- After a full sell and 30-day cooldown, stock re-enters with a **fresh cap**

---

## 6. Higher-Low Rule (Relaxed - Option C)

### 6.1 Rule (v2 - Relaxed)
The higher-low rule is now **context-sensitive** based on the 9M SMA:

| Stock position vs 9M SMA | Higher-low rule |
|--------------------------|----------------|
| **Above 9M SMA** (uptrend) | **Disabled** - always allow dip buys |
| **Below 9M SMA** (downtrend) | **Enforced** - require higher low to avoid falling knives |
| First dip cycle ever | **Disabled** - always buy (no prior low to compare) |

### 6.2 Rationale
Strong uptrending stocks (NVDA, AAPL, META) regularly make lower dip lows during
normal corrections while remaining in long-term uptrends. The old strict rule
prevented buying these dips unnecessarily. Now we only apply the filter when the
stock is already in a downtrend (below 9M SMA), where falling-knife risk is real.

### 6.3 Example
```
Stock above 9M SMA:
  Peak $100 → dips to $80 (-20%) → BUY ✅ (above SMA, rule relaxed)
  Recovers → dips to $75 → BUY ✅ (still above SMA, lower low ignored)

Stock below 9M SMA:
  Peak $100 → dips to $80 (-20%) → BUY ✅ [prev dip low = $80]
  Recovers to $95 → dips to $76 → SKIP ❌ ($76 < $80, lower low = downtrend)
```

---

## 7. Sell Rules (Option D - Trailing Stop)

### 7.1 Primary Sell Trigger - Trailing Stop
Sell the **entire position** when:
1. Current market value drops **≥ 20% below the position's peak market value**
2. This condition persists for **≥ 3 consecutive trading days** (confirmation)

```
Peak MV = highest market value since position was opened
Trailing stop = Peak MV × (1 - 0.20) = Peak MV × 0.80
Sell when: current MV < trailing stop  for 3+ consecutive days
```

**Why trailing stop instead of 9M SMA:**
- Lets winners like NVDA run for years without being stopped out on corrections
- Cuts losers faster than waiting 30 days below SMA
- Adapts to position size - a $500K position has a much higher stop than a $10K one
- Avoids the whipsaw problem of stocks briefly dipping below SMA then recovering

### 7.2 Minimum Hold Period
- Sell rule does **not** activate until **30 calendar days** after first buy
- Prevents immediate stop-out on volatile entries

### 7.3 Tax Efficiency Flag
- Positions held < 12 months flagged as "short-term" in trade log
- Positions held ≥ 12 months flagged as "long-term"
- Informational only - not a hard trading rule

### 7.4 Post-Sell Cooldown
- **30 calendar days** cooldown after selling before re-buying
- Avoids wash-sale rule issues

### 7.5 Re-entry After Sell
- After cooldown: stock re-enters watchlist with **fresh caps and reset dip history**
- Core holding layer will immediately re-buy if stock is above 9M SMA

---

## 8. Hold Rules

- **No profit-taking rule** - let winners run freely
- **No trimming** - position grows above 10% NAV cap (cap only limits new buys)
- **No rebalancing** - hold until trailing stop fires
- 9M SMA used only for: (1) core holding entry, (2) higher-low rule enforcement

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
| Full run | 15 years (2011 to present) |
| Performance slices | 15yr, 10yr, 5yr, 1yr (from end date backward) |
| Price data | Daily adjusted close (`PX_LAST`, split + dividend adjusted) |
| SMA calculation | Simple moving average, 189 trading days (9 months) |
| 52-week high | Rolling 252 trading-day maximum |
| Trailing stop | 20% from peak market value, 3-day confirmation |
| Core holding | 2% NAV per stock above 9M SMA, checked every 21 days |
| Execution | End-of-day close price on signal date (no look-ahead) |
| Transaction cost | Configurable via `--cost-bps` (default: 0) |
| Data storage | Download once to disk; backtest runs from CSV files |

---

## 11. File Structure

### Downloaded Data (`moat_data/`)
| File | Contents |
|------|----------|
| `prices.csv` | Daily adjusted close prices (all tickers + SMA warmup) |
| `fundamentals.csv` | Option B + C screening fields as of download date |
| `benchmark_rlusg.csv` | Russell 1000 Growth daily prices |
| `benchmark_xndx.csv` | Nasdaq-100 TR daily prices |
| `fedfunds.csv` | Fed Funds rate daily |
| `universe.csv` | Full ticker list |
| `manifest.json` | Download metadata (date, tickers, source) |

### Backtest Results (`moat_results_b/` or `moat_results_c/`)
| File | Contents |
|------|----------|
| `moat_portfolio_daily.csv` | Daily NAV, cash, invested, drawdown, benchmark values |
| `moat_trades.csv` | Every trade: date, ticker, action, dip%, shares, price, P&L, hold days, tax flag |
| `moat_positions.csv` | End-of-period open positions with unrealized P&L |
| `moat_universe_scored.csv` | Scored + selected tickers with all screening metrics |
| `moat_report.txt` | Full performance report (15/10/5/1yr vs RLUSG and XNDX) |
| `moat_chart.png` | 4-panel chart: NAV, cumulative return, drawdown, # positions |

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
| % of NAV sizing (v2) | Position sizes grow with portfolio; avoids chronic underinvestment |
| Core holding layer (v2) | Always invested in best uptrending stocks; closes gap vs buy-and-hold |
| Relaxed higher-low rule (v2) | Uptrending stocks (above SMA) should always be bought on dips |
| Trailing stop sell (v2) | Lets NVDA/AAPL-style winners run for years; cuts losers faster |
| 9-month SMA (189-day) | Used for core entry signal and higher-low rule context |
| 3-day trailing stop confirm | Avoids selling on single-day volatility spikes |
| 30-day minimum hold | Prevents immediate stop-out on volatile entries |
| 30-day cooldown after sell | Wash-sale rule compliance |
| Cap resets per cycle | Allows compounding across multiple dip cycles |
| Option B primary screen | Directly measures moat strength (gross margin + ROE) + growth |
| Option C secondary screen | Forward-looking analyst consensus as alternative signal |
| RLG/RLUSG as primary benchmark | Most relevant peer: large-cap US growth, total return |
| Download-once architecture | Preserves Bloomberg API quota; enables instant parameter iteration |

---

## 14. Usage Commands

```powershell
# Phase 1: Download all Bloomberg data once
python run_moat.py --download --data-dir .\moat_data

# Phase 2: Run backtest (no Bloomberg needed)
python run_moat.py --backtest --data-dir .\moat_data --screen B
python run_moat.py --backtest --data-dir .\moat_data --screen C

# Tweak parameters freely - instant, zero Bloomberg quota
python run_moat.py --backtest --data-dir .\moat_data --cost-bps 5
python run_moat.py --backtest --data-dir .\moat_data --top-n 20
python run_moat.py --backtest --data-dir .\moat_data --start 2015-01-01
python run_moat.py --backtest --data-dir .\moat_data --no-chart
```

---

## 15. Open Items / Future Enhancements

- [ ] Test alternative peak definitions (all-time high, 6-month high)
- [ ] Test with transaction costs (5 bps, 10 bps)
- [ ] Add sector concentration limits (e.g., max 30% in any one GICS sector)
- [ ] Test 15% and 25% trailing stop variants
- [ ] Test core allocation at 3% and 5% NAV
- [ ] Add volatility filter (exclude stocks with 30-day realized vol > 60%)
- [ ] Explore partial sell on trailing stop (sell 50%, hold remainder)
- [ ] Add momentum score to universe ranking (12-1 month return)
