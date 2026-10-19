"""
ASX DCF Dashboard — local-only Streamlit app.

Type a ticker, pull its financials via yfinance, and see a full DCF
valuation with every calculation step shown and manually adjustable. No
black-box numbers: every derived figure traces back to an assumption you
can see and edit. Each section's own adjustables live right above that
section's results — no sidebar, no hunting for the control that drives a
number somewhere else on the page.

Run locally with:
    pip install -r requirements.txt
    streamlit run dcf_dashboard.py
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import dcf_engine as eng
import excel_export as xl

st.set_page_config(page_title="ASX DCF Dashboard", layout="wide")

st.title("ASX DCF Dashboard")
st.caption("Revenue-driven FCF build-up, a fully auditable WACC, and a blended Perpetuity Growth / Exit Multiple terminal value — every number traceable to an editable assumption.")


# ---------------------------------------------------------------------------
# Data fetching (network — the only part of this file that isn't pure)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_ticker_data(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    info = t.info or {}
    return {
        "info": info,
        "financials": t.financials,
        "cashflow": t.cashflow,
        "balance_sheet": t.balance_sheet,
    }


def normalize_ticker(raw: str) -> str:
    raw = (raw or "").strip().upper()
    if raw and not raw.endswith(".AX") and "." not in raw:
        raw = f"{raw}.AX"
    return raw


def validate_fetched_info(ticker: str, info: dict) -> None:
    has_price = info.get("currentPrice") or info.get("regularMarketPrice")
    has_name = info.get("longName") or info.get("shortName")
    if not info or (not has_price and not has_name):
        raise eng.InsufficientDataError(
            f"'{ticker}' returned no usable data from Yahoo Finance. Check the ticker is correct and still "
            "listed — ASX tickers need the '.AX' suffix (e.g. 'BHP.AX'), which is added automatically if you "
            "don't type it."
        )


# ---------------------------------------------------------------------------
# Company — ticker/fetch, plus Tax Rate (genuinely shared by WACC and FCF
# below, so it lives here rather than under either one specifically).
# ---------------------------------------------------------------------------

with st.container(border=True):
    col_ticker, col_fetch = st.columns([4, 1])
    with col_ticker:
        ticker_input = st.text_input("ASX ticker", value="BHP", help="'.AX' is added automatically if you don't type it.")
    with col_fetch:
        st.write("")  # spacer so the button lines up with the text input, not its label
        fetch_clicked = st.button("Fetch & Run Valuation", type="primary", width="stretch")
    ticker = normalize_ticker(ticker_input)

    st.caption("Tax Rate is shared between the FCF tax shield (in FCF Forecast below) and WACC's after-tax cost of debt — one number, not two that could drift apart.")
    tax_rate_pct = st.slider("Tax Rate (%)", 0.0, 40.0, 30.0, 0.5)

st.caption("Data via yfinance. Missing fields fall back to defaults, shown as warnings in the relevant section below.")


# ---------------------------------------------------------------------------
# Session state — the fetch is network/cache-bound and happens once per
# click; everything downstream recomputes live from session_state + the
# current widget values on every script rerun (cheap, pure Python).
# ---------------------------------------------------------------------------

if fetch_clicked:
    if not ticker:
        st.error("Enter a ticker first.")
    else:
        try:
            with st.spinner(f"Pulling financials for {ticker}..."):
                raw = fetch_ticker_data(ticker)
                validate_fetched_info(ticker, raw["info"])
            st.session_state["raw_data"] = raw
            st.session_state["fetched_ticker"] = ticker
        except Exception as e:
            st.session_state.pop("raw_data", None)
            st.error(f"Couldn't fetch data for '{ticker}': {e}")

if "raw_data" not in st.session_state:
    st.info("Enter an ASX ticker above (e.g. **BHP**) and click **Fetch & Run Valuation**.")
    st.caption("Educational tool, not financial advice.")
    st.stop()

raw = st.session_state["raw_data"]
active_ticker = st.session_state["fetched_ticker"]
info, financials, cashflow, balance_sheet = raw["info"], raw["financials"], raw["cashflow"], raw["balance_sheet"]

snapshot = eng.extract_company_snapshot(info, balance_sheet)
interest_expense = eng._latest(financials, eng.INTEREST_EXPENSE_LABELS)
tax_rate = tax_rate_pct / 100.0

c1, c2, c3 = st.columns(3)
c1.metric("Company", snapshot.name or active_ticker)
c2.metric("Current Price", f"${snapshot.current_price:,.2f}" if snapshot.current_price else "n/a")
c3.metric("Market Cap", f"${snapshot.market_cap/1e9:,.2f}bn" if snapshot.market_cap else "n/a")
for w in snapshot.warnings:
    st.warning(w)

st.divider()

# ---------------------------------------------------------------------------
# WACC
# ---------------------------------------------------------------------------

st.subheader("WACC")

wc1, wc2, wc3 = st.columns(3)
risk_free_rate_pct = wc1.slider("Risk-Free Rate (%)", 1.0, 8.0, 4.3, 0.1)
erp_pct = wc2.slider("Equity Risk Premium (%)", 2.0, 9.0, 5.5, 0.1)
with wc3:
    override_beta = st.checkbox("Override Beta")
    beta_override_value = st.number_input("Beta override", value=1.0, step=0.05, disabled=not override_beta)

beta = beta_override_value if override_beta else snapshot.beta

wacc_res = eng.compute_wacc(
    risk_free_rate=risk_free_rate_pct / 100.0, beta=beta, equity_risk_premium=erp_pct / 100.0,
    interest_expense=interest_expense, total_debt=snapshot.total_debt, market_cap=snapshot.market_cap or 0.0,
    tax_rate=tax_rate,
)

w1, w2, w3, w4, w5 = st.columns(5)
w1.metric("Beta used", f"{beta:.2f}")
w2.metric("Cost of Equity", f"{wacc_res.cost_of_equity:.2%}")
w3.metric("Cost of Debt", f"{wacc_res.cost_of_debt:.2%}")
w4.metric("Weight Equity / Debt", f"{wacc_res.weight_of_equity:.0%} / {wacc_res.weight_of_debt:.0%}")
w5.metric("WACC", f"{wacc_res.wacc:.2%}")

with st.expander("Show WACC formulas with numbers substituted"):
    st.markdown(f"""
- **Cost of Equity** = Risk-Free Rate + Beta x ERP = {risk_free_rate_pct/100:.2%} + {beta:.2f} x {erp_pct/100:.2%} = **{wacc_res.cost_of_equity:.2%}**
- **Cost of Debt** ({wacc_res.cost_of_debt_method.replace('_', ' ')}) = **{wacc_res.cost_of_debt:.2%}**
- **Weight of Equity** = Market Cap / (Market Cap + Debt) = {snapshot.market_cap or 0:,.0f} / ({snapshot.market_cap or 0:,.0f} + {snapshot.total_debt:,.0f}) = **{wacc_res.weight_of_equity:.2%}**
- **Weight of Debt** = 1 - Weight of Equity = **{wacc_res.weight_of_debt:.2%}**
- **WACC** = {wacc_res.weight_of_equity:.2%} x {wacc_res.cost_of_equity:.2%} + {wacc_res.weight_of_debt:.2%} x {wacc_res.cost_of_debt:.2%} x (1 - {tax_rate:.0%}) = **{wacc_res.wacc:.2%}**
""")
for w in wacc_res.warnings:
    st.warning(w)

st.divider()

# ---------------------------------------------------------------------------
# FCF Forecast — editable per-year table
# ---------------------------------------------------------------------------

st.subheader("FCF Forecast")

f1, f2, f3 = st.columns(3)
n_forecast_years = f1.slider("Forecast Years", 1, 10, 5)
history_years = f2.slider(
    "History Years", 1, 8, 5,
    help="How many years of historical financials to pull and average into the default forecast assumptions (EBIT margin, CapEx/D&A/ΔNWC % of revenue, revenue CAGR). yfinance realistically caps out around 4-5 usable years even if you ask for more — you'll see a warning below if fewer were actually available.",
)
terminal_growth_pct = f3.slider(
    "Terminal Growth Rate (%)", 0.0, 6.0, 2.5, 0.1,
    help="The ending point of the revenue-growth glide path below, and reused as the perpetuity growth rate 'g' in Terminal Value further down.",
)

with st.expander("Base-year overrides (optional) — use if yfinance's income statement is missing data"):
    ov1, ov2 = st.columns(2)
    with ov1:
        override_revenue = st.checkbox("Override base-year Revenue")
        revenue_override_value = st.number_input("Base-year Revenue override", value=0.0, disabled=not override_revenue, format="%.0f")
    with ov2:
        override_ebit = st.checkbox("Override base-year EBIT")
        ebit_override_value = st.number_input("Base-year EBIT override", value=0.0, disabled=not override_ebit, format="%.0f")

try:
    hist = eng.extract_historical_financials(financials, cashflow, balance_sheet, max_years=history_years)
except eng.InsufficientDataError as e:
    if override_revenue and revenue_override_value > 0:
        hist = eng.historical_financials_from_revenue_override(
            revenue_override_value, ebit_override_value if override_ebit else None,
        )
    else:
        st.error(
            f"Couldn't derive historical financials for {active_ticker}: {e}\n\n"
            "Open **Base-year overrides** above, check **Override base-year Revenue**, and enter a figure "
            "to proceed with generic default assumptions."
        )
        st.stop()

if override_ebit and ebit_override_value and hist.n_years > 0:
    hist = eng.override_latest_ebit(hist, ebit_override_value)

base_revenue = revenue_override_value if (override_revenue and revenue_override_value > 0) else hist.latest_revenue
if not base_revenue or base_revenue <= 0:
    st.error("No usable base-year revenue. Enable **Override base-year Revenue** above to proceed.")
    st.stop()

for w in hist.warnings:
    st.warning(w)

state_key = f"assumptions_{active_ticker}_{n_forecast_years}_{terminal_growth_pct}_{history_years}"
if state_key not in st.session_state:
    st.session_state[state_key] = eng.build_default_forecast_assumptions(hist, n_forecast_years, terminal_growth_pct)

st.caption("Every cell is editable. Defaults: Year 1 revenue growth = historical CAGR, tapering linearly to the terminal growth rate; margins flat at historical averages.")
edited_assumptions = st.data_editor(
    st.session_state[state_key],
    column_config={
        "year": st.column_config.NumberColumn("Year", disabled=True),
        "revenue_growth_pct": st.column_config.NumberColumn("Revenue Growth %", format="%.2f"),
        "ebit_margin_pct": st.column_config.NumberColumn("EBIT Margin %", format="%.2f"),
        "capex_pct_of_revenue": st.column_config.NumberColumn("CapEx % of Revenue", format="%.2f"),
        "da_pct_of_revenue": st.column_config.NumberColumn("D&A % of Revenue", format="%.2f"),
        "nwc_pct_of_revenue": st.column_config.NumberColumn("ΔNWC % of Revenue", format="%.2f"),
    },
    hide_index=True, width="stretch", key=f"editor_{state_key}",
)

try:
    proj = eng.compute_fcf_projection(edited_assumptions, base_revenue, tax_rate, wacc_res.wacc)
except eng.InvalidInputError as e:
    st.error(str(e))
    st.stop()

audit_df = proj.copy()
audit_df.columns = ["Year", "Revenue", "Rev Growth %", "EBIT Margin %", "EBIT", "D&A", "CapEx", "ΔNWC", "EBITDA", "FCF", "Discount Factor", "PV of FCF"]
st.dataframe(
    audit_df.style.format({
        "Revenue": "{:,.0f}", "EBIT": "{:,.0f}", "D&A": "{:,.0f}", "CapEx": "{:,.0f}", "ΔNWC": "{:,.0f}",
        "EBITDA": "{:,.0f}", "FCF": "{:,.0f}", "PV of FCF": "{:,.0f}", "Discount Factor": "{:.4f}",
        "Rev Growth %": "{:.2f}%", "EBIT Margin %": "{:.2f}%",
    }),
    hide_index=True, width="stretch",
)
st.metric("Sum of PV(FCF)", f"${proj['pv_fcf'].sum():,.0f}")

st.divider()

# ---------------------------------------------------------------------------
# Terminal Value
# ---------------------------------------------------------------------------

st.subheader("Terminal Value")

default_multiple, exit_mult_warnings = eng.default_exit_multiple(
    snapshot.market_cap or 0.0, snapshot.total_debt, snapshot.cash, hist.latest_ebitda,
)

tv_c1, tv_c2 = st.columns(2)
perpetuity_weight_pct = tv_c1.slider("Perpetuity Weight — Perpetuity vs. Exit Multiple (%)", 0, 100, 50)
with tv_c2:
    override_exit_multiple = st.checkbox("Override Exit Multiple")
    exit_multiple_manual = st.number_input("Exit Multiple (x EBITDA)", value=8.0, step=0.5, disabled=not override_exit_multiple)

exit_multiple = exit_multiple_manual if override_exit_multiple else (default_multiple if default_multiple is not None else 8.0)

for w in exit_mult_warnings:
    st.warning(w)
if exit_multiple < 0:
    st.warning(f"Exit multiple is negative ({exit_multiple:.1f}x) — check your inputs before trusting the exit-multiple terminal value.")
elif exit_multiple > 40:
    st.warning(f"Exit multiple is unusually high ({exit_multiple:.1f}x, above 40x) — check your inputs before trusting the exit-multiple terminal value.")

final_fcf = float(proj["fcf"].iloc[-1])
final_ebitda = float(proj["ebitda"].iloc[-1])

try:
    tv = eng.compute_terminal_value(
        fcf_final=final_fcf, ebitda_final=final_ebitda, n_years=len(proj),
        wacc=wacc_res.wacc, terminal_growth=terminal_growth_pct / 100.0,
        exit_multiple=exit_multiple, perpetuity_weight=perpetuity_weight_pct / 100.0,
    )
except eng.TerminalGrowthExceedsWaccError as e:
    st.error(str(e))
    st.stop()

tv1, tv2 = st.columns(2)
with tv1:
    st.markdown("**Perpetuity Growth**")
    st.latex(r"TV = \frac{FCF_N \times (1+g)}{WACC - g}")
    st.markdown(f"= {final_fcf:,.0f} x (1 + {terminal_growth_pct/100:.2%}) / ({wacc_res.wacc:.2%} - {terminal_growth_pct/100:.2%}) = **\\${tv.tv_perpetuity:,.0f}**")
with tv2:
    st.markdown("**Exit Multiple**")
    st.latex(r"TV = EBITDA_N \times Multiple")
    st.markdown(f"= {final_ebitda:,.0f} x {exit_multiple:.1f}x = **\\${tv.tv_exit_multiple:,.0f}**")
    st.caption(f"Default multiple from (Market Cap + Debt - Cash) / Latest Historical EBITDA = {default_multiple:.1f}x" if default_multiple else "")

# Every literal '$' below is escaped (\$) — st.markdown treats a pair of
# unescaped '$' as inline LaTeX math-mode delimiters, which silently mangled
# these lines (dollar amounts disappearing, spaces stripped) when they weren't.
st.markdown(
    f"**Blended TV** = {perpetuity_weight_pct}% x \\${tv.tv_perpetuity:,.0f} + {100-perpetuity_weight_pct}% x \\${tv.tv_exit_multiple:,.0f} "
    f"= **\\${tv.blended_tv:,.0f}**  \n"
    f"**PV of Terminal Value** = \\${tv.blended_tv:,.0f} / (1 + {wacc_res.wacc:.2%})^{len(proj)} = **\\${tv.pv_terminal_value:,.0f}**"
)

with st.expander("Show implied assumptions (cross-checks)"):
    ic1, ic2 = st.columns(2)
    if tv.implied_exit_multiple_from_perpetuity is not None:
        ic1.metric("Perpetuity method implies exit multiple of", f"{tv.implied_exit_multiple_from_perpetuity:.1f}x")
    if tv.implied_perpetuity_growth_from_exit is not None:
        ic2.metric("Exit multiple method implies perpetual growth of", f"{tv.implied_perpetuity_growth_from_exit:.2%}")
        if tv.implied_perpetuity_growth_from_exit >= wacc_res.wacc:
            st.warning("The exit multiple's implied growth rate exceeds WACC — the two terminal value methods disagree sharply here.")

st.divider()

# ---------------------------------------------------------------------------
# Valuation Summary (bridge)
# ---------------------------------------------------------------------------

st.subheader("Valuation Summary")

vs1, vs2 = st.columns(2)
with vs1:
    override_net_debt = st.checkbox("Override Net Debt")
    net_debt_override_value = st.number_input("Net Debt override", value=0.0, disabled=not override_net_debt, format="%.0f")
with vs2:
    override_shares = st.checkbox("Override Shares Outstanding")
    shares_override_value = st.number_input("Shares Outstanding override", value=0.0, disabled=not override_shares, format="%.0f")

net_debt_override = net_debt_override_value if override_net_debt else None
shares_override = shares_override_value if (override_shares and shares_override_value > 0) else None
shares_outstanding = shares_override if shares_override is not None else snapshot.shares_outstanding

try:
    bridge = eng.compute_valuation_bridge(
        pv_fcf_sum=float(proj["pv_fcf"].sum()), pv_terminal_value=tv.pv_terminal_value,
        total_debt=snapshot.total_debt, cash=snapshot.cash, shares_outstanding=shares_outstanding,
        current_price=snapshot.current_price, net_debt_override=net_debt_override, shares_override=shares_override,
    )
except eng.InvalidInputError as e:
    st.error(f"{e} Enable **Override Shares Outstanding** above with a positive value to proceed.")
    st.stop()

bridge_df = pd.DataFrame([
    ("Sum of PV(FCF)", bridge.pv_fcf_sum),
    ("PV of Terminal Value", bridge.pv_terminal_value),
    ("Enterprise Value", bridge.enterprise_value),
    ("(-) Net Debt", -bridge.net_debt),
    ("Equity Value", bridge.equity_value),
    ("/ Shares Outstanding", bridge.shares_outstanding),
], columns=["Step", "Amount"])
st.dataframe(bridge_df.style.format({"Amount": "{:,.1f}"}), hide_index=True, width="stretch")

m1, m2, m3 = st.columns(3)
m1.metric("Value per Share", f"${bridge.value_per_share:,.2f}")
m2.metric("Current Price", f"${bridge.current_price:,.2f}" if bridge.current_price else "n/a")
if bridge.upside_downside is not None:
    m3.metric("Upside / (Downside)", f"{bridge.upside_downside:+.1%}")
    if bridge.is_extreme:
        st.warning(
            f"The implied upside/downside ({bridge.upside_downside:+.0%}) is extreme (beyond ±100%). "
            "This is a prompt to double-check your assumptions, not a valuation to take at face value."
        )
else:
    m3.metric("Upside / (Downside)", "n/a (no current price)")

st.divider()

# ---------------------------------------------------------------------------
# Sensitivity heatmap
# ---------------------------------------------------------------------------

st.subheader("Sensitivity — Value per Share")
st.caption("WACC x Terminal Growth Rate, **Perpetuity method only** — it re-discounts the explicit-period FCF at each WACC too, but does not capture the blended terminal value, so it will not exactly match the headline figure above.")

wacc_center = wacc_res.wacc
wacc_grid = [wacc_center + d for d in (-0.02, -0.01, 0.0, 0.01, 0.02)]
growth_grid = [max(0.0, terminal_growth_pct / 100 + d) for d in (-0.02, -0.01, 0.0, 0.01, 0.02)]
grid = eng.sensitivity_grid(
    list(proj["fcf"]), float(proj["ebit_margin_pct"].iloc[-1]),
    bridge.net_debt, bridge.shares_outstanding, wacc_grid, growth_grid,
)

heat_fig = go.Figure(data=go.Heatmap(
    z=grid.values, x=grid.columns, y=grid.index, colorscale="RdYlGn",
    text=np.round(grid.values, 2), texttemplate="%{text}", hovertemplate="WACC %{y}<br>Terminal g %{x}<br>Value/share %{z:.2f}<extra></extra>",
))
heat_fig.update_layout(xaxis_title="Terminal Growth Rate", yaxis_title="WACC", height=420, margin=dict(t=10, b=10))
st.plotly_chart(heat_fig, width="stretch")

st.divider()

# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

st.subheader("Export")
payload = xl.ExportPayload(
    ticker=active_ticker, company_name=snapshot.name, current_price=snapshot.current_price,
    tax_rate=tax_rate,
    risk_free_rate=risk_free_rate_pct / 100.0, equity_risk_premium=erp_pct / 100.0, beta=beta,
    interest_expense=interest_expense, total_debt=snapshot.total_debt, market_cap=snapshot.market_cap or 0.0,
    base_revenue=base_revenue, assumptions=edited_assumptions,
    terminal_growth=terminal_growth_pct / 100.0, exit_multiple=exit_multiple, perpetuity_weight=perpetuity_weight_pct / 100.0,
    latest_historical_ebitda=hist.latest_ebitda,
    cash=snapshot.cash, net_debt_override=net_debt_override, shares_outstanding=shares_outstanding, shares_override=shares_override,
)
excel_bytes = xl.export_to_bytes(payload)
st.download_button(
    "Download formula-driven Excel model (.xlsx)", data=excel_bytes,
    file_name=f"{active_ticker.replace('.', '_')}_dcf_model.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

st.divider()
st.caption("Educational tool, not financial advice. All figures are model outputs based on editable assumptions, not predictions.")
