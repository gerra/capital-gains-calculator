"""Securities named with --exempt-securities are listed but never charged."""

from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cgt_calc.currency_converter import CurrencyConverter
from cgt_calc.current_price_fetcher import CurrentPriceFetcher
from cgt_calc.initial_prices import InitialPrices
from cgt_calc.isin_converter import IsinConverter
from cgt_calc.main import CapitalGainsCalculator
from cgt_calc.model import ActionType, BrokerTransaction, Isin, RuleType
from cgt_calc.render_latex import render_pdf
from cgt_calc.spin_off_handler import SpinOffHandler

from .calc_test_data import GBP, transaction

GILT = "TN28"
GILT_ISIN = Isin("GB00BMBL1G81")
BUY_DAY = datetime.date(2024, 6, 15)
SELL_DAY = datetime.date(2024, 8, 25)


def calculator(exempt_securities: list[str]) -> CapitalGainsCalculator:
    """Build a calculator for 2024/25 with the given exempt list."""
    currency_converter = CurrencyConverter.create(Path("tests/exchange_rates_data.csv"))
    return CapitalGainsCalculator(
        2024,
        currency_converter,
        IsinConverter(),
        CurrentPriceFetcher(currency_converter, {}, {}),
        SpinOffHandler(),
        InitialPrices(),
        interest_fund_tickers=[],
        balance_check=False,
        exempt_securities=exempt_securities,
    )


def gilt_round_trip(isin: Isin | None = None) -> list[BrokerTransaction]:
    """Buy 100 units of a gilt at 0.94 and sell them at 0.95: a £1 gain."""
    return [
        transaction(BUY_DAY, ActionType.BUY, GILT, 100, 0.94, 0, -94, GBP, isin=isin),
        transaction(SELL_DAY, ActionType.SELL, GILT, 100, 0.95, 0, 95, GBP, isin=isin),
    ]


def share_round_trip() -> list[BrokerTransaction]:
    """Buy 10 shares at 10 and sell them at 12: a £20 gain."""
    return [
        transaction(BUY_DAY, ActionType.BUY, "FOO", 10, 10, 0, -100, GBP),
        transaction(SELL_DAY, ActionType.SELL, "FOO", 10, 12, 0, 120, GBP),
    ]


def test_exempt_disposal_is_listed_but_not_charged() -> None:
    """The gilt sale appears in the log as exempt and adds nothing to the totals."""
    calc = calculator([GILT])
    calc.convert_to_hmrc_transactions(gilt_round_trip() + share_round_trip())
    report = calc.calculate_capital_gain()

    assert report.disposal_count == 1
    assert report.disposal_proceeds == Decimal(120)
    assert report.capital_gain == Decimal(20)
    assert report.total_gain() == Decimal(20)
    assert set(report.calculation_log[SELL_DAY]) == {"sell$FOO", f"exempt${GILT}"}
    [entry] = report.calculation_log[SELL_DAY][f"exempt${GILT}"]
    assert entry.rule_type is RuleType.SECTION_104
    assert entry.gain == Decimal(1)
    assert report.exempt_disposal_count() == 1
    assert report.exempt_disposal_proceeds() == Decimal(95)
    assert all(
        entry.quantity == 0 for entry in report.portfolio if entry.symbol == GILT
    )


def test_exempt_security_matched_by_isin() -> None:
    """An ISIN in the list exempts the ticker the rows carry."""
    calc = calculator([GILT_ISIN.lower()])
    calc.convert_to_hmrc_transactions(gilt_round_trip(isin=GILT_ISIN))
    report = calc.calculate_capital_gain()

    assert report.disposal_count == 0
    assert report.exempt_disposal_count() == 1


def test_without_the_list_the_gilt_is_charged() -> None:
    """Nothing is exempt unless named."""
    calc = calculator([])
    calc.convert_to_hmrc_transactions(gilt_round_trip())
    report = calc.calculate_capital_gain()

    assert report.disposal_count == 1
    assert report.capital_gain == Decimal(1)
    assert report.exempt_disposal_count() == 0


def test_summary_names_exempt_disposals() -> None:
    """The console summary counts exempt disposals only when there are some."""
    calc = calculator([GILT])
    calc.convert_to_hmrc_transactions(gilt_round_trip())
    summary = str(calc.calculate_capital_gain())

    assert "Exempt disposals" in summary
    assert "1 (£95.00, not chargeable)" in summary

    calc = calculator([])
    calc.convert_to_hmrc_transactions(share_round_trip())
    assert "Exempt disposals" not in str(calc.calculate_capital_gain())


def test_report_marks_exempt_disposal(tmp_path: Path) -> None:
    """The rendered report lists the disposal as exempt with its gain."""
    calc = calculator([GILT])
    calc.convert_to_hmrc_transactions(gilt_round_trip())
    report = calc.calculate_capital_gain()
    render_pdf(report, tmp_path / "report.pdf", skip_pdflatex=True)
    source = (tmp_path / "report.tex").read_text(encoding="utf-8")

    assert f"Exempt disposal 1: 100 units of {GILT} for £95.00" in source
    assert "gain of £1.00" in source
    assert "Exempt disposals, not chargeable: 1 (£95.00 proceeds)" in source
    assert "Number of disposals: 0" in source


@pytest.mark.parametrize("name", [GILT, GILT_ISIN])
def test_exempt_names_are_case_insensitive(name: str) -> None:
    """Tickers and ISINs match however they were typed."""
    calc = calculator([name.lower()])
    calc.convert_to_hmrc_transactions(gilt_round_trip(isin=GILT_ISIN))

    assert calc.exempt_symbols == {GILT}
