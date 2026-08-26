"""Test Freetrade support."""

import csv
from datetime import date
from decimal import Decimal
import logging
from pathlib import Path
import subprocess

import pytest

from cgt_calc.exceptions import (
    ParsingError,
    UnsupportedBrokerActionError,
    UnsupportedBrokerCurrencyError,
)
from cgt_calc.model import ActionType, BrokerTransaction
from cgt_calc.parsers.freetrade import (
    COLUMNS,
    FreetradeColumn,
    FreetradeParser,
    FreetradeTransaction,
    _with_tbill_redemptions,
)
from tests.utils import build_cmd, report_path, stderr_alerts

# Header of a Freetrade export downloaded after the format change reported in
# issue #800: two columns were renamed and the "Stock Split" ones are new.
NEW_COLUMNS: list[str] = [
    "Title",
    "Type",
    "Timestamp",
    "Account Currency",
    "Total Amount in Account Currency",
    "Buy / Sell",
    "Ticker",
    "ISIN",
    "Price per Share in Account Currency",
    "Stamp Duty",
    "Quantity",
    "Venue",
    "Order ID",
    "Order Type",
    "Instrument Currency",
    "Total Amount in Instrument Currency",
    "Price per Share",
    "FX Rate",
    "Base FX Rate",
    "FX Fee (BPS)",
    "FX Fee Amount",
    "Dividend Ex Date",
    "Dividend Pay Date",
    "Dividend Eligible Quantity",
    "Dividend Amount Per Share",
    "Dividend Gross Distribution Amount",
    "Dividend Net Distribution Amount",
    "Dividend Withheld Tax Percentage",
    "Dividend Withheld Tax Amount",
    "Stock Split Ex Date",
    "Stock Split Pay Date",
    "Stock Split New ISIN",
    "Stock Split Rate of Share Outturn From",
    "Stock Split Rate of Share Outturn To",
    "Stock Split Maintain Holding of Initial ISIN",
    "Stock Split New Share Quantity",
    "Stock Split Rate of Cash Outturn Amount",
    "Stock Split Rate of Cash Outturn Currency",
    "Stock Split Cash Outturn Received Amount",
    "Stock Split Has Fractional Payout",
    "Stock Split Rate of Fractional Payout Amount",
    "Stock Split Rate of Fractional Payout Currency",
    "Stock Split Fractional Payout Cash Received Amount",
    "Stock Split Fractional Payout Cash Received Currency",
]

BASE_ROW_VALUES = {
    FreetradeColumn.TITLE.value: "Buy Apple",
    FreetradeColumn.TYPE.value: "ORDER",
    FreetradeColumn.TIMESTAMP.value: "2024-01-01T10:00:00",
    FreetradeColumn.ACCOUNT_CURRENCY.value: "GBP",
    FreetradeColumn.TOTAL_AMOUNT.value: "100",
    FreetradeColumn.BUY_SELL.value: "BUY",
    FreetradeColumn.TICKER.value: "AAPL",
    FreetradeColumn.ISIN.value: "US0378331005",
    FreetradeColumn.PRICE_PER_SHARE_ACCOUNT.value: "100",
    FreetradeColumn.STAMP_DUTY.value: "0",
    FreetradeColumn.QUANTITY.value: "1",
    FreetradeColumn.VENUE.value: "",
    FreetradeColumn.ORDER_ID.value: "123",
    FreetradeColumn.ORDER_TYPE.value: "MARKET",
    FreetradeColumn.INSTRUMENT_CURRENCY.value: "GBP",
    FreetradeColumn.TOTAL_SHARES_AMOUNT.value: "100",
    FreetradeColumn.PRICE_PER_SHARE.value: "100",
    FreetradeColumn.FX_RATE.value: "1",
    FreetradeColumn.BASE_FX_RATE.value: "1",
    FreetradeColumn.FX_FEE_BPS.value: "0",
    FreetradeColumn.FX_FEE_AMOUNT.value: "0",
    FreetradeColumn.DIVIDEND_EX_DATE.value: "",
    FreetradeColumn.DIVIDEND_PAY_DATE.value: "",
    FreetradeColumn.DIVIDEND_ELIGIBLE_QUANTITY.value: "",
    FreetradeColumn.DIVIDEND_AMOUNT_PER_SHARE.value: "0",
    FreetradeColumn.DIVIDEND_GROSS_AMOUNT.value: "0",
    FreetradeColumn.DIVIDEND_NET_AMOUNT.value: "0",
    FreetradeColumn.DIVIDEND_WITHHELD_PERCENTAGE.value: "0",
    FreetradeColumn.DIVIDEND_WITHHELD_AMOUNT.value: "0",
}

# The two columns Freetrade renamed, spelled out rather than read back from the
# parser so that a wrong mapping there fails these tests.
RENAMED_COLUMNS: dict[str, str] = {
    FreetradeColumn.TOTAL_AMOUNT.value: "Total Amount in Account Currency",
    FreetradeColumn.TOTAL_SHARES_AMOUNT.value: "Total Amount in Instrument Currency",
}


def _default_row(overrides: dict[str, str] | None = None) -> list[str]:
    """Return default row data with optional overrides."""
    values = BASE_ROW_VALUES.copy()
    if overrides:
        values.update(overrides)
    return [values[column] for column in COLUMNS]


def _default_new_row(overrides: dict[str, str] | None = None) -> list[str]:
    """Return default row data laid out for the new header."""
    values = {
        RENAMED_COLUMNS.get(column, column): value
        for column, value in BASE_ROW_VALUES.items()
    }
    if overrides:
        values.update(overrides)
    return [values.get(column, "") for column in NEW_COLUMNS]


def _write_csv(
    tmp_path: Path, header: list[str], rows: list[list[str]] | None = None
) -> Path:
    """Write CSV file with provided header and rows."""
    target = tmp_path / "freetrade.csv"
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        if rows:
            writer.writerows(rows)
    return target


def test_run_with_freetrade_file(request: pytest.FixtureRequest) -> None:
    """Runs the script and verifies it doesn't fail."""
    cmd = build_cmd(
        "--year",
        "2023",
        "--freetrade-file",
        "tests/freetrade/data/transactions.csv",
        "--output",
        report_path(request),
    )
    result = subprocess.run(cmd, capture_output=True, encoding="utf-8", check=False)
    if result.returncode:
        pytest.fail(
            "Integration test failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    assert stderr_alerts(result.stderr) == [], "Run with example files generated errors"
    expected_file = Path("tests") / "freetrade" / "data" / "expected_output.txt"
    expected = expected_file.read_text(encoding="utf-8")
    cmd_str = " ".join([param or "''" for param in cmd])
    assert result.stdout == expected, (
        "Run with example files generated unexpected outputs, "
        "if you added new features update the test with:\n"
        f"{cmd_str} > {expected_file}"
    )


def test_read_freetrade_transactions_empty_file(tmp_path: Path) -> None:
    """Ensure parser raises when CSV is empty."""
    empty_file = tmp_path / "empty.csv"
    empty_file.write_text("")

    with pytest.raises(ParsingError):
        FreetradeParser().load_from_file(empty_file)


def test_read_freetrade_transactions_missing_column(tmp_path: Path) -> None:
    """Missing required columns trigger ParsingError."""
    header = COLUMNS[:-1]
    path = _write_csv(tmp_path, header)

    with pytest.raises(ParsingError, match="Missing columns"):
        FreetradeParser().load_from_file(path)


def test_read_freetrade_transactions_unknown_column(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unknown columns are tolerated with a warning, not rejected."""
    header = [*COLUMNS, "Unexpected"]
    path = _write_csv(tmp_path, header)

    with caplog.at_level(logging.WARNING, logger="cgt_calc.parsers.freetrade"):
        FreetradeParser().load_from_file(path)

    assert "Unknown columns" in caplog.text
    assert "Unexpected" in caplog.text


def test_read_freetrade_transactions_invalid_decimal(tmp_path: Path) -> None:
    """Invalid decimal values surface as ParsingError with row context."""
    overrides = {FreetradeColumn.QUANTITY.value: "not-a-number"}
    path = _write_csv(tmp_path, COLUMNS, [_default_row(overrides)])

    with pytest.raises(
        ParsingError,
        match=", row 2: Invalid decimal in column 'Quantity'",
    ):
        FreetradeParser().load_from_file(path)


def test_read_freetrade_transactions_unsupported_currency(tmp_path: Path) -> None:
    """Non-GBP account currencies raise a dedicated error."""
    overrides = {FreetradeColumn.ACCOUNT_CURRENCY.value: "USD"}
    path = _write_csv(tmp_path, COLUMNS, [_default_row(overrides)])

    with pytest.raises(
        UnsupportedBrokerCurrencyError,
        match="parser does not support the provided account currency",
    ):
        FreetradeParser().load_from_file(path)


def test_read_freetrade_transactions_success(tmp_path: Path) -> None:
    """Default row parses into a valid BUY transaction."""
    path = _write_csv(tmp_path, COLUMNS, [_default_row()])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.action is ActionType.BUY
    assert transaction.symbol == "AAPL"
    assert transaction.quantity == Decimal(1)
    assert transaction.price == Decimal(100)
    assert transaction.fees == Decimal(0)
    assert transaction.amount == Decimal(-100)
    assert transaction.currency == "GBP"
    assert transaction.isin == "US0378331005"


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        # The tax year boundary always falls inside BST, so the last hour
        # of 5 April in UTC already belongs to the next tax year.
        ("2025-04-05T22:59:59.000Z", date(2025, 4, 5)),
        ("2025-04-05T23:00:00.000Z", date(2025, 4, 6)),
        ("2025-04-05T23:30:00.000", date(2025, 4, 6)),
        # The clocks go back on 25 October 2026, so the last hour of the
        # 26th is already GMT.
        ("2026-10-26T23:30:00.000Z", date(2026, 10, 26)),
        # Outside summer time UK dates and UTC dates agree.
        ("2026-01-15T23:30:00.000Z", date(2026, 1, 15)),
        ("2026-11-01T23:30:00.000Z", date(2026, 11, 1)),
    ],
)
def test_read_freetrade_transactions_uses_uk_dates(
    tmp_path: Path, timestamp: str, expected: date
) -> None:
    """Take the tax date from the UK calendar, not the UTC one."""
    overrides = {FreetradeColumn.TIMESTAMP.value: timestamp}
    path = _write_csv(tmp_path, COLUMNS, [_default_row(overrides)])

    transactions = FreetradeParser().load_from_file(path)

    assert transactions[0].date == expected


@pytest.mark.parametrize(
    "document_type",
    ["MONTHLY_STATEMENT", "MONTHLY_SHARE_LENDING_STATEMENT", "TAX_CERTIFICATE"],
)
def test_read_freetrade_ignores_document_rows(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    document_type: str,
) -> None:
    """Document links in an All Activity export are not transactions."""
    values = dict.fromkeys(COLUMNS, "")
    values.update(
        {
            FreetradeColumn.TITLE.value: "Account document",
            FreetradeColumn.TYPE.value: document_type,
            FreetradeColumn.TIMESTAMP.value: "2024-01-02T10:00:00",
        }
    )
    document_row = [values[column] for column in COLUMNS]
    path = _write_csv(tmp_path, COLUMNS, [_default_row(), document_row])
    caplog.set_level(logging.DEBUG, logger="cgt_calc.parsers.freetrade")

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    assert transactions[0].action is ActionType.BUY
    assert (
        f"Skipping non-transaction Freetrade row 3 ({document_type})" in caplog.messages
    )


def _row_from_values(values: dict[str, str]) -> list[str]:
    """Lay out a row with every unnamed column blank, as sparse export rows are."""
    row = dict.fromkeys(COLUMNS, "")
    row.update(values)
    return [row[column] for column in COLUMNS]


def _bond_interest_row(withheld: str = "0.00") -> list[str]:
    """Build a gilt coupon row: dividend layout, INTEREST type."""
    return _row_from_values(
        {
            FreetradeColumn.TITLE.value: "1/8% Gilt 2028",
            FreetradeColumn.TYPE.value: "INTEREST",
            FreetradeColumn.TIMESTAMP.value: "2026-07-31T15:57:00.000Z",
            FreetradeColumn.ACCOUNT_CURRENCY.value: "GBP",
            FreetradeColumn.TOTAL_AMOUNT.value: "10.62",
            FreetradeColumn.TICKER.value: "TN28",
            FreetradeColumn.ISIN.value: "GB00BMBL1G81",
            FreetradeColumn.QUANTITY.value: "17005.77000000",
            FreetradeColumn.INSTRUMENT_CURRENCY.value: "GBP",
            FreetradeColumn.DIVIDEND_EX_DATE.value: "2026-07-22",
            FreetradeColumn.DIVIDEND_PAY_DATE.value: "2026-07-31",
            FreetradeColumn.DIVIDEND_ELIGIBLE_QUANTITY.value: "17005.77000000",
            FreetradeColumn.DIVIDEND_AMOUNT_PER_SHARE.value: "0.00062500",
            FreetradeColumn.DIVIDEND_WITHHELD_PERCENTAGE.value: "0",
            FreetradeColumn.DIVIDEND_WITHHELD_AMOUNT.value: withheld,
        }
    )


def test_read_freetrade_bond_interest(tmp_path: Path) -> None:
    """A coupon on a directly held gilt is interest, not a dividend."""
    path = _write_csv(tmp_path, COLUMNS, [_bond_interest_row()])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.action is ActionType.INTEREST
    assert transaction.symbol == "TN28"
    assert transaction.isin == "GB00BMBL1G81"
    assert transaction.amount == Decimal("10.62")
    assert transaction.quantity is None
    assert transaction.price is None
    assert transaction.currency == "GBP"


def test_read_freetrade_bond_interest_with_tax_withheld_is_grossed_up(
    tmp_path: Path,
) -> None:
    """Total Amount is net of tax: the interest is rebuilt gross and the tax kept."""
    path = _write_csv(tmp_path, COLUMNS, [_bond_interest_row(withheld="2.12")])

    interest, tax = FreetradeParser().load_from_file(path)

    assert interest.action is ActionType.INTEREST
    assert interest.amount == Decimal("12.74")
    assert tax.action is ActionType.INTEREST_TAX
    assert tax.amount == Decimal("-2.12")
    assert tax.symbol == interest.symbol == "TN28"
    assert tax.isin == interest.isin
    assert tax.date == interest.date


def _property_income_row() -> list[str]:
    """Build a REIT property income distribution row: 20% tax withheld."""
    return _row_from_values(
        {
            FreetradeColumn.TITLE.value: "Primary Health",
            FreetradeColumn.TYPE.value: "PROPERTY",
            FreetradeColumn.TIMESTAMP.value: "2026-05-08T16:08:00.000Z",
            FreetradeColumn.ACCOUNT_CURRENCY.value: "GBP",
            FreetradeColumn.TOTAL_AMOUNT.value: "64.51",
            FreetradeColumn.TICKER.value: "PHP",
            FreetradeColumn.ISIN.value: "GB00BYRJ5J14",
            FreetradeColumn.QUANTITY.value: "6086.00000000",
            FreetradeColumn.INSTRUMENT_CURRENCY.value: "GBP",
            FreetradeColumn.DIVIDEND_EX_DATE.value: "2026-03-26",
            FreetradeColumn.DIVIDEND_PAY_DATE.value: "2026-05-08",
            FreetradeColumn.DIVIDEND_ELIGIBLE_QUANTITY.value: "6086.00000000",
            FreetradeColumn.DIVIDEND_AMOUNT_PER_SHARE.value: "0.01325000",
            FreetradeColumn.DIVIDEND_WITHHELD_PERCENTAGE.value: "20",
            FreetradeColumn.DIVIDEND_WITHHELD_AMOUNT.value: "16.13",
        }
    )


def test_read_freetrade_property_income_keeps_withheld_tax(tmp_path: Path) -> None:
    """A PID is booked gross, with the tax the REIT withheld alongside."""
    path = _write_csv(tmp_path, COLUMNS, [_property_income_row()])

    income, tax = FreetradeParser().load_from_file(path)

    assert income.action is ActionType.OTHER_INCOME
    assert income.amount == Decimal("80.64")
    assert income.symbol == "PHP"
    assert income.currency == "GBP"
    assert tax.action is ActionType.OTHER_INCOME_TAX
    assert tax.amount == Decimal("-16.13")
    assert tax.symbol == "PHP"
    assert tax.isin == "GB00BYRJ5J14"


def test_read_freetrade_share_lending_income(tmp_path: Path) -> None:
    """Share lending fees are other income with no instrument attached."""
    row = _row_from_values(
        {
            FreetradeColumn.TITLE.value: "Share Lending Income",
            FreetradeColumn.TYPE.value: "SHARE_LENDING_INCOME",
            FreetradeColumn.TIMESTAMP.value: "2026-08-17T15:35:09.138Z",
            FreetradeColumn.ACCOUNT_CURRENCY.value: "GBP",
            FreetradeColumn.TOTAL_AMOUNT.value: "0.12",
        }
    )
    path = _write_csv(tmp_path, COLUMNS, [row])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.action is ActionType.OTHER_INCOME
    assert transaction.symbol is None
    assert transaction.isin is None
    assert transaction.amount == Decimal("0.12")
    assert transaction.currency == "GBP"


def test_read_freetrade_skips_blank_rows(tmp_path: Path) -> None:
    """Blank and comma-only lines in an export are not transactions."""
    blank_row: list[str] = []
    empty_fields_row = [""] * len(COLUMNS)
    path = _write_csv(tmp_path, COLUMNS, [_default_row(), blank_row, empty_fields_row])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    assert transactions[0].action is ActionType.BUY


@pytest.mark.parametrize(
    ("buy_sell", "total_amount", "expected_amount"),
    [
        ("BUY", "100.75", Decimal("-100.75")),
        ("SELL", "99.25", Decimal("99.25")),
    ],
)
def test_read_freetrade_trade_uses_account_amount_and_fees(
    tmp_path: Path,
    buy_sell: str,
    total_amount: str,
    expected_amount: Decimal,
) -> None:
    """GBP cash, price, stamp duty and FX fees come from their direct fields."""
    overrides = {
        FreetradeColumn.TOTAL_AMOUNT.value: total_amount,
        FreetradeColumn.BUY_SELL.value: buy_sell,
        FreetradeColumn.PRICE_PER_SHARE_ACCOUNT.value: "100",
        FreetradeColumn.STAMP_DUTY.value: "0.50",
        FreetradeColumn.INSTRUMENT_CURRENCY.value: "USD",
        FreetradeColumn.TOTAL_SHARES_AMOUNT.value: "125",
        FreetradeColumn.PRICE_PER_SHARE.value: "125",
        FreetradeColumn.FX_RATE.value: "1.25",
        FreetradeColumn.FX_FEE_AMOUNT.value: "0.25",
    }
    path = _write_csv(tmp_path, COLUMNS, [_default_row(overrides)])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.action is (
        ActionType.BUY if buy_sell == "BUY" else ActionType.SELL
    )
    assert transaction.price == Decimal(100)
    assert transaction.fees == Decimal("0.75")
    assert transaction.amount == expected_amount
    assert transaction.currency == "GBP"


def test_read_freetrade_foreign_dividend_and_withholding_tax(
    tmp_path: Path,
) -> None:
    """Foreign dividend gross income and withholding use GBP-per-unit base FX."""
    overrides = {
        FreetradeColumn.TITLE.value: "Nasdaq",
        FreetradeColumn.TYPE.value: "DIVIDEND",
        FreetradeColumn.TOTAL_AMOUNT.value: "0.93",
        FreetradeColumn.BUY_SELL.value: "",
        FreetradeColumn.TICKER.value: "NDAQ",
        FreetradeColumn.ISIN.value: "US6311031081",
        FreetradeColumn.INSTRUMENT_CURRENCY.value: "USD",
        FreetradeColumn.BASE_FX_RATE.value: "0.78491703",
        FreetradeColumn.DIVIDEND_GROSS_AMOUNT.value: "1.40",
        FreetradeColumn.DIVIDEND_NET_AMOUNT.value: "1.19",
        FreetradeColumn.DIVIDEND_WITHHELD_PERCENTAGE.value: "15",
        FreetradeColumn.DIVIDEND_WITHHELD_AMOUNT.value: "0.21",
    }
    path = _write_csv(tmp_path, COLUMNS, [_default_row(overrides)])

    dividend, tax = FreetradeParser().load_from_file(path)

    assert dividend.action is ActionType.DIVIDEND
    assert dividend.amount == Decimal("1.40") * Decimal("0.78491703")
    assert dividend.currency == "GBP"
    assert tax.action is ActionType.DIVIDEND_TAX
    assert tax.amount == Decimal("-0.21") * Decimal("0.78491703")
    assert tax.currency == "GBP"
    assert tax.symbol == dividend.symbol
    assert tax.isin == dividend.isin


def test_read_freetrade_transactions_new_header_success(tmp_path: Path) -> None:
    """Renamed and added columns of the newer export parse like the old ones."""
    path = _write_csv(tmp_path, NEW_COLUMNS, [_default_new_row()])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.action is ActionType.BUY
    assert transaction.symbol == "AAPL"
    assert transaction.quantity == Decimal(1)
    assert transaction.price == Decimal(100)
    assert transaction.amount == Decimal(-100)
    assert transaction.currency == "GBP"
    assert transaction.isin == "US0378331005"


def test_read_freetrade_transactions_new_header_top_up(tmp_path: Path) -> None:
    """The renamed account currency amount is read from its new column."""
    overrides = {
        FreetradeColumn.TYPE.value: "TOP_UP",
        FreetradeColumn.TICKER.value: "",
        RENAMED_COLUMNS[FreetradeColumn.TOTAL_AMOUNT.value]: "150",
    }
    path = _write_csv(tmp_path, NEW_COLUMNS, [_default_new_row(overrides)])

    transactions = FreetradeParser().load_from_file(path)

    assert len(transactions) == 1
    assert transactions[0].action is ActionType.TRANSFER
    assert transactions[0].amount == Decimal(150)


def test_read_freetrade_transactions_new_header_missing_column(tmp_path: Path) -> None:
    """A renamed column still counts as missing when the export drops it."""
    dropped = RENAMED_COLUMNS[FreetradeColumn.TOTAL_SHARES_AMOUNT.value]
    header = [column for column in NEW_COLUMNS if column != dropped]
    path = _write_csv(tmp_path, header)

    with pytest.raises(ParsingError, match="Missing columns: Total Shares Amount"):
        FreetradeParser().load_from_file(path)


def test_freetrade_transaction_unsupported_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupported action types raise a helpful error."""

    def fake_action_from_str(action_type: str, buy_sell: str, file: Path) -> ActionType:
        return ActionType.ADJUSTMENT

    monkeypatch.setattr(
        "cgt_calc.parsers.freetrade._action_from_str", fake_action_from_str
    )
    dummy_file = tmp_path / "dummy.csv"
    dummy_file.write_text("")
    row = _default_row({FreetradeColumn.TYPE.value: "ADJUSTMENT"})

    with pytest.raises(
        UnsupportedBrokerActionError,
        match="Unsupported Freetrade action 'ADJUSTMENT'",
    ):
        FreetradeTransaction(dict(zip(COLUMNS, row, strict=True)), dummy_file)


# ── Treasury bill redemptions ─────────────────────────────────────────────────


def _tbill_row(
    title: str, timestamp: str, isin: str, face: str, cost: str
) -> list[str]:
    """Build a T-bill purchase as Freetrade writes it.

    The ISIN sits in the Ticker column, and the quantity is the £1-per-unit
    face value the bill will redeem for.
    """
    return _default_row(
        {
            FreetradeColumn.TITLE.value: title,
            FreetradeColumn.TIMESTAMP.value: timestamp,
            FreetradeColumn.TICKER.value: isin,
            FreetradeColumn.ISIN.value: isin,
            FreetradeColumn.QUANTITY.value: face,
            FreetradeColumn.TOTAL_AMOUNT.value: cost,
            FreetradeColumn.PRICE_PER_SHARE_ACCOUNT.value: str(
                Decimal(cost) / Decimal(face)
            ),
        }
    )


# Exports are newest first, so a rolled ladder reads bottom-up.
LADDER = [
    _tbill_row(
        "UK T-Bill 12/08/24",
        "2024-07-12T07:42:00",
        "GB00BP24DZ03",
        "3060.07",
        "3047.95",
    ),
    _tbill_row(
        "UK T-Bill 15/07/24",
        "2024-06-14T07:47:48",
        "GB00BP243M73",
        "3047.95",
        "3035.83",
    ),
]


def test_tbill_redeems_on_the_purchase_its_face_value_paid_for(tmp_path: Path) -> None:
    """Freetrade posts no maturity row; the next bill's cost identifies it."""
    transactions = FreetradeParser().load_from_file(
        _write_csv(tmp_path, COLUMNS, LADDER)
    )

    redemptions = [t for t in transactions if t.action is ActionType.SELL]
    assert len(redemptions) == 2
    first = redemptions[0]
    # The June bill redeemed at face on the day the July bill was bought.
    assert first.date == date(2024, 7, 12)
    assert first.symbol == "GB00BP243M73"
    assert first.quantity == Decimal("3047.95")
    assert first.price == Decimal(1)
    assert first.amount == Decimal("3047.95")
    assert first.fees == Decimal(0)
    # It has to be seen before the purchase it paid for.
    same_day = [t for t in transactions if t.date == date(2024, 7, 12)]
    assert [t.action for t in same_day] == [ActionType.SELL, ActionType.BUY]
    # The last bill has no successor, so its title date is used instead.
    assert redemptions[1].date == date(2024, 8, 12)


def _redemptions_as_of(
    tmp_path: Path, rows: list[list[str]], today: date
) -> list[BrokerTransaction]:
    """Redemptions the parser would synthesise on a given day.

    load_from_file already applies today's date, so the purchases are taken
    back out of its result and re-resolved against the date under test.
    """
    parsed = FreetradeParser().load_from_file(_write_csv(tmp_path, COLUMNS, rows))
    purchases = [t for t in parsed if t.action is ActionType.BUY]
    return [
        t
        for t in _with_tbill_redemptions(purchases, today=today)
        if t.action is ActionType.SELL
    ]


def test_tbill_still_held_is_not_redeemed(tmp_path: Path) -> None:
    """A bill whose maturity hasn't arrived is a real holding, not cash."""
    transactions = _redemptions_as_of(tmp_path, LADDER, date(2024, 7, 20))

    # Only the June bill, which the July purchase proves matured.
    assert [t.symbol for t in transactions] == ["GB00BP243M73"]


def test_two_tbills_merged_into_one_purchase_both_redeem(tmp_path: Path) -> None:
    """Two ladders rolled into a single bill: the costs sum."""
    rows = [
        _tbill_row(
            "UK T-Bill 09/03/26",
            "2026-02-06T08:40:54",
            "GB00BSGM1W44",
            "4362.57",
            "4350.58",
        ),
        _tbill_row(
            "UK T-Bill 09/02/26",
            "2026-01-09T08:03:14",
            "GB00BSGNGR75",
            "1090.54",
            "1087.45",
        ),
        _tbill_row(
            "UK T-Bill 09/02/26",
            "2026-01-09T08:03:14",
            "GB00BSGNGR75",
            "3260.04",
            "3250.81",
        ),
    ]

    redemptions = _redemptions_as_of(tmp_path, rows, date(2026, 2, 10))

    # Both January bills matured into the February purchase, which cost their
    # two face values added together.
    assert {t.quantity for t in redemptions} == {Decimal("1090.54"), Decimal("3260.04")}
    assert {t.date for t in redemptions} == {date(2026, 2, 6)}


def test_a_title_date_that_cannot_be_a_maturity_is_ignored(tmp_path: Path) -> None:
    """Some titles carry the purchase date, or a typo a year out."""
    rows = [
        _tbill_row(
            "UK T-Bill 23/06/26",
            "2025-05-23T08:18:26",
            "GB00BSGJG649",
            "3181.59",
            "3171.61",
        ),
    ]

    (redemption,) = _redemptions_as_of(tmp_path, rows, date(2025, 12, 31))
    # 200 days out is no maturity for a bill, so the assumed 28-day term wins.
    assert redemption.date == date(2025, 6, 20)


def test_non_tbill_purchases_are_left_alone(tmp_path: Path) -> None:
    """An ordinary share purchase gains no redemption row."""
    transactions = FreetradeParser().load_from_file(
        _write_csv(tmp_path, COLUMNS, [_default_row()])
    )

    assert [t.action for t in transactions] == [ActionType.BUY]
