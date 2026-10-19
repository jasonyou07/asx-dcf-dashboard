# ASX DCF Dashboard

A local-only Streamlit dashboard that runs a full, revenue-driven DCF
valuation on any ASX-listed stock, pulling data from Yahoo Finance via
`yfinance`. Type a ticker, get a valuation — with every calculation step
shown and manually adjustable. No black-box numbers: every derived figure
traces back to an assumption you can see and edit.

## Setup

```bash
pip install -r requirements.txt
streamlit run dcf_dashboard.py
```

Then open the local URL Streamlit prints (usually `http://localhost:8501`).

## Usage

1. Enter a ticker in the **Assumptions** panel at the top (e.g. `BHP`) — `.AX` is appended automatically if you don't type it. All inputs live in this one panel, grouped together in the main page (not a sidebar), so nothing requires scrolling back and forth to find the control that drives a result further down.
2. Click **Fetch & Run Valuation**. This is the only step that hits the network; every slider, checkbox, and table edit afterward recomputes instantly from the already-fetched data.
3. Adjust **Tax Rate**, **Forecast Years**, **History Years** (how many years of historicals to average into the default assumptions — see below), **Risk-Free Rate**, **Equity Risk Premium**, **Terminal Growth Rate**, and **Perpetuity Weight** directly in that panel. Less-frequently-used manual overrides (Beta, Exit Multiple, Net Debt, Shares Outstanding, base-year Revenue/EBIT) are tucked into the **Overrides** expander just below it.
4. Edit the per-year FCF assumptions table directly — the entire valuation (WACC, FCF projection, terminal value, bridge, sensitivity heatmap) recalculates live from whatever is currently in the Assumptions panel and the table.
5. Download the **formula-driven Excel model** to keep editing outside the browser — every input cell (blue/yellow) is editable in Excel and every formula (black) recalculates natively.

## Project structure

- `dcf_engine.py` — pure calculation functions (statement-row fallback parsing, historical ratio extraction, FCF build-up, WACC, terminal value, valuation bridge, sensitivity grid). No Streamlit or network imports — fully unit-testable with synthetic data.
- `excel_export.py` — builds the formula-driven `.xlsx` export via `openpyxl`.
- `dcf_dashboard.py` — the Streamlit UI; the only file that imports `yfinance` or `streamlit`.
- `tests/` — pytest suite (see **Testing** below).

## How the FCF forecast is built

Unlike a single blended growth number, this builds FCF up from revenue:

1. **Historical baseline**: for each historical year within the **History Years** window (adjustable, 1-8; yfinance realistically caps out around 4-5 clean years regardless of how high you set it — a warning says so if fewer were actually available), compute EBIT margin, D&A/CapEx/ΔNWC as % of revenue, and the effective tax rate — then average each into a default forecast assumption.
2. **Per-year forecast table** (editable): Revenue Growth %, EBIT Margin %, CapEx % of Revenue, D&A % of Revenue, ΔNWC % of Revenue — one row per forecast year.
   - Revenue growth defaults to a **declining glide path**: Year 1 = historical revenue CAGR (clipped to -5%..20%), tapering linearly to the terminal growth rate by the final forecast year.
   - Every margin assumption defaults flat at its historical average.
3. **Per-year calculation**: `FCF_t = EBIT_t x (1 - TaxRate) + D&A_t - CapEx_t - ΔNWC_t`, where `EBIT_t = Revenue_t x EBITMargin_t` and `Revenue_t = Revenue_{t-1} x (1 + Growth_t)`.

The full audit trail (Revenue, EBIT, D&A, CapEx, ΔNWC, EBITDA, FCF, discount factor, PV of FCF) is shown for every forecast year, not just the final number.

## WACC

Held constant across the forecast (standard practice):
`WACC = WeightEquity x CostEquity + WeightDebt x CostDebt x (1 - TaxRate)`,
with `CostEquity = RiskFree + Beta x ERP` and `CostDebt = InterestExpense / TotalDebt` (falling back to `RiskFree + 2%` if interest expense or total debt is missing). Tax Rate is a **single shared slider** — used in both the FCF tax shield and here — so it can't drift into two different values.

## Terminal value

Both methods are computed and blended: **Perpetuity Growth** (`FCF_N x (1+g) / (WACC-g)`) and **Exit Multiple** (`EBITDA_N x Multiple`, where `EBITDA_N` is the *forecast* final year's EBIT + D&A — the default multiple itself is derived from `(MarketCap + Debt - Cash) / LatestActualEBITDA`). The two undiscounted terminal values are blended first, then discounted once. If `WACC <= Terminal Growth`, the app blocks with a clear warning instead of dividing by zero/negative. An "implied assumptions" toggle cross-checks each method against the other (what exit multiple the perpetuity method implies, and vice versa).

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_dcf_engine.py` — unit tests for every pure function, including edge cases: missing cash flow data, a negative-then-positive revenue history (which would otherwise make the CAGR formula compute a fractional power of a negative number — undefined), WACC ≤ terminal growth, zero shares outstanding, missing EBIT, balance-sheet-fallback ΔNWC, etc.
- `tests/test_excel_export.py` — builds a workbook from synthetic inputs, then headlessly recalculates it with the [`formulas`](https://pypi.org/project/formulas/) library and cross-checks every derived figure (WACC, terminal value, both cross-checks, the full valuation bridge) against `dcf_engine.py`'s own Python calculation for the same inputs, to float precision. Also scans every cell for Excel error values (`#REF!`, `#DIV/0!`, etc.). Skips automatically if `formulas` isn't installed (it's a verification-only dev dependency).
- `tests/test_dashboard_smoke.py` — runs the actual Streamlit app end-to-end via `streamlit.testing.v1.AppTest`, with `yfinance.Ticker` mocked to a realistic synthetic company (this environment can't reach Yahoo Finance's live API reliably either). Exercises the base case, every override checkbox (individually and combined), the Excel export button, an empty-data ticker, and a simulated network failure — all assert zero uncaught exceptions and a plain-English `st.error` where one is expected.

## Known limitations

- **Currency**: some ASX-listed companies (e.g. `BHP.AX`) trade in AUD but report financial statements in USD. This tool does **not** currently detect or convert for that — if a ticker's `financialCurrency` differs from its trading currency, every dollar-denominated figure (revenue, FCF, EBITDA, debt, cash) will be in the statement currency while market cap/share price are in the trading currency, which can produce a value-per-share that's systematically off by the FX rate. Worth checking (`info['currency']` vs `info['financialCurrency']`) before trusting a result for a dual-currency reporter.
- yfinance's row labels vary by ticker/version — every lookup tries a fallback chain of candidate labels, but a genuinely unusual statement layout can still produce a warning-flagged default rather than a real figure.
- The sensitivity heatmap reflects the **Perpetuity method only** (it re-discounts the explicit-period FCF at each WACC too, but doesn't capture the blended terminal value) — it will not exactly match the headline blended value per share.
- WACC and the terminal growth rate are user-set assumptions, not derived from a model of the business's actual risk profile — this tool surfaces the mechanics clearly so you can interrogate your own assumptions, not a prediction.

---

Educational tool, not financial advice.
