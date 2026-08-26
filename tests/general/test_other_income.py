"""Other income (REIT distributions, share-lending fees) is reported on its own."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from cgt_calc.model import ActionType, RuleType
from cgt_calc.render_latex import render_pdf

from .calc_test_data import GBP, transaction
from .test_calc import create_calculator, get_report

if TYPE_CHECKING:
    from pathlib import Path

PID_DAY = datetime.date(2026, 5, 8)
FEE_DAY = datetime.date(2026, 8, 17)
LAST_YEAR = datetime.date(2025, 4, 11)

TRANSACTIONS = [
    # A property income distribution: £80.64 gross, £16.13 withheld.
    transaction(PID_DAY, ActionType.OTHER_INCOME, "PHP", amount=80.64, currency=GBP),
    transaction(
        PID_DAY, ActionType.OTHER_INCOME_TAX, "PHP", amount=-16.13, currency=GBP
    ),
    # A share-lending fee, with no instrument attached.
    transaction(FEE_DAY, ActionType.OTHER_INCOME, None, amount=0.12, currency=GBP),
    # Last year's distribution: not this year's income.
    transaction(LAST_YEAR, ActionType.OTHER_INCOME, "LAND", amount=88.63, currency=GBP),
    transaction(
        LAST_YEAR, ActionType.OTHER_INCOME_TAX, "LAND", amount=-17.73, currency=GBP
    ),
]


def test_other_income_totals_and_log() -> None:
    """Gross income and the tax taken off are totalled for the year only."""
    report = get_report(
        create_calculator(tax_year=2026, balance_check=False), TRANSACTIONS
    )

    assert report.total_other_income == Decimal("80.76")
    assert report.total_other_income_tax == Decimal("16.13")
    assert report.total_uk_interest == Decimal(0)
    assert report.total_dividends_amount() == Decimal(0)

    [pid] = report.calculation_log_yields[PID_DAY]["otherIncome$PHP"]
    assert pid.rule_type is RuleType.OTHER_INCOME
    assert pid.amount == Decimal("80.64")
    [tax] = report.calculation_log_yields[PID_DAY]["otherIncomeTax$PHP"]
    assert tax.rule_type is RuleType.OTHER_INCOME_TAX
    assert tax.amount == Decimal("16.13")
    [fee] = report.calculation_log_yields[FEE_DAY]["otherIncome$Testing"]
    assert fee.amount == Decimal("0.12")
    assert LAST_YEAR not in report.calculation_log_yields


def test_summary_shows_other_income_only_when_present() -> None:
    """The console summary gains an Other income group when there is some."""
    summary = str(
        get_report(create_calculator(tax_year=2026, balance_check=False), TRANSACTIONS)
    )
    assert "Other income" in summary
    assert "£80.76" in summary
    assert "£16.13" in summary

    summary = str(
        get_report(create_calculator(tax_year=2024, balance_check=False), TRANSACTIONS)
    )
    assert "Other income" not in summary


def test_report_lists_other_income(tmp_path: Path) -> None:
    """The rendered report names each payment and totals them."""
    report = get_report(
        create_calculator(tax_year=2026, balance_check=False), TRANSACTIONS
    )
    render_pdf(report, tmp_path / "report.pdf", skip_pdflatex=True)
    source = (tmp_path / "report.tex").read_text(encoding="utf-8")

    assert "Other income 1:} PHP for £80.64" in source
    assert "Tax on other income 1:} PHP for £16.13" in source
    assert "Other income 2:} Testing for £0.12" in source
    assert "Total amount of other income: £80.76" in source
    assert "Total amount of tax taken off other income: £16.13" in source
