"""Tests for the broker registry."""

from __future__ import annotations

import csv
import datetime
from decimal import Decimal
import logging
from typing import TYPE_CHECKING

from cgt_calc.args_parser import create_parser
from cgt_calc.const import RENAME_DESCRIPTION_PREFIX
from cgt_calc.isin_converter import IsinConverter
from cgt_calc.model import ActionType, BrokerTransaction, CurrencyCode, Isin
from cgt_calc.parsers.broker_registry import (
    BrokerRegistry,
    _resolve_isin_placeholders,
    _resolve_isins,
)
from cgt_calc.parsers.freetrade import COLUMNS, FreetradeColumn
from cgt_calc.parsers.vanguard import VanguardTransaction

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _vanguard_warning(caplog: pytest.LogCaptureFixture) -> str:
    """Return the unmapped-symbol warning, whatever else the run logged."""
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "no ISIN mapping was found" in record.getMessage()
    ]
    assert len(warnings) == 1, f"Expected exactly one warning, got {warnings}"
    return warnings[0]


def _write_vanguard_csv(tmp_path: Path) -> Path:
    """Write a one-row Vanguard cash table whose symbol is a fund name."""
    vanguard_file = tmp_path / "vanguard.csv"
    vanguard_file.write_text(
        "Date,Details,Amount,Balance\n09/03/2022,Bought 10 Foo Fund,-100.00,0\n",
        encoding="utf-8",
    )
    return vanguard_file


def test_load_all_transactions_without_files(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn when no transactions are found."""
    args = create_parser().parse_args(["--year", "2023"])

    with caplog.at_level(logging.WARNING):
        transactions = BrokerRegistry.load_all_transactions(args, IsinConverter())

    assert transactions == []
    assert "Found 0 broker transactions" in caplog.text


def test_load_all_transactions_from_raw_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Load and sort transactions from a single broker."""
    raw_file = tmp_path / "raw.csv"
    raw_file.write_text(
        "date,action,symbol,quantity,price,fees,currency\n"
        "2023-02-09,DIVIDEND,OPRA,4200,0.80,0.0,USD\n"
        "2022-11-14,SELL,META,19,116.00,0.05,USD\n"
    )
    args = create_parser().parse_args(["--year", "2023", "--raw-file", str(raw_file)])

    with caplog.at_level(logging.INFO):
        transactions = BrokerRegistry.load_all_transactions(args, IsinConverter())

    assert len(transactions) == 2
    assert "Loaded 2 transactions from RAW format" in caplog.text
    # Transactions are sorted by date.
    assert [str(transaction.date) for transaction in transactions] == [
        "2022-11-14",
        "2023-02-09",
    ]


def test_vanguard_symbol_without_isin_mapping_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Warn that ERI cannot be matched to an unmapped Vanguard symbol."""
    vanguard_file = _write_vanguard_csv(tmp_path)
    args = create_parser().parse_args(
        ["--year", "2023", "--vanguard-file", str(vanguard_file)]
    )

    cache = tmp_path / "isin_translation.csv"
    converter = IsinConverter(isin_translation_file=cache)

    with caplog.at_level(logging.WARNING):
        BrokerRegistry.load_all_transactions(args, converter)

    warning = _vanguard_warning(caplog)
    assert "no ISIN mapping was found for: Foo Fund" in warning
    assert f"--isin-translation-file cache at {cache}." in warning
    # Appending a row that drops an alias breaks a later run, so the advice
    # has to say how the cache is keyed, not just where it lives.
    assert "single row listing every symbol it is known by" in warning
    assert "https://cgt-calc.uk/configuration/" in warning


def test_vanguard_warning_omits_cache_path_when_flag_is_cleared(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Do not advertise a cache location when the flag was explicitly cleared."""
    vanguard_file = _write_vanguard_csv(tmp_path)
    args = create_parser().parse_args(
        [
            "--year",
            "2023",
            "--vanguard-file",
            str(vanguard_file),
            "--isin-translation-file",
            "",
        ]
    )

    converter = IsinConverter(isin_translation_file=args.isin_translation_file)

    with caplog.at_level(logging.WARNING):
        BrokerRegistry.load_all_transactions(args, converter)

    warning = _vanguard_warning(caplog)
    assert "no ISIN mapping was found for: Foo Fund" in warning
    assert "--isin-translation-file cache. " in warning


def test_vanguard_symbol_with_isin_mapping_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Do not warn when a Vanguard symbol has an explicit ISIN mapping."""
    vanguard_file = _write_vanguard_csv(tmp_path)
    args = create_parser().parse_args(
        ["--year", "2023", "--vanguard-file", str(vanguard_file)]
    )
    converter = IsinConverter()
    converter.data[Isin("US0378331005")] = {"Foo Fund"}

    with caplog.at_level(logging.WARNING):
        BrokerRegistry.load_all_transactions(args, converter)

    assert "no ISIN mapping was found" not in caplog.text


def test_vanguard_display_name_does_not_define_parser_provenance() -> None:
    """Ignore a non-Vanguard transaction whose broker label happens to match."""
    transaction = BrokerTransaction(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="Foo Fund",
        description="",
        quantity=Decimal(10),
        price=Decimal(10),
        fees=Decimal(0),
        amount=Decimal(-100),
        currency=CurrencyCode("GBP"),
        broker="Vanguard",
    )

    assert _unmapped([transaction], {}) == []


def test_vanguard_symbol_resolved_by_another_broker_does_not_warn() -> None:
    """Stay quiet when another broker's row already supplies the ISIN.

    ERI matches through that row, so telling the user to add a mapping would
    send them after a change that makes no difference.
    """
    vanguard = VanguardTransaction.from_fields(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="VWRP",
        quantity=Decimal(10),
        price=Decimal(10),
        amount=Decimal(-100),
        currency=CurrencyCode("GBP"),
        is_reversal=False,
    )
    elsewhere = BrokerTransaction(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="VWRP",
        description="",
        quantity=Decimal(5),
        price=Decimal(10),
        fees=Decimal(0),
        amount=Decimal(-50),
        currency=CurrencyCode("GBP"),
        broker="Trading212",
        isin=Isin("IE00BK5BQT80"),
    )

    assert _unmapped([vanguard, elsewhere], {}) == []
    assert _unmapped([vanguard], {}) == ["VWRP"]


def test_vanguard_blank_symbol_is_not_reported() -> None:
    """A whitespace-only symbol must not render as a phantom empty entry."""
    blank = VanguardTransaction.from_fields(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="",
        quantity=Decimal(1),
        price=Decimal(1),
        amount=Decimal(-1),
        currency=CurrencyCode("GBP"),
        is_reversal=False,
    )

    whitespace = VanguardTransaction.from_fields(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol=" ",
        quantity=Decimal(1),
        price=Decimal(1),
        amount=Decimal(-1),
        currency=CurrencyCode("GBP"),
        is_reversal=False,
    )

    assert _unmapped([blank], {}) == []
    assert _unmapped([whitespace], {}) == []


def _unmapped(
    transactions: list[BrokerTransaction], isin_map: dict[str, Isin]
) -> list[str]:
    """Return just the unmapped-symbol half of the resolution."""
    return _resolve_isins(transactions, isin_map)[1]


def test_vanguard_symbol_renamed_to_a_mapped_symbol_does_not_warn() -> None:
    """A mapping for the new name also covers rows carrying the old one."""
    pre_rename = VanguardTransaction.from_fields(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="VDXX",
        quantity=Decimal(10),
        price=Decimal(10),
        amount=Decimal(-100),
        currency=CurrencyCode("GBP"),
        is_reversal=False,
    )
    rename = VanguardTransaction.from_fields(
        date=datetime.date(2022, 3, 10),
        action=ActionType.RENAME,
        symbol="VGER",
        quantity=Decimal(0),
        price=None,
        amount=Decimal(0),
        currency=CurrencyCode("GBP"),
        is_reversal=False,
        description=f"{RENAME_DESCRIPTION_PREFIX}VDXX",
    )
    mapped = {"VGER": Isin("IE00BK5BQT80")}

    assert _unmapped([pre_rename, rename], mapped) == []
    # Without the mapping both names are genuinely unmatchable.
    assert _unmapped([pre_rename, rename], {}) == ["VDXX", "VGER"]


def test_resolve_isins_matches_the_eri_filter() -> None:
    """The resolved set is what the ERI filter consumes, from the same pass."""
    vanguard = VanguardTransaction.from_fields(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="VWRP",
        quantity=Decimal(10),
        price=Decimal(10),
        amount=Decimal(-100),
        currency=CurrencyCode("GBP"),
        is_reversal=False,
    )
    elsewhere = BrokerTransaction(
        date=datetime.date(2022, 3, 9),
        action=ActionType.BUY,
        symbol="VWRP",
        description="",
        quantity=Decimal(5),
        price=Decimal(10),
        fees=Decimal(0),
        amount=Decimal(-50),
        currency=CurrencyCode("GBP"),
        broker="Trading212",
        isin=Isin("IE00BK5BQT80"),
    )

    isins, unmapped = _resolve_isins([vanguard, elsewhere], {})

    assert isins == {Isin("IE00BK5BQT80")}
    assert unmapped == []


# ── ISIN placeholder symbols ──────────────────────────────────────────────────

CUKS_ISIN = Isin("IE00B3VWLG82")
# Valid, but absent from the bundled translation data, as are the tickers
# the cache tests give it.
UNLISTED_ISIN = Isin("US5949181045")


def _isin_transaction(
    day: str, symbol: str, isin: Isin | None = CUKS_ISIN
) -> BrokerTransaction:
    """Build a GBP buy of one unit carrying an ISIN."""
    return BrokerTransaction(
        date=datetime.date.fromisoformat(day),
        action=ActionType.BUY,
        symbol=symbol,
        description="",
        quantity=Decimal(1),
        price=Decimal(1),
        fees=Decimal(0),
        amount=Decimal(-1),
        currency=CurrencyCode("GBP"),
        broker="Test",
        isin=isin,
    )


def test_isin_placeholder_takes_ticker_from_nearest_dated_row() -> None:
    """A row whose symbol is its ISIN adopts the ticker of the closest row."""
    old_name = _isin_transaction("2021-01-01", "OLDX")
    new_name = _isin_transaction("2024-01-01", "NEWX")
    early = _isin_transaction("2020-06-01", CUKS_ISIN)
    late = _isin_transaction("2024-06-01", CUKS_ISIN)
    other = _isin_transaction("2024-06-01", "US0378331005", Isin("US0378331005"))

    _resolve_isin_placeholders(
        [late, early, old_name, new_name, other], IsinConverter()
    )

    assert early.symbol == "OLDX"
    assert late.symbol == "NEWX"
    assert old_name.symbol == "OLDX"
    assert new_name.symbol == "NEWX"
    # Nothing in the run or the cache names this one, so it keeps the ISIN.
    assert other.symbol == "US0378331005"


def test_isin_placeholder_takes_ticker_from_translation_cache(
    tmp_path: Path,
) -> None:
    """With no named row in the run, a cache that knows one ticker settles it."""
    cache = tmp_path / "isin_translation.csv"
    cache.write_text(f"ISIN,symbol\n{UNLISTED_ISIN},FAKEA\n", encoding="utf-8")
    placeholder = _isin_transaction("2023-05-17", UNLISTED_ISIN, UNLISTED_ISIN)

    _resolve_isin_placeholders([placeholder], IsinConverter(cache))

    assert placeholder.symbol == "FAKEA"


def test_isin_placeholder_kept_when_cache_is_ambiguous(tmp_path: Path) -> None:
    """Two cached tickers and no row to pick between them: leave it alone."""
    cache = tmp_path / "isin_translation.csv"
    cache.write_text(f"ISIN,symbol\n{UNLISTED_ISIN},FAKEA,FAKEB\n", encoding="utf-8")
    placeholder = _isin_transaction("2023-05-17", UNLISTED_ISIN, UNLISTED_ISIN)

    _resolve_isin_placeholders([placeholder], IsinConverter(cache))

    assert placeholder.symbol == UNLISTED_ISIN


def _freetrade_row(values: dict[str, str]) -> list[str]:
    """Lay out a Freetrade row with every unnamed column blank."""
    row = dict.fromkeys(COLUMNS, "")
    row.update(values)
    return [row[column] for column in COLUMNS]


def _freetrade_order(
    timestamp: str, buy_sell: str, ticker: str, order_type: str = "ORDER"
) -> list[str]:
    """Build a one-share GBP order for the CUKS fund."""
    return _freetrade_row(
        {
            FreetradeColumn.TITLE.value: "MSCI UK Small Cap",
            FreetradeColumn.TYPE.value: order_type,
            FreetradeColumn.TIMESTAMP.value: timestamp,
            FreetradeColumn.ACCOUNT_CURRENCY.value: "GBP",
            FreetradeColumn.TOTAL_AMOUNT.value: "200",
            FreetradeColumn.BUY_SELL.value: buy_sell,
            FreetradeColumn.TICKER.value: ticker,
            FreetradeColumn.ISIN.value: str(CUKS_ISIN),
            FreetradeColumn.PRICE_PER_SHARE_ACCOUNT.value: "200",
            FreetradeColumn.STAMP_DUTY.value: "0",
            FreetradeColumn.QUANTITY.value: "1.00000000",
            FreetradeColumn.INSTRUMENT_CURRENCY.value: "GBP",
        }
    )


def test_freetrade_free_share_without_ticker_pools_with_its_fund(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A Freetrade free share row leaves Ticker blank; it must not become its own holding."""
    freetrade_file = tmp_path / "freetrade.csv"
    with freetrade_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(
            [
                _freetrade_order("2024-02-01T10:00:00.000Z", "SELL", "CUKS"),
                _freetrade_order("2023-09-01T10:00:00.000Z", "BUY", "CUKS"),
                _freetrade_order(
                    "2023-05-17T10:00:00.000Z", "BUY", "", order_type="FREESHARE_ORDER"
                ),
            ]
        )
    args = create_parser().parse_args(
        ["--year", "2023", "--freetrade-file", str(freetrade_file)]
    )
    converter = IsinConverter()

    with caplog.at_level(logging.INFO):
        transactions = BrokerRegistry.load_all_transactions(args, converter)

    # The registry also appends the fund's bundled ERI rows; only the broker
    # rows are of interest here.
    assert [
        transaction.symbol
        for transaction in transactions
        if transaction.action is not ActionType.EXCESS_REPORTED_INCOME
    ] == ["CUKS"] * 3
    assert f"2023-05-17 BUY row of {CUKS_ISIN} carries no ticker; using CUKS" in (
        caplog.text
    )
    # The converter now sees one ticker for the ISIN, so the check that used
    # to reject the free share row passes.
    for transaction in transactions:
        converter.add_from_transaction(transaction)
