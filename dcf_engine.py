"""
Pure calculation engine for the ASX DCF dashboard.

No Streamlit, no network calls (no `yfinance` import) — every function here
takes already-fetched data (pandas DataFrames shaped like yfinance's
statements, or plain floats) and returns plain data structures. That's what
makes it unit-testable with synthetic data and reusable from both
dcf_dashboard.py (the UI) and excel_export.py (the Excel mirror).

Percentage convention: every per-year assumption (revenue growth, EBIT
margin, CapEx/D&A/NWC % of revenue) is stored and passed around as a
PERCENTAGE NUMBER (e.g. 12.5 means 12.5%), not a fraction — that's what a
human types into an editable table cell labeled "%". Each function that
turns assumptions into dollars divides by 100 internally. Rates that come
from sliders (WACC components, terminal growth) are fractions (0.043, not
4.3), following standard Python finance-code convention — see each
function's docstring if the convention for a given parameter is ambiguous.
"""

from dataclasses import dataclass, field, replace
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Errors — raised instead of crashing with a raw traceback or a NaN/inf, so
# the UI layer can catch these and show a plain-English message.
# ---------------------------------------------------------------------------

class DcfError(Exception):
    """Base class for all engine-level errors the UI should handle gracefully."""


class TerminalGrowthExceedsWaccError(DcfError):
    """Terminal growth >= WACC makes the perpetuity-growth formula blow up (divide by zero or negative)."""


class InsufficientDataError(DcfError):
    """Not enough data to compute something, and no override was supplied."""


class InvalidInputError(DcfError):
    """A supplied value (override or otherwise) is not usable, e.g. zero shares outstanding."""


# ---------------------------------------------------------------------------
# Statement-row fallback helpers — yfinance's row labels vary by ticker and
# by yfinance version, so every lookup tries a list of candidate labels in
# order rather than hardcoding one.
# ---------------------------------------------------------------------------

def _row(df: pd.DataFrame | None, candidates: list[str]) -> pd.Series | None:
    """First candidate row label present in df.index, with NaNs dropped — or None if no candidate matches."""
    if df is None or df.empty:
        return None
    for label in candidates:
        if label in df.index:
            series = df.loc[label]
            if isinstance(series, pd.DataFrame):  # duplicate index label — take the first
                series = series.iloc[0]
            series = series.dropna()
            if not series.empty:
                return series
    return None


def _latest(df: pd.DataFrame | None, candidates: list[str], default: float | None = None) -> float | None:
    row = _row(df, candidates)
    if row is None:
        return default
    return float(row.iloc[0])


# Candidate label lists, one named constant per line item so excel_export.py
# and dcf_dashboard.py can reference the same source of truth.
REVENUE_LABELS = ["Total Revenue", "Operating Revenue"]
EBIT_LABELS = ["EBIT", "Operating Income"]
INTEREST_EXPENSE_LABELS = ["Interest Expense", "Interest Expense Non Operating"]
TAX_PROVISION_LABELS = ["Tax Provision"]
PRETAX_INCOME_LABELS = ["Pretax Income"]
DA_LABELS = ["Depreciation And Amortization", "Depreciation", "Depreciation Amortization Depletion"]
CAPEX_LABELS = ["Capital Expenditure", "Capital Expenditures"]
NWC_CHANGE_LABELS = ["Change In Working Capital"]
CURRENT_ASSETS_LABELS = ["Current Assets", "Total Current Assets"]
CURRENT_LIABILITIES_LABELS = ["Current Liabilities", "Total Current Liabilities"]
TOTAL_DEBT_LABELS = ["Total Debt", "Total Debt Net Minority Interest"]
CASH_LABELS = ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]


# ---------------------------------------------------------------------------
# Company snapshot — plain `info` dict parsing, pure/no network, with the
# same fallback-chain discipline as the statement helpers.
# ---------------------------------------------------------------------------

@dataclass
class CompanySnapshot:
    current_price: float | None
    market_cap: float | None
    beta: float | None
    shares_outstanding: float | None
    total_debt: float | None
    cash: float | None
    name: str
    warnings: list[str] = field(default_factory=list)


def extract_company_snapshot(info: dict, balance_sheet: pd.DataFrame | None = None) -> CompanySnapshot:
    warnings: list[str] = []
    info = info or {}

    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    market_cap = info.get("marketCap")
    beta = info.get("beta")
    shares_outstanding = info.get("sharesOutstanding")
    name = info.get("longName") or info.get("shortName") or ""

    if beta is None:
        warnings.append("Beta not available from yfinance — defaulted to 1.0 (override in the sidebar if you have a better estimate)")
        beta = 1.0

    if shares_outstanding is None and market_cap and current_price:
        shares_outstanding = market_cap / current_price
        warnings.append("Shares outstanding not available — derived from market cap / price")

    total_debt = info.get("totalDebt")
    if total_debt is None:
        total_debt = _latest(balance_sheet, TOTAL_DEBT_LABELS)
        if total_debt is not None:
            warnings.append("Total debt not in info — used balance sheet's latest 'Total Debt' instead")
    if total_debt is None:
        total_debt = 0.0
        warnings.append("Total debt not found anywhere — defaulted to 0 (override on the Balance Sheet panel if this is wrong)")

    cash = info.get("totalCash")
    if cash is None:
        cash = _latest(balance_sheet, CASH_LABELS)
        if cash is not None:
            warnings.append("Cash not in info — used balance sheet's latest 'Cash And Cash Equivalents' instead")
    if cash is None:
        cash = 0.0
        warnings.append("Cash not found anywhere — defaulted to 0 (override on the Balance Sheet panel if this is wrong)")

    return CompanySnapshot(
        current_price=current_price, market_cap=market_cap, beta=beta,
        shares_outstanding=shares_outstanding, total_debt=total_debt, cash=cash,
        name=name, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Historical ratio extraction
# ---------------------------------------------------------------------------

@dataclass
class HistoricalFinancials:
    n_years: int
    revenue: pd.Series                 # most-recent-first
    ebit: pd.Series
    ebit_margin_avg: float
    da_pct_avg: float
    capex_pct_avg: float
    nwc_pct_avg: float
    tax_rate_avg: float
    revenue_cagr: float
    latest_revenue: float | None
    latest_ebit: float | None
    latest_da: float | None
    latest_ebitda: float | None        # most recent ACTUAL year's EBIT + D&A (not forecast)
    warnings: list[str] = field(default_factory=list)


def _nwc_increase_series(cashflow: pd.DataFrame | None, balance_sheet: pd.DataFrame | None, warnings: list[str]) -> pd.Series | None:
    """
    Positive value = working capital increased = a use of cash (matches the
    sign convention the FCF formula expects: FCF subtracts NWC% * Revenue).
    Primary source is the cash flow statement's 'Change In Working Capital'
    (which, in yfinance, is signed so a positive value is a cash INFLOW —
    i.e. the opposite convention — so it's negated here). Falls back to the
    balance sheet's year-over-year change in (Current Assets - Current
    Liabilities) if the cash flow statement doesn't have the line at all.
    """
    row = _row(cashflow, NWC_CHANGE_LABELS)
    if row is not None:
        return -row  # yfinance: positive = cash inflow -> flip so positive = cash use

    current_assets = _row(balance_sheet, CURRENT_ASSETS_LABELS)
    current_liabilities = _row(balance_sheet, CURRENT_LIABILITIES_LABELS)
    if current_assets is None or current_liabilities is None:
        warnings.append(
            "Change in working capital not found in the cash flow statement, and Current Assets/"
            "Current Liabilities not found in the balance sheet either — ΔNWC % of revenue defaulted to 0%"
        )
        return None

    net_working_capital = (current_assets - current_liabilities).dropna()
    if len(net_working_capital) < 2:
        warnings.append("Not enough balance sheet history to derive ΔNWC from Current Assets - Current Liabilities — ΔNWC % of revenue defaulted to 0%")
        return None

    warnings.append("Change in working capital not found in the cash flow statement — derived from the balance sheet's year-over-year Current Assets - Current Liabilities instead")
    # most-recent-first series; increase = this period's NWC minus the prior (older) period's
    diffs = net_working_capital.iloc[:-1].values - net_working_capital.iloc[1:].values
    return pd.Series(diffs, index=net_working_capital.index[:-1])


def extract_historical_financials(
    financials: pd.DataFrame | None,
    cashflow: pd.DataFrame | None,
    balance_sheet: pd.DataFrame | None,
    max_years: int = 5,
) -> HistoricalFinancials:
    warnings: list[str] = []

    revenue = _row(financials, REVENUE_LABELS)
    ebit = _row(financials, EBIT_LABELS)
    tax_provision = _row(financials, TAX_PROVISION_LABELS)
    pretax_income = _row(financials, PRETAX_INCOME_LABELS)
    da = _row(cashflow, DA_LABELS)
    capex = _row(cashflow, CAPEX_LABELS)
    nwc_increase = _nwc_increase_series(cashflow, balance_sheet, warnings)

    if revenue is None:
        raise InsufficientDataError(
            "No revenue figures found in the income statement (tried: " + ", ".join(REVENUE_LABELS) + "). "
            "Use the base-year Revenue override to supply it manually."
        )
    revenue = revenue.iloc[:max_years]

    if ebit is None:
        warnings.append("EBIT/Operating Income not found — EBIT margin and every downstream FCF figure will be unavailable until you use the base-year EBIT override")
        ebit_margin_avg = None
        latest_ebit = None
    else:
        ebit = ebit.iloc[:max_years]
        aligned = pd.concat([revenue, ebit], axis=1, join="inner")
        aligned.columns = ["revenue", "ebit"]
        aligned = aligned[aligned["revenue"] != 0]
        ebit_margin_avg = float((aligned["ebit"] / aligned["revenue"]).mean()) if not aligned.empty else None
        latest_ebit = float(ebit.iloc[0])

    def _pct_of_revenue_avg(series: pd.Series | None, label: str, take_abs: bool = False) -> tuple[float, float | None]:
        if series is None:
            warnings.append(f"{label} not found — defaulted its % of revenue to 0%")
            return 0.0, None
        s = series.iloc[:max_years]
        aligned = pd.concat([revenue, s], axis=1, join="inner")
        aligned.columns = ["revenue", "value"]
        aligned = aligned[aligned["revenue"] != 0]
        if aligned.empty:
            warnings.append(f"{label} had no overlapping years with revenue — defaulted its % of revenue to 0%")
            return 0.0, None
        values = aligned["value"].abs() if take_abs else aligned["value"]
        return float((values / aligned["revenue"]).mean()), float(s.iloc[0])

    da_pct_avg, latest_da = _pct_of_revenue_avg(da, "D&A", take_abs=True)
    capex_pct_avg, _latest_capex = _pct_of_revenue_avg(capex, "CapEx", take_abs=True)
    nwc_pct_avg, _latest_nwc = _pct_of_revenue_avg(nwc_increase, "Change in working capital")

    if tax_provision is not None and pretax_income is not None:
        aligned = pd.concat([tax_provision, pretax_income], axis=1, join="inner")
        aligned.columns = ["tax", "pretax"]
        aligned = aligned[aligned["pretax"] != 0]
        if not aligned.empty:
            raw_tax_rate = float((aligned["tax"] / aligned["pretax"]).mean())
            tax_rate_avg = float(np.clip(raw_tax_rate, 0.0, 0.40))
            if abs(raw_tax_rate - tax_rate_avg) > 1e-9:
                warnings.append(f"Raw historical effective tax rate ({raw_tax_rate:.0%}) was out of a sane range — clipped to {tax_rate_avg:.0%}")
        else:
            tax_rate_avg = 0.30
            warnings.append("Tax Provision / Pretax Income had no overlapping non-zero years — effective tax rate defaulted to 30%")
    else:
        tax_rate_avg = 0.30
        warnings.append("Tax Provision and/or Pretax Income not found — effective tax rate defaulted to 30%")

    # Revenue CAGR — guards against a negative endpoint making the fractional
    # power undefined (a real bug seen in an earlier version of this kind of
    # calc: (negative/positive)**(1/n) is NaN, not a real number).
    if len(revenue) >= 2 and revenue.iloc[0] > 0 and revenue.iloc[-1] > 0:
        years_span = len(revenue) - 1
        revenue_cagr = float((revenue.iloc[0] / revenue.iloc[-1]) ** (1 / years_span) - 1)
        revenue_cagr = float(np.clip(revenue_cagr, -0.05, 0.20))
    else:
        revenue_cagr = 0.03
        warnings.append(
            "Could not compute a revenue CAGR (need positive revenue at both the start and end of the "
            "history window) — defaulted Year 1 revenue growth to 3%"
        )

    latest_ebitda = (latest_ebit + latest_da) if (latest_ebit is not None and latest_da is not None) else None
    if latest_ebitda is None:
        warnings.append("Could not compute latest actual EBITDA (EBIT + D&A) — the default Exit Multiple will need a manual EBITDA figure")

    n_years = int(min(len(revenue), max_years))
    if n_years < 3:
        warnings.append(f"Only {n_years} year(s) of historical data available — forecast assumption defaults are based on a short window")

    return HistoricalFinancials(
        n_years=n_years, revenue=revenue, ebit=ebit if ebit is not None else pd.Series(dtype=float),
        ebit_margin_avg=ebit_margin_avg if ebit_margin_avg is not None else 0.10,
        da_pct_avg=da_pct_avg, capex_pct_avg=capex_pct_avg, nwc_pct_avg=nwc_pct_avg,
        tax_rate_avg=tax_rate_avg, revenue_cagr=revenue_cagr,
        latest_revenue=float(revenue.iloc[0]), latest_ebit=latest_ebit, latest_da=latest_da,
        latest_ebitda=latest_ebitda, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Base-year overrides — used when yfinance's income statement is missing
# Revenue and/or EBIT, or the user simply wants to substitute their own
# figure. Pure/dataclass-based so the dashboard's override-handling logic
# stays testable without network or Streamlit.
# ---------------------------------------------------------------------------

def historical_financials_from_revenue_override(revenue_override: float, ebit_override: float | None = None) -> HistoricalFinancials:
    """
    Builds a minimal HistoricalFinancials when yfinance had no usable
    revenue at all (extract_historical_financials() raised
    InsufficientDataError) and the user has supplied a base-year Revenue
    override instead. Ratio assumptions fall back to generic defaults —
    there's no real history to average — flagged with a warning so the user
    knows to review every row of the forecast table, not just trust it.
    """
    if revenue_override is None or revenue_override <= 0:
        raise InvalidInputError("Base-year Revenue override must be a positive number")

    ebit_margin = (ebit_override / revenue_override) if ebit_override else 0.10
    latest_ebit = ebit_override if ebit_override else None
    warnings = [
        "No usable revenue history was found from yfinance — using the manual base-year Revenue override. "
        "EBIT margin / CapEx / D&A / ΔNWC % assumptions are generic defaults (no real history to average) — "
        "review and edit every row of the forecast table."
    ]
    return HistoricalFinancials(
        n_years=0, revenue=pd.Series([revenue_override]), ebit=pd.Series([latest_ebit] if latest_ebit else []),
        ebit_margin_avg=ebit_margin, da_pct_avg=0.0, capex_pct_avg=0.0, nwc_pct_avg=0.0,
        tax_rate_avg=0.30, revenue_cagr=0.03,
        latest_revenue=revenue_override, latest_ebit=latest_ebit, latest_da=None,
        latest_ebitda=latest_ebit, warnings=warnings,
    )


def override_latest_ebit(hist: HistoricalFinancials, manual_ebit: float) -> HistoricalFinancials:
    """Recomputes ebit_margin_avg and latest_ebitda from a manually-supplied latest-year EBIT, keeping D&A/CapEx/ΔNWC/tax as auto-derived. Returns a new HistoricalFinancials (the input is left unmodified)."""
    if not hist.latest_revenue:
        raise InvalidInputError("Cannot apply an EBIT override without a base-year revenue figure")
    new_margin = manual_ebit / hist.latest_revenue
    new_ebitda = manual_ebit + hist.latest_da if hist.latest_da is not None else manual_ebit
    return replace(
        hist, ebit_margin_avg=new_margin, latest_ebit=manual_ebit, latest_ebitda=new_ebitda,
        warnings=hist.warnings + ["EBIT margin and latest EBITDA recomputed from the manual base-year EBIT override"],
    )


# ---------------------------------------------------------------------------
# Forecast assumptions table (the editable per-year grid)
# ---------------------------------------------------------------------------

FORECAST_COLUMNS = ["year", "revenue_growth_pct", "ebit_margin_pct", "capex_pct_of_revenue", "da_pct_of_revenue", "nwc_pct_of_revenue"]


def growth_glide_path(growth_1_pct: float, terminal_growth_pct: float, n_years: int) -> list[float]:
    """Linear taper from Year 1's growth rate down to the terminal growth rate by the final forecast year. All inputs/outputs in percentage points."""
    if n_years <= 1:
        return [growth_1_pct][:n_years]
    return [growth_1_pct - (growth_1_pct - terminal_growth_pct) * t / (n_years - 1) for t in range(n_years)]


def build_default_forecast_assumptions(hist: HistoricalFinancials, n_years: int, terminal_growth_pct: float) -> pd.DataFrame:
    """Defaults per the spec: Year 1 growth = historical revenue CAGR, tapering linearly to the terminal growth rate; every margin flat at its historical average. Every cell is still individually editable afterward."""
    growth_1_pct = hist.revenue_cagr * 100
    growth_path = growth_glide_path(growth_1_pct, terminal_growth_pct, n_years)
    return pd.DataFrame({
        "year": list(range(1, n_years + 1)),
        "revenue_growth_pct": growth_path,
        "ebit_margin_pct": [hist.ebit_margin_avg * 100] * n_years,
        "capex_pct_of_revenue": [hist.capex_pct_avg * 100] * n_years,
        "da_pct_of_revenue": [hist.da_pct_avg * 100] * n_years,
        "nwc_pct_of_revenue": [hist.nwc_pct_avg * 100] * n_years,
    })


# ---------------------------------------------------------------------------
# FCF projection — the core per-year build-up, with a full audit trail
# ---------------------------------------------------------------------------

def compute_fcf_projection(assumptions: pd.DataFrame, base_revenue: float, tax_rate: float, wacc: float) -> pd.DataFrame:
    """
    assumptions: a DataFrame shaped like build_default_forecast_assumptions()'s
    output (percentages, one row per forecast year, in order).
    tax_rate, wacc: fractions (0.30, not 30).
    Returns a DataFrame with every intermediate figure (revenue, EBIT, D&A,
    CapEx, ΔNWC, FCF, EBITDA, discount factor, PV of FCF) — the audit trail.
    """
    if assumptions.empty:
        raise InvalidInputError("Forecast assumptions table is empty — need at least one forecast year")

    rows = []
    revenue_prev = base_revenue
    for i, r in enumerate(assumptions.itertuples(index=False), start=1):
        growth = r.revenue_growth_pct / 100.0
        ebit_margin = r.ebit_margin_pct / 100.0
        capex_pct = r.capex_pct_of_revenue / 100.0
        da_pct = r.da_pct_of_revenue / 100.0
        nwc_pct = r.nwc_pct_of_revenue / 100.0

        revenue = revenue_prev * (1 + growth)
        ebit = revenue * ebit_margin
        da = da_pct * revenue
        capex = capex_pct * revenue
        nwc = nwc_pct * revenue
        fcf = ebit * (1 - tax_rate) + da - capex - nwc
        ebitda = ebit + da
        discount_factor = 1.0 / (1.0 + wacc) ** i
        pv_fcf = fcf * discount_factor

        rows.append(dict(
            year=i, revenue=revenue, revenue_growth_pct=r.revenue_growth_pct, ebit_margin_pct=r.ebit_margin_pct,
            ebit=ebit, da=da, capex=capex, nwc=nwc, ebitda=ebitda, fcf=fcf,
            discount_factor=discount_factor, pv_fcf=pv_fcf,
        ))
        revenue_prev = revenue

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# WACC
# ---------------------------------------------------------------------------

@dataclass
class WaccResult:
    cost_of_equity: float
    cost_of_debt: float
    cost_of_debt_method: str   # "interest_expense_over_debt" or "risk_free_plus_2pct"
    weight_of_equity: float
    weight_of_debt: float
    wacc: float
    warnings: list[str] = field(default_factory=list)


def compute_wacc(
    risk_free_rate: float, beta: float, equity_risk_premium: float,
    interest_expense: float | None, total_debt: float, market_cap: float, tax_rate: float,
) -> WaccResult:
    warnings: list[str] = []
    cost_of_equity = risk_free_rate + beta * equity_risk_premium

    if interest_expense is not None and total_debt and total_debt > 0:
        cost_of_debt = abs(interest_expense) / total_debt
        method = "interest_expense_over_debt"
    else:
        cost_of_debt = risk_free_rate + 0.02
        method = "risk_free_plus_2pct"
        warnings.append("Interest expense missing or total debt is zero — cost of debt defaulted to risk-free rate + 2%")

    denom = market_cap + total_debt
    if denom <= 0:
        weight_of_equity, weight_of_debt = 1.0, 0.0
        warnings.append("Market cap + total debt is zero or negative — WACC used 100% cost of equity")
    else:
        weight_of_equity = market_cap / denom
        weight_of_debt = 1.0 - weight_of_equity

    wacc = weight_of_equity * cost_of_equity + weight_of_debt * cost_of_debt * (1 - tax_rate)

    return WaccResult(
        cost_of_equity=cost_of_equity, cost_of_debt=cost_of_debt, cost_of_debt_method=method,
        weight_of_equity=weight_of_equity, weight_of_debt=weight_of_debt, wacc=wacc, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Terminal value
# ---------------------------------------------------------------------------

@dataclass
class TerminalValueResult:
    tv_perpetuity: float
    tv_exit_multiple: float
    blended_tv: float
    pv_terminal_value: float
    implied_exit_multiple_from_perpetuity: float | None
    implied_perpetuity_growth_from_exit: float | None


def compute_terminal_value(
    fcf_final: float, ebitda_final: float, n_years: int,
    wacc: float, terminal_growth: float, exit_multiple: float, perpetuity_weight: float,
) -> TerminalValueResult:
    """
    wacc, terminal_growth, perpetuity_weight: fractions. exit_multiple: a raw multiple (e.g. 8.0, not 800%).

    fcf_final / ebitda_final MUST be the FINAL FORECAST YEAR's figures (FCF_N,
    EBIT_N + D&A_N — i.e. the last row of compute_fcf_projection()'s output),
    not the latest historical/actual figures. Latest historical EBITDA is a
    separate number, used only by default_exit_multiple() to derive a
    starting-point exit multiple — passing it here instead of the forecast
    year's EBITDA silently overstates the exit-multiple terminal value.
    """
    if terminal_growth >= wacc:
        raise TerminalGrowthExceedsWaccError(
            f"Terminal growth rate ({terminal_growth:.1%}) must be less than WACC ({wacc:.1%}) — the perpetuity-growth "
            "formula divides by (WACC - growth), which blows up or goes negative otherwise. Lower the terminal growth "
            "rate or raise WACC."
        )

    tv_perpetuity = fcf_final * (1 + terminal_growth) / (wacc - terminal_growth)
    tv_exit_multiple = ebitda_final * exit_multiple
    blended_tv = perpetuity_weight * tv_perpetuity + (1 - perpetuity_weight) * tv_exit_multiple
    pv_terminal_value = blended_tv / (1 + wacc) ** n_years

    implied_exit_multiple = (tv_perpetuity / ebitda_final) if ebitda_final else None

    denom = fcf_final + tv_exit_multiple
    if denom != 0:
        implied_growth = (tv_exit_multiple * wacc - fcf_final) / denom
    else:
        implied_growth = None

    return TerminalValueResult(
        tv_perpetuity=tv_perpetuity, tv_exit_multiple=tv_exit_multiple, blended_tv=blended_tv,
        pv_terminal_value=pv_terminal_value, implied_exit_multiple_from_perpetuity=implied_exit_multiple,
        implied_perpetuity_growth_from_exit=implied_growth,
    )


def default_exit_multiple(market_cap: float, total_debt: float, cash: float, latest_historical_ebitda: float | None) -> tuple[float | None, list[str]]:
    warnings: list[str] = []
    if not latest_historical_ebitda or latest_historical_ebitda == 0:
        warnings.append("Latest historical EBITDA unavailable or zero — could not derive a default exit multiple; enter one manually")
        return None, warnings
    multiple = (market_cap + total_debt - cash) / latest_historical_ebitda
    if multiple < 0:
        warnings.append(f"Implied default exit multiple ({multiple:.1f}x) is negative — EV or EBITDA is likely unreliable for this company; check it before trusting the terminal value")
    elif multiple > 40:
        warnings.append(f"Implied default exit multiple ({multiple:.1f}x) is unusually high (>40x) — check it before trusting the terminal value")
    return multiple, warnings


# ---------------------------------------------------------------------------
# Valuation bridge
# ---------------------------------------------------------------------------

@dataclass
class ValuationBridge:
    pv_fcf_sum: float
    pv_terminal_value: float
    enterprise_value: float
    net_debt: float
    equity_value: float
    shares_outstanding: float
    value_per_share: float
    current_price: float | None
    upside_downside: float | None
    is_extreme: bool


def compute_valuation_bridge(
    pv_fcf_sum: float, pv_terminal_value: float, total_debt: float, cash: float,
    shares_outstanding: float, current_price: float | None,
    net_debt_override: float | None = None, shares_override: float | None = None,
) -> ValuationBridge:
    enterprise_value = pv_fcf_sum + pv_terminal_value
    net_debt = net_debt_override if net_debt_override is not None else (total_debt - cash)
    equity_value = enterprise_value - net_debt

    shares = shares_override if shares_override is not None else shares_outstanding
    if not shares or shares <= 0:
        raise InvalidInputError("Shares outstanding is zero or missing — supply a shares-outstanding override to compute a per-share value")
    value_per_share = equity_value / shares

    upside_downside = None
    is_extreme = False
    if current_price and current_price > 0:
        upside_downside = value_per_share / current_price - 1
        is_extreme = abs(upside_downside) > 1.0

    return ValuationBridge(
        pv_fcf_sum=pv_fcf_sum, pv_terminal_value=pv_terminal_value, enterprise_value=enterprise_value,
        net_debt=net_debt, equity_value=equity_value, shares_outstanding=shares, value_per_share=value_per_share,
        current_price=current_price, upside_downside=upside_downside, is_extreme=is_extreme,
    )


# ---------------------------------------------------------------------------
# Sensitivity grid (WACC x terminal growth, perpetuity method)
# ---------------------------------------------------------------------------

def sensitivity_grid(
    fcf_series: list[float], ebit_margin_last_pct: float,  # noqa: ARG001 (kept for API symmetry/future use)
    net_debt: float, shares_outstanding: float,
    wacc_values: list[float], terminal_growth_values: list[float],
) -> pd.DataFrame:
    """
    Value per share across a WACC (rows) x terminal growth (columns) grid,
    using the PERPETUITY method only (re-discounting the same undiscounted
    FCF series at each WACC, since the explicit-period PV also depends on
    WACC, not just the terminal value). Cells where terminal growth >= WACC
    are left as NaN rather than raising, so the grid renders cleanly around
    the infeasible region.
    """
    n = len(fcf_series)
    out = pd.DataFrame(index=[f"{w:.1%}" for w in wacc_values], columns=[f"{g:.1%}" for g in terminal_growth_values], dtype=float)

    for wacc in wacc_values:
        discount_factors = [1.0 / (1.0 + wacc) ** t for t in range(1, n + 1)]
        pv_fcf_sum = sum(fcf * df for fcf, df in zip(fcf_series, discount_factors))
        for g in terminal_growth_values:
            row_label, col_label = f"{wacc:.1%}", f"{g:.1%}"
            if g >= wacc or not fcf_series:
                out.loc[row_label, col_label] = np.nan
                continue
            tv = fcf_series[-1] * (1 + g) / (wacc - g)
            pv_tv = tv / (1 + wacc) ** n
            equity_value = pv_fcf_sum + pv_tv - net_debt
            out.loc[row_label, col_label] = equity_value / shares_outstanding if shares_outstanding else np.nan

    return out
