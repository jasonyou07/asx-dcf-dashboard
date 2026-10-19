"""
Full-app smoke tests using Streamlit's AppTest, with yfinance.Ticker mocked
to return realistic synthetic data (this sandbox can't reach Yahoo
Finance's live API either, per the project brief). Confirms the app runs
cleanly end-to-end: the base case, each manual override, the Excel export
button, and an invalid-ticker path — no uncaught exceptions anywhere.
"""
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import yfinance as yf
from streamlit.testing.v1 import AppTest

DASHBOARD_PATH = str(Path(__file__).resolve().parent.parent / "dcf_dashboard.py")
DASHBOARD_PATH_TEXT = Path(DASHBOARD_PATH).read_text(encoding="utf-8")


def _mock_statement(data: dict) -> pd.DataFrame:
    columns = pd.to_datetime(["2025-06-30", "2024-06-30", "2023-06-30", "2022-06-30"])
    return pd.DataFrame(data, index=columns).T


def make_mock_ticker(
    info: dict | None = None,
    financials: pd.DataFrame | None = None,
    cashflow: pd.DataFrame | None = None,
    balance_sheet: pd.DataFrame | None = None,
):
    """Builds a MagicMock standing in for yf.Ticker(symbol), shaped like a realistic company."""
    default_info = {
        "currentPrice": 42.0, "marketCap": 50_000.0, "beta": 1.05, "sharesOutstanding": 1000.0,
        "longName": "Mock Industries Ltd", "totalDebt": 8_000.0, "totalCash": 3_000.0,
    }
    default_financials = _mock_statement({
        "Total Revenue": [20_000.0, 18_000.0, 16_500.0, 15_000.0],
        "EBIT": [3_000.0, 2_600.0, 2_300.0, 2_000.0],
        "Interest Expense": [400.0, 380.0, 360.0, 340.0],
        "Tax Provision": [700.0, 600.0, 530.0, 460.0],
        "Pretax Income": [2_700.0, 2_300.0, 2_000.0, 1_750.0],
    })
    default_cashflow = _mock_statement({
        "Depreciation And Amortization": [800.0, 720.0, 660.0, 600.0],
        "Capital Expenditure": [-1_200.0, -1_080.0, -990.0, -900.0],
        "Change In Working Capital": [-200.0, -160.0, -100.0, -80.0],
    })
    default_balance_sheet = _mock_statement({
        "Total Debt": [8_000.0, 8_200.0, 8_400.0, 8_600.0],
        "Cash And Cash Equivalents": [3_000.0, 2_600.0, 2_200.0, 1_800.0],
    })

    mock = MagicMock()
    mock.info = info if info is not None else default_info
    mock.financials = financials if financials is not None else default_financials
    mock.cashflow = cashflow if cashflow is not None else default_cashflow
    mock.balance_sheet = balance_sheet if balance_sheet is not None else default_balance_sheet
    return mock


def enable_override(at: AppTest, checkbox_substr: str, number_label: str, value: float) -> None:
    """Checks an 'Override ...' checkbox (now in the main body's Overrides
    expander, not the sidebar), reruns so Streamlit un-disables its paired
    number_input (disabled-state only updates after a script rerun — a
    browser user would see the same two-step behavior), then sets the value."""
    found_checkbox = False
    for cb in at.checkbox:
        if checkbox_substr in (cb.label or ""):
            cb.set_value(True)
            found_checkbox = True
    assert found_checkbox, f"no checkbox found matching {checkbox_substr!r}"
    at.run()
    found_input = False
    for ni in at.number_input:
        if number_label == ni.label:
            ni.set_value(value)
            found_input = True
    assert found_input, f"no number_input found with label {number_label!r}"
    at.run()


def run_app_with_ticker(ticker_symbol: str, mock_ticker=None, widget_setup=None) -> AppTest:
    """Launches dcf_dashboard.py under AppTest, types a ticker, clicks
    Fetch & Run (this renders every section and its own widgets, since each
    section's sliders/checkboxes now live inside that section rather than a
    sidebar that renders unconditionally), then optionally applies
    widget_setup (which should use enable_override() and may call at.run()
    itself as needed) and reruns so the adjusted values take effect."""
    at = AppTest.from_file(DASHBOARD_PATH)
    with patch("yfinance.Ticker", return_value=mock_ticker or make_mock_ticker()):
        at.run()
        at.text_input[0].set_value(ticker_symbol)
        at.button[0].click()
        at.run()
        if widget_setup:
            widget_setup(at)
            at.run()
    return at


class TestBaseCase:
    def test_runs_without_exception(self):
        at = run_app_with_ticker("MOCKBASE")
        assert at.exception == []

    def test_shows_company_name_and_valuation(self):
        at = run_app_with_ticker("MOCKBASE2")
        assert at.exception == []
        body_text = "\n".join(m.value for m in at.main if hasattr(m, "value") and isinstance(m.value, str))
        assert "Mock Industries" in body_text or any("Mock Industries" in str(getattr(el, "value", "")) for el in at.main)


class TestHistoryYears:
    def test_slider_present_and_adjustable(self):
        def setup(at):
            found = False
            for s in at.slider:
                if s.label == "History Years":
                    s.set_value(2)
                    found = True
            assert found, "History Years slider not found"
        at = run_app_with_ticker("MOCKHIST2", widget_setup=setup)
        assert at.exception == []

    def test_narrow_history_window_surfaces_short_window_warning(self):
        """With History Years=1, extract_historical_financials should report
        n_years=1 and the dashboard should surface its 'short window' warning."""
        def setup(at):
            for s in at.slider:
                if s.label == "History Years":
                    s.set_value(1)
        at = run_app_with_ticker("MOCKHIST1", widget_setup=setup)
        assert at.exception == []
        body_text = "\n".join(str(getattr(el, "value", "")) for el in at.main)
        assert "Only 1 year" in body_text

    def test_changing_history_years_changes_forecast_defaults(self):
        """A narrower history window changes the averaged ratios feeding the
        default forecast table — the state_key must include history_years so
        the cached table actually regenerates instead of silently going stale."""
        def set_history(at, years):
            for s in at.slider:
                if s.label == "History Years":
                    s.set_value(years)
            at.run()

        at = AppTest.from_file(DASHBOARD_PATH)
        with patch("yfinance.Ticker", return_value=make_mock_ticker()):
            at.run()
            at.text_input[0].set_value("MOCKHISTDIFF")
            at.button[0].click()
            at.run()  # fetch completes; History Years slider (default 5) now exists

            set_history(at, 4)
            full_history_first_growth = at.dataframe[0].value["revenue_growth_pct"].iloc[0]

            set_history(at, 1)
            short_history_first_growth = at.dataframe[0].value["revenue_growth_pct"].iloc[0]

        assert at.exception == []
        assert full_history_first_growth != pytest.approx(short_history_first_growth)


class TestOverrides:
    def test_beta_override_runs_without_exception(self):
        at = run_app_with_ticker("MOCKBETA", widget_setup=lambda at: enable_override(at, "Override Beta", "Beta override", 1.5))
        assert at.exception == []

    def test_exit_multiple_override_runs_without_exception(self):
        at = run_app_with_ticker("MOCKEXIT", widget_setup=lambda at: enable_override(at, "Override Exit Multiple", "Exit Multiple (x EBITDA)", 10.0))
        assert at.exception == []

    def test_net_debt_override_runs_without_exception(self):
        at = run_app_with_ticker("MOCKNETDEBT", widget_setup=lambda at: enable_override(at, "Override Net Debt", "Net Debt override", 5000.0))
        assert at.exception == []

    def test_shares_outstanding_override_runs_without_exception(self):
        at = run_app_with_ticker("MOCKSHARES", widget_setup=lambda at: enable_override(at, "Override Shares Outstanding", "Shares Outstanding override", 1200.0))
        assert at.exception == []

    def test_base_year_revenue_override_runs_without_exception(self):
        mock = make_mock_ticker(financials=_mock_statement({"EBIT": [100.0]}))  # no revenue at all
        at = run_app_with_ticker(
            "MOCKNOREV", mock_ticker=mock,
            widget_setup=lambda at: enable_override(at, "Override base-year Revenue", "Base-year Revenue override", 25_000.0),
        )
        assert at.exception == []

    def test_base_year_ebit_override_runs_without_exception(self):
        at = run_app_with_ticker("MOCKEBIT", widget_setup=lambda at: enable_override(at, "Override base-year EBIT", "Base-year EBIT override", 3500.0))
        assert at.exception == []

    def test_all_overrides_together_run_without_exception(self):
        def setup(at):
            enable_override(at, "Override Beta", "Beta override", 1.3)
            enable_override(at, "Override Exit Multiple", "Exit Multiple (x EBITDA)", 9.0)
            enable_override(at, "Override Net Debt", "Net Debt override", 4000.0)
            enable_override(at, "Override Shares Outstanding", "Shares Outstanding override", 1100.0)
        at = run_app_with_ticker("MOCKALL", widget_setup=setup)
        assert at.exception == []


class TestInvalidTicker:
    def test_empty_info_shows_error_not_exception(self):
        mock = make_mock_ticker(info={}, financials=pd.DataFrame(), cashflow=pd.DataFrame(), balance_sheet=pd.DataFrame())
        at = run_app_with_ticker("ZZZZINVALID", mock_ticker=mock)
        assert at.exception == []
        assert len(at.error) > 0

    def test_network_exception_shows_error_not_crash(self):
        at = AppTest.from_file(DASHBOARD_PATH)
        with patch("yfinance.Ticker", side_effect=ConnectionError("simulated network failure")):
            at.run()
            at.text_input[0].set_value("MOCKNETERR")
            at.button[0].click()
            at.run()
        assert at.exception == []
        assert len(at.error) > 0


class TestRateLimitHandling:
    """Regression guard: Yahoo rate-limiting (or any yfinance-side failure)
    must surface as a plain-English st.error, never a raw traceback, and the
    message must actually say something about rate-limiting — not just a
    generic 'couldn't fetch' with no explanation of why."""

    def test_yf_rate_limit_error_shows_friendly_message(self):
        at = AppTest.from_file(DASHBOARD_PATH)
        with patch("yfinance.Ticker", side_effect=yf.exceptions.YFRateLimitError()):
            at.run()
            at.text_input[0].set_value("MOCKRATELIMIT")
            at.button[0].click()
            at.run()
        assert at.exception == []
        assert len(at.error) > 0
        assert "rate-limit" in at.error[0].value.lower()

    def test_http_429_in_exception_message_shows_friendly_rate_limit_message(self):
        """Some yfinance code paths let a raw HTTP error through instead of
        raising their own typed exception — a '429' in the message should
        still be recognized and treated the same way."""
        at = AppTest.from_file(DASHBOARD_PATH)
        with patch("yfinance.Ticker", side_effect=RuntimeError("HTTP Error 429: Too Many Requests")):
            at.run()
            at.text_input[0].set_value("MOCK429")
            at.button[0].click()
            at.run()
        assert at.exception == []
        assert len(at.error) > 0
        assert "rate-limit" in at.error[0].value.lower()

    def test_other_yfinance_exception_shows_friendly_message_not_traceback(self):
        at = AppTest.from_file(DASHBOARD_PATH)
        with patch("yfinance.Ticker", side_effect=yf.exceptions.YFTickerMissingError("MOCKYFEXC", "no data found")):
            at.run()
            at.text_input[0].set_value("MOCKYFEXC")
            at.button[0].click()
            at.run()
        assert at.exception == []
        assert len(at.error) > 0
        error_text = at.error[0].value
        assert "Traceback" not in error_text
        assert "Yahoo Finance" in error_text

    def test_fetch_is_cached_with_a_few_hours_ttl(self):
        """FETCH_CACHE_TTL_SECONDS should be 'a few hours', not the old 1-hour
        default — read straight from source so this doesn't need a live session."""
        source = DASHBOARD_PATH_TEXT
        match = re.search(r"FETCH_CACHE_TTL_SECONDS\s*=\s*(.+)", source)
        assert match, "FETCH_CACHE_TTL_SECONDS constant not found in dcf_dashboard.py"
        ttl_seconds = eval(match.group(1).split("#")[0].strip())  # trusted: our own source file
        assert 2 * 3600 <= ttl_seconds <= 6 * 3600, f"expected a 'few hours' TTL (2-6h), got {ttl_seconds}s"

        assert "ttl=FETCH_CACHE_TTL_SECONDS" in source, "fetch_ticker_data's @st.cache_data should use the named TTL constant"


class TestExcelExportButton:
    def test_download_button_renders_without_exception(self):
        at = run_app_with_ticker("MOCKEXCEL")
        assert at.exception == []
        assert len(at.download_button) >= 1
        assert "Download formula-driven Excel model" in at.download_button[0].label
