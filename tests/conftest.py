"""Shared fixtures — synthetic, yfinance-shaped data. No network access required anywhere in this suite."""
import pandas as pd
import pytest


def mock_statement(data: dict, columns=None) -> pd.DataFrame:
    """Builds a yfinance-shaped statement DataFrame: rows = line items, columns = period dates (most-recent first)."""
    if columns is None:
        n = len(next(iter(data.values())))
        columns = pd.to_datetime(["2025-06-30", "2024-06-30", "2023-06-30", "2022-06-30", "2021-06-30"][:n])
    return pd.DataFrame(data, index=columns).T


@pytest.fixture
def healthy_financials():
    return mock_statement({
        "Total Revenue": [1000.0, 900.0, 820.0, 750.0],
        "EBIT": [150.0, 130.0, 115.0, 100.0],
        "Tax Provision": [36.0, 31.0, 27.0, 24.0],
        "Pretax Income": [140.0, 122.0, 108.0, 94.0],
    })


@pytest.fixture
def healthy_cashflow():
    return mock_statement({
        "Depreciation And Amortization": [40.0, 36.0, 33.0, 30.0],
        "Capital Expenditure": [-60.0, -54.0, -50.0, -45.0],
        "Change In Working Capital": [-10.0, -8.0, -5.0, -4.0],
    })


@pytest.fixture
def healthy_balance_sheet():
    return mock_statement({
        "Total Debt": [300.0, 310.0, 320.0, 330.0],
        "Cash And Cash Equivalents": [150.0, 130.0, 110.0, 90.0],
    })


@pytest.fixture
def healthy_info():
    return {
        "currentPrice": 12.5, "marketCap": 5000.0, "beta": 1.1, "sharesOutstanding": 400.0,
        "longName": "Test Company Ltd", "totalDebt": 300.0, "totalCash": 150.0,
    }
