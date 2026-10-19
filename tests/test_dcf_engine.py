"""
Unit tests for dcf_engine.py — pure functions, synthetic yfinance-shaped
data, no network access needed anywhere in this file.
"""
import numpy as np
import pandas as pd
import pytest

import dcf_engine as eng
from tests.conftest import mock_statement


# ---------------------------------------------------------------------------
# Statement-row helpers
# ---------------------------------------------------------------------------

class TestRowHelpers:
    def test_row_first_candidate_match(self):
        df = mock_statement({"EBIT": [100.0, 90.0], "Operating Income": [95.0, 85.0]})
        row = eng._row(df, ["EBIT", "Operating Income"])
        assert list(row.values) == [100.0, 90.0]

    def test_row_falls_through_candidates(self):
        df = mock_statement({"Operating Income": [95.0, 85.0]})
        row = eng._row(df, ["EBIT", "Operating Income"])
        assert list(row.values) == [95.0, 85.0]

    def test_row_none_if_no_match(self):
        df = mock_statement({"Something Else": [1.0, 2.0]})
        assert eng._row(df, ["EBIT"]) is None

    def test_row_none_for_none_or_empty(self):
        assert eng._row(None, ["EBIT"]) is None
        assert eng._row(pd.DataFrame(), ["EBIT"]) is None

    def test_row_skips_all_nan_candidate(self):
        df = mock_statement({"EBIT": [np.nan, np.nan], "Operating Income": [10.0, 20.0]})
        row = eng._row(df, ["EBIT", "Operating Income"])
        assert list(row.values) == [10.0, 20.0]

    def test_latest_returns_first_value(self):
        df = mock_statement({"EBIT": [100.0, 90.0]})
        assert eng._latest(df, ["EBIT"]) == 100.0

    def test_latest_default_when_missing(self):
        assert eng._latest(None, ["EBIT"], default=42.0) == 42.0


# ---------------------------------------------------------------------------
# Company snapshot
# ---------------------------------------------------------------------------

class TestCompanySnapshot:
    def test_healthy_case(self, healthy_info, healthy_balance_sheet):
        snap = eng.extract_company_snapshot(healthy_info, healthy_balance_sheet)
        assert snap.current_price == 12.5
        assert snap.market_cap == 5000.0
        assert snap.beta == 1.1
        assert snap.shares_outstanding == 400.0
        assert snap.total_debt == 300.0
        assert snap.cash == 150.0
        assert snap.name == "Test Company Ltd"
        assert snap.warnings == []

    def test_missing_beta_defaults_to_one(self, healthy_balance_sheet):
        snap = eng.extract_company_snapshot({"currentPrice": 1.0, "marketCap": 100.0, "sharesOutstanding": 50.0}, healthy_balance_sheet)
        assert snap.beta == 1.0
        assert any("Beta" in w for w in snap.warnings)

    def test_shares_outstanding_derived_from_marketcap_and_price(self, healthy_balance_sheet):
        snap = eng.extract_company_snapshot({"currentPrice": 10.0, "marketCap": 1000.0, "beta": 1.0}, healthy_balance_sheet)
        assert snap.shares_outstanding == 100.0
        assert any("derived from market cap" in w for w in snap.warnings)

    def test_total_debt_falls_back_to_balance_sheet(self, healthy_balance_sheet):
        snap = eng.extract_company_snapshot({"beta": 1.0}, healthy_balance_sheet)
        assert snap.total_debt == 300.0
        assert any("balance sheet" in w for w in snap.warnings)

    def test_total_debt_defaults_to_zero_when_totally_missing(self):
        snap = eng.extract_company_snapshot({"beta": 1.0}, None)
        assert snap.total_debt == 0.0
        assert any("defaulted to 0" in w for w in snap.warnings)

    def test_cash_defaults_to_zero_when_totally_missing(self):
        snap = eng.extract_company_snapshot({"beta": 1.0}, None)
        assert snap.cash == 0.0


# ---------------------------------------------------------------------------
# Historical financials
# ---------------------------------------------------------------------------

class TestExtractHistoricalFinancials:
    def test_healthy_case_ratios(self, healthy_financials, healthy_cashflow, healthy_balance_sheet):
        hist = eng.extract_historical_financials(healthy_financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.n_years == 4
        assert hist.latest_revenue == 1000.0
        assert hist.latest_ebit == 150.0
        assert hist.latest_da == 40.0
        assert hist.latest_ebitda == 190.0
        # EBIT margin avg = mean(150/1000, 130/900, 115/820, 100/750)
        expected_margin = np.mean([150/1000, 130/900, 115/820, 100/750])
        assert hist.ebit_margin_avg == pytest.approx(expected_margin)
        # tax rate avg = mean(36/140, 31/122, 27/108, 24/94)
        expected_tax = np.mean([36/140, 31/122, 27/108, 24/94])
        assert hist.tax_rate_avg == pytest.approx(expected_tax)
        assert hist.warnings == []

    def test_nwc_sign_convention_cashflow_source(self, healthy_financials, healthy_cashflow, healthy_balance_sheet):
        """yfinance 'Change In Working Capital' is positive=cash-inflow; the
        engine must flip it so a positive nwc_pct means NWC grew (cash use, subtracted in FCF)."""
        hist = eng.extract_historical_financials(healthy_financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.nwc_pct_avg > 0  # cashflow rows were negative (-10,-8,-5,-4) -> NWC increased -> positive after flip

    def test_missing_ebit_defaults_with_warning(self, healthy_cashflow, healthy_balance_sheet):
        financials = mock_statement({"Total Revenue": [1000.0, 900.0, 820.0, 750.0]})
        hist = eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.ebit_margin_avg == 0.10
        assert hist.latest_ebit is None
        assert any("EBIT/Operating Income not found" in w for w in hist.warnings)

    def test_missing_da_and_capex_default_to_zero_pct(self, healthy_financials, healthy_balance_sheet):
        cashflow = mock_statement({"Change In Working Capital": [-10.0, -8.0, -5.0, -4.0]})
        hist = eng.extract_historical_financials(healthy_financials, cashflow, healthy_balance_sheet)
        assert hist.da_pct_avg == 0.0
        assert hist.capex_pct_avg == 0.0
        assert any("D&A not found" in w for w in hist.warnings)
        assert any("CapEx not found" in w for w in hist.warnings)

    def test_nwc_falls_back_to_balance_sheet_current_assets_minus_liabilities(self, healthy_financials, healthy_balance_sheet):
        cashflow = mock_statement({  # no "Change In Working Capital" row at all
            "Depreciation And Amortization": [40.0, 36.0, 33.0, 30.0],
            "Capital Expenditure": [-60.0, -54.0, -50.0, -45.0],
        })
        balance_sheet = mock_statement({
            "Total Debt": [300.0, 310.0, 320.0, 330.0],
            "Cash And Cash Equivalents": [150.0, 130.0, 110.0, 90.0],
            "Current Assets": [500.0, 460.0, 420.0, 380.0],
            "Current Liabilities": [300.0, 280.0, 260.0, 240.0],
        })
        hist = eng.extract_historical_financials(healthy_financials, cashflow, balance_sheet)
        assert any("derived from the balance sheet" in w for w in hist.warnings)
        # NWC = CA - CL: 200, 180, 160, 140 (most-recent-first) -> increases of 20,20,20 across periods
        # increase_t = NWC_t - NWC_{t+1(older)}: (200-180)=20, (180-160)=20, (160-140)=20
        expected_nwc_pct = np.mean([20/1000, 20/900, 20/820])
        assert hist.nwc_pct_avg == pytest.approx(expected_nwc_pct)

    def test_nwc_defaults_to_zero_when_nothing_available(self, healthy_financials):
        cashflow = mock_statement({"Depreciation And Amortization": [40.0, 36.0, 33.0, 30.0]})
        hist = eng.extract_historical_financials(healthy_financials, cashflow, None)
        assert hist.nwc_pct_avg == 0.0
        assert any("Current Assets/Current Liabilities not found" in w for w in hist.warnings)

    def test_missing_revenue_raises(self, healthy_cashflow, healthy_balance_sheet):
        financials = mock_statement({"EBIT": [100.0, 90.0]})
        with pytest.raises(eng.InsufficientDataError):
            eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet)

    def test_none_statements_raise(self):
        with pytest.raises(eng.InsufficientDataError):
            eng.extract_historical_financials(None, None, None)

    def test_revenue_cagr_undefined_when_negative_endpoint_defaults_safely(self, healthy_cashflow, healthy_balance_sheet):
        """Regression guard: a negative endpoint makes (recent/oldest)**(1/n) undefined (NaN in real
        arithmetic) — must default cleanly instead of propagating NaN."""
        financials = mock_statement({
            "Total Revenue": [-50.0, 900.0, 820.0, 750.0],  # most recent year negative
            "EBIT": [10.0, 130.0, 115.0, 100.0],
        })
        hist = eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.revenue_cagr == 0.03
        assert np.isfinite(hist.revenue_cagr)
        assert any("Could not compute a revenue CAGR" in w for w in hist.warnings)

    def test_revenue_cagr_clipped_to_sane_range(self, healthy_cashflow, healthy_balance_sheet):
        financials = mock_statement({
            "Total Revenue": [10000.0, 100.0],  # absurd 100x growth over 1 year
            "EBIT": [1000.0, 10.0],
        }, columns=pd.to_datetime(["2025-06-30", "2024-06-30"]))
        hist = eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.revenue_cagr <= 0.20

    def test_tax_rate_clipped_to_sane_range(self, healthy_cashflow, healthy_balance_sheet):
        financials = mock_statement({
            "Total Revenue": [1000.0, 900.0],
            "EBIT": [150.0, 130.0],
            "Tax Provision": [90.0, 80.0],     # 90% effective tax rate - implausible
            "Pretax Income": [100.0, 90.0],
        }, columns=pd.to_datetime(["2025-06-30", "2024-06-30"]))
        hist = eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.tax_rate_avg == 0.40
        assert any("clipped" in w for w in hist.warnings)

    def test_single_year_of_history(self, healthy_cashflow, healthy_balance_sheet):
        financials = mock_statement({"Total Revenue": [1000.0], "EBIT": [150.0]}, columns=pd.to_datetime(["2025-06-30"]))
        hist = eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet)
        assert hist.n_years == 1
        assert hist.revenue_cagr == 0.03  # can't compute a CAGR from one year
        assert any("Only 1 year" in w for w in hist.warnings)

    def test_max_years_window_respected(self, healthy_cashflow, healthy_balance_sheet):
        financials = mock_statement({
            "Total Revenue": [1000.0, 900.0, 820.0, 750.0, 700.0],
            "EBIT": [150.0, 130.0, 115.0, 100.0, 90.0],
        })
        hist = eng.extract_historical_financials(financials, healthy_cashflow, healthy_balance_sheet, max_years=2)
        assert hist.n_years == 2


# ---------------------------------------------------------------------------
# Base-year overrides
# ---------------------------------------------------------------------------

class TestBaseYearOverrides:
    def test_revenue_override_builds_minimal_history(self):
        hist = eng.historical_financials_from_revenue_override(500.0, ebit_override=75.0)
        assert hist.latest_revenue == 500.0
        assert hist.n_years == 0
        assert hist.ebit_margin_avg == pytest.approx(0.15)
        assert hist.latest_ebitda == 75.0
        assert hist.warnings

    def test_revenue_override_without_ebit_uses_generic_margin(self):
        hist = eng.historical_financials_from_revenue_override(500.0)
        assert hist.ebit_margin_avg == 0.10

    def test_revenue_override_rejects_non_positive(self):
        with pytest.raises(eng.InvalidInputError):
            eng.historical_financials_from_revenue_override(0.0)
        with pytest.raises(eng.InvalidInputError):
            eng.historical_financials_from_revenue_override(-10.0)

    def test_ebit_override_recomputes_margin_and_ebitda(self, healthy_financials, healthy_cashflow, healthy_balance_sheet):
        hist = eng.extract_historical_financials(healthy_financials, healthy_cashflow, healthy_balance_sheet)
        updated = eng.override_latest_ebit(hist, manual_ebit=200.0)
        assert updated.ebit_margin_avg == pytest.approx(200.0 / hist.latest_revenue)
        assert updated.latest_ebitda == pytest.approx(200.0 + hist.latest_da)
        assert updated.latest_ebit == 200.0
        assert hist.ebit_margin_avg != updated.ebit_margin_avg  # original untouched

    def test_ebit_override_requires_base_revenue(self):
        hist = eng.HistoricalFinancials(
            n_years=0, revenue=pd.Series(dtype=float), ebit=pd.Series(dtype=float),
            ebit_margin_avg=0.1, da_pct_avg=0, capex_pct_avg=0, nwc_pct_avg=0, tax_rate_avg=0.3,
            revenue_cagr=0.03, latest_revenue=None, latest_ebit=None, latest_da=None, latest_ebitda=None,
        )
        with pytest.raises(eng.InvalidInputError):
            eng.override_latest_ebit(hist, 100.0)


# ---------------------------------------------------------------------------
# Growth glide path / forecast assumptions
# ---------------------------------------------------------------------------

class TestGrowthGlidePath:
    def test_single_year_returns_growth_1_only(self):
        assert eng.growth_glide_path(10.0, 2.5, 1) == [10.0]

    def test_linear_taper_endpoints(self):
        path = eng.growth_glide_path(10.0, 2.0, 5)
        assert path[0] == pytest.approx(10.0)
        assert path[-1] == pytest.approx(2.0)
        assert len(path) == 5

    def test_linear_taper_is_evenly_spaced(self):
        path = eng.growth_glide_path(10.0, 2.0, 5)
        diffs = [path[i] - path[i + 1] for i in range(len(path) - 1)]
        assert all(d == pytest.approx(diffs[0]) for d in diffs)

    def test_flat_path_when_growth_equals_terminal(self):
        path = eng.growth_glide_path(3.0, 3.0, 4)
        assert all(p == pytest.approx(3.0) for p in path)


class TestBuildDefaultForecastAssumptions:
    def test_shape_and_defaults(self, healthy_financials, healthy_cashflow, healthy_balance_sheet):
        hist = eng.extract_historical_financials(healthy_financials, healthy_cashflow, healthy_balance_sheet)
        df = eng.build_default_forecast_assumptions(hist, n_years=5, terminal_growth_pct=2.5)
        assert list(df["year"]) == [1, 2, 3, 4, 5]
        assert df["revenue_growth_pct"].iloc[0] == pytest.approx(hist.revenue_cagr * 100)
        assert df["revenue_growth_pct"].iloc[-1] == pytest.approx(2.5)
        assert (df["ebit_margin_pct"] == hist.ebit_margin_avg * 100).all()
        assert list(df.columns) == eng.FORECAST_COLUMNS


# ---------------------------------------------------------------------------
# FCF projection
# ---------------------------------------------------------------------------

class TestComputeFcfProjection:
    def test_manual_two_year_calc(self):
        assumptions = pd.DataFrame({
            "year": [1, 2], "revenue_growth_pct": [10.0, 5.0], "ebit_margin_pct": [15.0, 15.0],
            "capex_pct_of_revenue": [6.0, 6.0], "da_pct_of_revenue": [4.0, 4.0], "nwc_pct_of_revenue": [1.0, 1.0],
        })
        proj = eng.compute_fcf_projection(assumptions, base_revenue=1000.0, tax_rate=0.30, wacc=0.10)

        rev1 = 1000 * 1.10
        ebit1 = rev1 * 0.15
        da1 = rev1 * 0.04
        capex1 = rev1 * 0.06
        nwc1 = rev1 * 0.01
        fcf1 = ebit1 * (1 - 0.30) + da1 - capex1 - nwc1
        assert proj["revenue"].iloc[0] == pytest.approx(rev1)
        assert proj["fcf"].iloc[0] == pytest.approx(fcf1)
        assert proj["discount_factor"].iloc[0] == pytest.approx(1 / 1.10)
        assert proj["pv_fcf"].iloc[0] == pytest.approx(fcf1 / 1.10)

        rev2 = rev1 * 1.05
        assert proj["revenue"].iloc[1] == pytest.approx(rev2)
        assert proj["discount_factor"].iloc[1] == pytest.approx(1 / 1.10 ** 2)

    def test_empty_assumptions_raises(self):
        with pytest.raises(eng.InvalidInputError):
            eng.compute_fcf_projection(pd.DataFrame(), 1000.0, 0.30, 0.10)

    def test_discount_factors_strictly_decreasing(self):
        assumptions = pd.DataFrame({
            "year": range(1, 6), "revenue_growth_pct": [5.0] * 5, "ebit_margin_pct": [15.0] * 5,
            "capex_pct_of_revenue": [6.0] * 5, "da_pct_of_revenue": [4.0] * 5, "nwc_pct_of_revenue": [1.0] * 5,
        })
        proj = eng.compute_fcf_projection(assumptions, 1000.0, 0.30, 0.10)
        factors = list(proj["discount_factor"])
        assert factors == sorted(factors, reverse=True)

    def test_ebitda_equals_ebit_plus_da(self):
        assumptions = pd.DataFrame({
            "year": [1], "revenue_growth_pct": [5.0], "ebit_margin_pct": [15.0],
            "capex_pct_of_revenue": [6.0], "da_pct_of_revenue": [4.0], "nwc_pct_of_revenue": [1.0],
        })
        proj = eng.compute_fcf_projection(assumptions, 1000.0, 0.30, 0.10)
        assert proj["ebitda"].iloc[0] == pytest.approx(proj["ebit"].iloc[0] + proj["da"].iloc[0])


# ---------------------------------------------------------------------------
# WACC
# ---------------------------------------------------------------------------

class TestComputeWacc:
    def test_normal_case_matches_formula(self):
        res = eng.compute_wacc(0.043, beta=1.1, equity_risk_premium=0.055, interest_expense=15.0, total_debt=300.0, market_cap=5000.0, tax_rate=0.25)
        expected_coe = 0.043 + 1.1 * 0.055
        expected_cod = 15.0 / 300.0
        expected_we = 5000.0 / 5300.0
        expected_wacc = expected_we * expected_coe + (1 - expected_we) * expected_cod * 0.75
        assert res.cost_of_equity == pytest.approx(expected_coe)
        assert res.cost_of_debt == pytest.approx(expected_cod)
        assert res.wacc == pytest.approx(expected_wacc)
        assert res.cost_of_debt_method == "interest_expense_over_debt"
        assert res.warnings == []

    def test_cost_of_debt_fallback_when_interest_expense_missing(self):
        res = eng.compute_wacc(0.043, 1.0, 0.055, interest_expense=None, total_debt=300.0, market_cap=1000.0, tax_rate=0.30)
        assert res.cost_of_debt == pytest.approx(0.043 + 0.02)
        assert res.cost_of_debt_method == "risk_free_plus_2pct"
        assert any("defaulted to risk-free rate + 2%" in w for w in res.warnings)

    def test_cost_of_debt_fallback_when_total_debt_zero(self):
        res = eng.compute_wacc(0.043, 1.0, 0.055, interest_expense=15.0, total_debt=0.0, market_cap=1000.0, tax_rate=0.30)
        assert res.cost_of_debt_method == "risk_free_plus_2pct"

    def test_zero_market_cap_and_debt_falls_back_to_full_equity_weight(self):
        res = eng.compute_wacc(0.043, 1.0, 0.055, interest_expense=None, total_debt=0.0, market_cap=0.0, tax_rate=0.30)
        assert res.weight_of_equity == 1.0
        assert res.weight_of_debt == 0.0
        assert any("100% cost of equity" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# Terminal value
# ---------------------------------------------------------------------------

class TestComputeTerminalValue:
    def test_normal_blend(self):
        tv = eng.compute_terminal_value(fcf_final=100.0, ebitda_final=150.0, n_years=5, wacc=0.10, terminal_growth=0.03, exit_multiple=8.0, perpetuity_weight=0.5)
        expected_perp = 100.0 * 1.03 / (0.10 - 0.03)
        expected_exit = 150.0 * 8.0
        expected_blend = 0.5 * expected_perp + 0.5 * expected_exit
        assert tv.tv_perpetuity == pytest.approx(expected_perp)
        assert tv.tv_exit_multiple == pytest.approx(expected_exit)
        assert tv.blended_tv == pytest.approx(expected_blend)
        assert tv.pv_terminal_value == pytest.approx(expected_blend / 1.10 ** 5)

    def test_weight_one_matches_pure_perpetuity(self):
        tv = eng.compute_terminal_value(100.0, 150.0, 5, 0.10, 0.03, 8.0, perpetuity_weight=1.0)
        assert tv.blended_tv == pytest.approx(tv.tv_perpetuity)

    def test_weight_zero_matches_pure_exit_multiple(self):
        tv = eng.compute_terminal_value(100.0, 150.0, 5, 0.10, 0.03, 8.0, perpetuity_weight=0.0)
        assert tv.blended_tv == pytest.approx(tv.tv_exit_multiple)

    def test_terminal_growth_equal_to_wacc_raises(self):
        with pytest.raises(eng.TerminalGrowthExceedsWaccError):
            eng.compute_terminal_value(100.0, 150.0, 5, wacc=0.08, terminal_growth=0.08, exit_multiple=8.0, perpetuity_weight=0.5)

    def test_terminal_growth_exceeds_wacc_raises(self):
        with pytest.raises(eng.TerminalGrowthExceedsWaccError):
            eng.compute_terminal_value(100.0, 150.0, 5, wacc=0.05, terminal_growth=0.06, exit_multiple=8.0, perpetuity_weight=0.5)

    def test_implied_exit_multiple_cross_check(self):
        tv = eng.compute_terminal_value(100.0, 150.0, 5, 0.10, 0.03, 8.0, 0.5)
        assert tv.implied_exit_multiple_from_perpetuity == pytest.approx(tv.tv_perpetuity / 150.0)

    def test_implied_perpetuity_growth_cross_check_round_trips(self):
        """Solve g from TV_exit, then re-derive TV_perpetuity at that g — should recover TV_exit."""
        tv = eng.compute_terminal_value(100.0, 150.0, 5, 0.10, 0.03, 8.0, 0.5)
        g = tv.implied_perpetuity_growth_from_exit
        recovered_tv = 100.0 * (1 + g) / (0.10 - g)
        assert recovered_tv == pytest.approx(tv.tv_exit_multiple)

    def test_implied_exit_multiple_none_when_ebitda_zero(self):
        tv = eng.compute_terminal_value(100.0, 0.0, 5, 0.10, 0.03, 8.0, 0.5)
        assert tv.implied_exit_multiple_from_perpetuity is None


class TestDefaultExitMultiple:
    def test_normal_case(self):
        multiple, warnings = eng.default_exit_multiple(market_cap=1000.0, total_debt=300.0, cash=100.0, latest_historical_ebitda=150.0)
        assert multiple == pytest.approx((1000.0 + 300.0 - 100.0) / 150.0)
        assert warnings == []

    def test_zero_ebitda_returns_none_with_warning(self):
        multiple, warnings = eng.default_exit_multiple(1000.0, 300.0, 100.0, 0.0)
        assert multiple is None
        assert warnings

    def test_none_ebitda_returns_none_with_warning(self):
        multiple, warnings = eng.default_exit_multiple(1000.0, 300.0, 100.0, None)
        assert multiple is None

    def test_negative_multiple_warns(self):
        multiple, warnings = eng.default_exit_multiple(market_cap=100.0, total_debt=50.0, cash=500.0, latest_historical_ebitda=50.0)
        assert multiple < 0
        assert any("negative" in w for w in warnings)

    def test_extremely_high_multiple_warns(self):
        multiple, warnings = eng.default_exit_multiple(market_cap=10000.0, total_debt=0.0, cash=0.0, latest_historical_ebitda=10.0)
        assert multiple > 40
        assert any("unusually high" in w for w in warnings)


# ---------------------------------------------------------------------------
# Valuation bridge
# ---------------------------------------------------------------------------

class TestComputeValuationBridge:
    def test_normal_case(self):
        bridge = eng.compute_valuation_bridge(pv_fcf_sum=500.0, pv_terminal_value=1500.0, total_debt=300.0, cash=100.0, shares_outstanding=400.0, current_price=4.0)
        assert bridge.enterprise_value == pytest.approx(2000.0)
        assert bridge.net_debt == pytest.approx(200.0)
        assert bridge.equity_value == pytest.approx(1800.0)
        assert bridge.value_per_share == pytest.approx(4.5)
        assert bridge.upside_downside == pytest.approx(0.125)
        assert bridge.is_extreme is False

    def test_zero_shares_outstanding_raises(self):
        with pytest.raises(eng.InvalidInputError):
            eng.compute_valuation_bridge(500.0, 1500.0, 300.0, 100.0, shares_outstanding=0.0, current_price=4.0)

    def test_none_shares_outstanding_raises(self):
        with pytest.raises(eng.InvalidInputError):
            eng.compute_valuation_bridge(500.0, 1500.0, 300.0, 100.0, shares_outstanding=None, current_price=4.0)

    def test_net_debt_override_applied(self):
        bridge = eng.compute_valuation_bridge(500.0, 1500.0, 300.0, 100.0, 400.0, 4.0, net_debt_override=50.0)
        assert bridge.net_debt == 50.0
        assert bridge.equity_value == pytest.approx(2000.0 - 50.0)

    def test_shares_override_applied(self):
        bridge = eng.compute_valuation_bridge(500.0, 1500.0, 300.0, 100.0, 400.0, 4.0, shares_override=200.0)
        assert bridge.shares_outstanding == 200.0
        assert bridge.value_per_share == pytest.approx(1800.0 / 200.0)

    def test_no_current_price_upside_is_none(self):
        bridge = eng.compute_valuation_bridge(500.0, 1500.0, 300.0, 100.0, 400.0, current_price=None)
        assert bridge.upside_downside is None
        assert bridge.is_extreme is False

    def test_extreme_upside_flagged(self):
        bridge = eng.compute_valuation_bridge(pv_fcf_sum=5000.0, pv_terminal_value=15000.0, total_debt=0.0, cash=0.0, shares_outstanding=400.0, current_price=1.0)
        assert bridge.upside_downside > 1.0
        assert bridge.is_extreme is True

    def test_extreme_downside_flagged(self):
        # heavy net debt relative to enterprise value drives equity value negative -> downside < -100%
        bridge = eng.compute_valuation_bridge(pv_fcf_sum=10.0, pv_terminal_value=10.0, total_debt=1000.0, cash=0.0, shares_outstanding=400.0, current_price=100.0)
        assert bridge.value_per_share < 0
        assert bridge.upside_downside < -1.0
        assert bridge.is_extreme is True


# ---------------------------------------------------------------------------
# Sensitivity grid
# ---------------------------------------------------------------------------

class TestSensitivityGrid:
    def test_shape(self):
        grid = eng.sensitivity_grid([100, 110, 121], 15.0, net_debt=200.0, shares_outstanding=400.0,
                                     wacc_values=[0.08, 0.09, 0.10], terminal_growth_values=[0.01, 0.02, 0.03])
        assert grid.shape == (3, 3)

    def test_infeasible_cells_are_nan(self):
        grid = eng.sensitivity_grid([100, 110], 15.0, net_debt=0.0, shares_outstanding=100.0,
                                     wacc_values=[0.05], terminal_growth_values=[0.05, 0.06])
        assert grid.isna().all().all()  # terminal growth >= the only wacc value everywhere

    def test_center_cell_matches_manual_calc(self):
        fcf_series = [100.0, 110.0]
        wacc, g = 0.10, 0.03
        grid = eng.sensitivity_grid(fcf_series, 15.0, net_debt=50.0, shares_outstanding=100.0,
                                     wacc_values=[wacc], terminal_growth_values=[g])
        pv_fcf = 100 / 1.10 + 110 / 1.10 ** 2
        tv = 110 * 1.03 / (0.10 - 0.03)
        pv_tv = tv / 1.10 ** 2
        expected_value_per_share = (pv_fcf + pv_tv - 50.0) / 100.0
        assert grid.iloc[0, 0] == pytest.approx(expected_value_per_share)

    def test_empty_fcf_series_does_not_crash(self):
        grid = eng.sensitivity_grid([], 15.0, net_debt=0.0, shares_outstanding=100.0, wacc_values=[0.10], terminal_growth_values=[0.03])
        assert grid.isna().all().all()
