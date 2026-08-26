"""Freetrade parser."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import combinations
import logging
import re
from typing import TYPE_CHECKING, ClassVar, Final, TextIO, override

from cgt_calc.const import UK_TIMEZONE
from cgt_calc.exceptions import (
    ParsingError,
    UnsupportedBrokerActionError,
    UnsupportedBrokerCurrencyError,
)
from cgt_calc.model import ActionType, BrokerTransaction, CurrencyCode, Isin

from .base_parsers import BaseSingleFileParser

if TYPE_CHECKING:
    from pathlib import Path

BROKER_NAME: Final = "Freetrade"
LOGGER = logging.getLogger(__name__)


class FreetradeColumn(StrEnum):
    """Column names expected in Freetrade CSV exports."""

    TITLE = "Title"
    TYPE = "Type"
    TIMESTAMP = "Timestamp"
    ACCOUNT_CURRENCY = "Account Currency"
    TOTAL_AMOUNT = "Total Amount"
    BUY_SELL = "Buy / Sell"
    TICKER = "Ticker"
    ISIN = "ISIN"
    PRICE_PER_SHARE_ACCOUNT = "Price per Share in Account Currency"
    STAMP_DUTY = "Stamp Duty"
    QUANTITY = "Quantity"
    VENUE = "Venue"
    ORDER_ID = "Order ID"
    ORDER_TYPE = "Order Type"
    INSTRUMENT_CURRENCY = "Instrument Currency"
    TOTAL_SHARES_AMOUNT = "Total Shares Amount"
    PRICE_PER_SHARE = "Price per Share"
    FX_RATE = "FX Rate"
    BASE_FX_RATE = "Base FX Rate"
    FX_FEE_BPS = "FX Fee (BPS)"
    FX_FEE_AMOUNT = "FX Fee Amount"
    DIVIDEND_EX_DATE = "Dividend Ex Date"
    DIVIDEND_PAY_DATE = "Dividend Pay Date"
    DIVIDEND_ELIGIBLE_QUANTITY = "Dividend Eligible Quantity"
    DIVIDEND_AMOUNT_PER_SHARE = "Dividend Amount Per Share"
    DIVIDEND_GROSS_AMOUNT = "Dividend Gross Distribution Amount"
    DIVIDEND_NET_AMOUNT = "Dividend Net Distribution Amount"
    DIVIDEND_WITHHELD_PERCENTAGE = "Dividend Withheld Tax Percentage"
    DIVIDEND_WITHHELD_AMOUNT = "Dividend Withheld Tax Amount"


COLUMNS: Final[list[str]] = [column.value for column in FreetradeColumn]
REQUIRED_COLUMNS: Final[set[str]] = set(COLUMNS)

# Freetrade renamed these columns, older exports still use the names above.
COLUMN_ALIASES: Final[dict[str, str]] = {
    "Total Amount in Account Currency": FreetradeColumn.TOTAL_AMOUNT.value,
    "Total Amount in Instrument Currency": FreetradeColumn.TOTAL_SHARES_AMOUNT.value,
}

# Newer exports add a block of stock split columns. They are tolerated so that
# the header validates, their values are not read yet: a stock split row still
# fails as an unknown type.
STOCK_SPLIT_PREFIX: Final = "Stock Split "

# These Activity Feed rows point to documents rather than cash or security
# transactions. Freetrade includes them in an All Activity export.
IGNORED_TYPES: Final[frozenset[str]] = frozenset(
    {"MONTHLY_STATEMENT", "MONTHLY_SHARE_LENDING_STATEMENT", "TAX_CERTIFICATE"}
)

# Rows that move cash without a trade: Total Amount is the whole story and
# a symbol is optional.
CASH_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.TRANSFER, ActionType.INTEREST, ActionType.OTHER_INCOME}
)


def _parse_decimal(row: dict[str, str], column: FreetradeColumn) -> Decimal:
    """Parse Decimal value for column, raising ValueError with context on failure."""
    value = row[column]
    try:
        return Decimal(value)
    except InvalidOperation as err:
        raise ValueError(
            f"Invalid decimal in column '{column.value}': {value!r}"
        ) from err


def _parse_optional_decimal(row: dict[str, str], column: FreetradeColumn) -> Decimal:
    """Parse a decimal that Freetrade leaves blank when it does not apply."""
    if row[column] == "":
        return Decimal(0)
    return _parse_decimal(row, column)


def _instrument_amount_in_gbp(row: dict[str, str], amount: Decimal) -> Decimal:
    """Convert an instrument-currency amount to account currency."""
    if amount != 0:
        instrument_currency = CurrencyCode(row[FreetradeColumn.INSTRUMENT_CURRENCY])
        if instrument_currency != "GBP":
            # Dividend exports express Base FX Rate as GBP per unit of the
            # instrument currency (unlike the trade-only FX Rate column).
            amount *= _parse_decimal(row, FreetradeColumn.BASE_FX_RATE)
    return amount


def _dividend_amount_in_gbp(row: dict[str, str], column: FreetradeColumn) -> Decimal:
    """Convert a dividend field from instrument currency to account currency."""
    return _instrument_amount_in_gbp(row, _parse_decimal(row, column))


def _dividend_gross_in_gbp(row: dict[str, str]) -> Decimal:
    """Gross dividend in GBP, rebuilt from per-share figures when left blank."""
    if row[FreetradeColumn.DIVIDEND_GROSS_AMOUNT] != "":
        return _dividend_amount_in_gbp(row, FreetradeColumn.DIVIDEND_GROSS_AMOUNT)
    amount = _parse_decimal(
        row, FreetradeColumn.DIVIDEND_ELIGIBLE_QUANTITY
    ) * _parse_decimal(row, FreetradeColumn.DIVIDEND_AMOUNT_PER_SHARE)
    return _instrument_amount_in_gbp(row, amount)


def _action_from_str(action_type: str, buy_sell: str, file: Path) -> ActionType:
    """Infer action type."""
    if action_type == "INTEREST_FROM_CASH":
        return ActionType.INTEREST
    if action_type == "INTEREST":
        # A coupon on a bond or gilt held directly, or an interest distribution
        # from a bond fund. Laid out like a dividend row (ticker, per-share
        # amount) but taxed as interest.
        return ActionType.INTEREST
    if action_type == "SHARE_LENDING_INCOME":
        # Fees from Freetrade's share lending programme: miscellaneous income
        # rather than interest (SA100 box 17, covered by the £1,000 trading
        # and miscellaneous income allowance).
        return ActionType.OTHER_INCOME
    if action_type == "DIVIDEND":
        return ActionType.DIVIDEND
    if action_type == "PROPERTY":
        # REIT Property Income Distributions are not qualifying dividends;
        # they are taxed as property income (SA100 box 17, with the 20%
        # basic-rate tax the REIT withheld in box 19). Total Amount is net
        # of that tax, which the row also states.
        return ActionType.OTHER_INCOME
    if action_type in {"TOP_UP", "WITHDRAWAL"}:
        return ActionType.TRANSFER
    if action_type in {"ORDER", "FREESHARE_ORDER"}:
        if buy_sell == "BUY":
            return ActionType.BUY
        if buy_sell == "SELL":
            return ActionType.SELL

        raise ParsingError(file, f"Unknown buy_sell: '{buy_sell}'")

    raise ParsingError(file, f"Unknown type: '{action_type}'")


class FreetradeTransaction(BrokerTransaction):
    """Represents a single Freetrade transaction."""

    def __init__(self, row: dict[str, str], file: Path) -> None:
        """Create transaction from a CSV row keyed by column name."""
        action = _action_from_str(
            row[FreetradeColumn.TYPE], row[FreetradeColumn.BUY_SELL], file
        )

        # Some rows carry no ticker (free shares, delisted or renamed lines);
        # fall back to the ISIN so the transaction still has a symbol. The
        # BrokerRegistry swaps it for the ticker once every row is loaded.
        symbol = row[FreetradeColumn.TICKER] or row[FreetradeColumn.ISIN] or None
        if symbol is None and action not in CASH_ACTIONS:
            raise ParsingError(file, f"No symbol for action: {action}")

        # The importer and calculation path below use the exported GBP account
        # currency fields. Reject another account currency rather than
        # silently treating it as sterling.
        if row[FreetradeColumn.ACCOUNT_CURRENCY] != "GBP":
            raise UnsupportedBrokerCurrencyError(
                file, BROKER_NAME, row[FreetradeColumn.ACCOUNT_CURRENCY]
            )

        fees = Decimal(0)
        if action in {ActionType.SELL, ActionType.BUY}:
            quantity = _parse_decimal(row, FreetradeColumn.QUANTITY)
            # These two fields are already in account currency. Total Amount
            # is the cash movement after fees, while the account-currency unit
            # price excludes them, so retaining the exported fee fields keeps
            # all three values mutually consistent.
            price = _parse_decimal(row, FreetradeColumn.PRICE_PER_SHARE_ACCOUNT)
            amount = _parse_decimal(row, FreetradeColumn.TOTAL_AMOUNT)
            fees = _parse_optional_decimal(
                row, FreetradeColumn.STAMP_DUTY
            ) + _parse_optional_decimal(row, FreetradeColumn.FX_FEE_AMOUNT)
            currency = CurrencyCode("GBP")
        elif action == ActionType.DIVIDEND:
            amount = _dividend_gross_in_gbp(row)
            quantity, price = None, None
            currency = CurrencyCode("GBP")
        elif action in CASH_ACTIONS:
            # Total Amount is the cash that arrived. On a row laid out like a
            # dividend (a coupon, a REIT distribution) that is net of any tax
            # withheld, so the gross income is rebuilt from the withheld
            # figure, which the tax-at-source transaction then carries back.
            amount = _parse_decimal(
                row, FreetradeColumn.TOTAL_AMOUNT
            ) + _withheld_tax_in_gbp(row)
            quantity, price = None, None
            currency = CurrencyCode("GBP")
        else:
            raise UnsupportedBrokerActionError(
                file, BROKER_NAME, row[FreetradeColumn.TYPE]
            )

        if row[FreetradeColumn.TYPE] == "FREESHARE_ORDER":
            price = Decimal(0)
            amount = Decimal(0)
            fees = Decimal(0)

        amount_negative = (
            action == ActionType.BUY or row[FreetradeColumn.TYPE] == "WITHDRAWAL"
        )
        if amount_negative:
            amount *= -1

        isin_raw = row.get(FreetradeColumn.ISIN)
        isin = Isin(isin_raw) if isin_raw else None

        # The timestamp is in UTC, but the date that drives the tax year and
        # the matching rules is the UK one.
        timestamp = datetime.fromisoformat(row[FreetradeColumn.TIMESTAMP])
        if timestamp.tzinfo is None:
            # The export states its times in UTC, so a value without a zone
            # is read as UTC too rather than as the machine's local time.
            timestamp = timestamp.replace(tzinfo=UTC)

        super().__init__(
            date=timestamp.astimezone(UK_TIMEZONE).date(),
            action=action,
            symbol=symbol,
            description=f"{row[FreetradeColumn.TITLE]} {action}",
            quantity=quantity,
            price=price,
            fees=fees,
            amount=amount,
            currency=currency,
            broker=BROKER_NAME,
            isin=isin,
        )


# ── Treasury bill redemptions ─────────────────────────────────────────────────
# Freetrade posts a row when a Treasury bill is bought and none when it matures:
# the cash simply reappears, and is usually spent the same day on the next bill.
# Left alone that breaks two things at once — the cash balance falls by the whole
# ladder, and every bill ever bought stays in the pool for ever. Bills redeem at
# £1 per unit, so a purchase's quantity IS its face value in pounds, and the
# redemption is reconstructed here.
_TBILL_TITLE: Final = re.compile(
    r"\b(?:uk\s+)?t-?bills?\b|\btreasury\s+bills?\b", re.IGNORECASE
)
# "UK T-Bill 29/06/26". The title carries a date, but it is a poor maturity: in
# real exports it runs a few days after the roll, and some titles hold the
# purchase date instead. It is only consulted for a bill nothing else explains.
_TBILL_TITLE_DATE: Final = re.compile(r"(\d{2})/(\d{2})/(\d{2,4})")
# UK bills run 1-6 months, so a title date outside that window is a typo.
_TBILL_MAX_TERM: Final = timedelta(days=200)
# Fallback term for a matured bill with no successor and no usable title date.
_TBILL_ASSUMED_TERM: Final = timedelta(days=28)
# A two-digit year in a title is this century.
_CENTURY: Final = 100


def _is_tbill_purchase(transaction: BrokerTransaction) -> bool:
    return (
        transaction.action is ActionType.BUY
        and transaction.quantity is not None
        and transaction.amount is not None
        and bool(_TBILL_TITLE.search(transaction.description))
    )


def _tbill_title_maturity(transaction: BrokerTransaction) -> date | None:
    """Read the maturity out of a bill's title, if it is a plausible one."""
    match = _TBILL_TITLE_DATE.search(transaction.description)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < _CENTURY:
        year += 2000
    try:
        titled = date(year, month, day)
    except ValueError:
        return None
    if transaction.date < titled <= transaction.date + _TBILL_MAX_TERM:
        return titled
    return None


def _funded_by(
    outstanding: list[BrokerTransaction], cost: Decimal
) -> list[BrokerTransaction]:
    """Which held bills a purchase of `cost` was paid for with, if any.

    A rolled ladder buys the next bill for exactly the face value that just
    matured, which identifies the maturity date far more reliably than the
    title does. Two bills merging into one purchase is common enough to match
    for as well; beyond that the title takes over.
    """
    for bill in outstanding:
        if bill.quantity == cost:
            return [bill]
    for first, second in combinations(outstanding, 2):
        if first.quantity + second.quantity == cost:  # type: ignore[operator]
            return [first, second]
    return []


def _tbill_redemptions(
    transactions: list[BrokerTransaction], today: date
) -> list[BrokerTransaction]:
    """Reconstruct the redemption rows Freetrade leaves out of its exports."""
    bills = [t for t in transactions if _is_tbill_purchase(t)]
    if not bills:
        return []

    matured: list[tuple[BrokerTransaction, date]] = []
    outstanding: list[BrokerTransaction] = []
    for bill in bills:  # read_transactions returns oldest first
        for funder in _funded_by(outstanding, -bill.amount):  # type: ignore[operator]
            matured.append((funder, bill.date))
            outstanding.remove(funder)
        outstanding.append(bill)

    # Whatever is left either matured after the last purchase in the file or is
    # still held. Only the former gets a redemption.
    for bill in outstanding:
        maturity = _tbill_title_maturity(bill) or bill.date + _TBILL_ASSUMED_TERM
        if maturity <= today:
            matured.append((bill, maturity))

    redemptions = [
        BrokerTransaction(
            date=redeemed_on,
            action=ActionType.SELL,
            symbol=bill.symbol,
            description=f"{bill.description} redeemed at face value",
            quantity=bill.quantity,
            price=Decimal(1),
            fees=Decimal(0),
            amount=bill.quantity,
            currency=CurrencyCode("GBP"),
            broker=BROKER_NAME,
            isin=bill.isin,
        )
        for bill, redeemed_on in matured
    ]
    LOGGER.warning(
        "Freetrade exports carry no Treasury bill maturities, so %d redemptions "
        "totalling %s GBP were reconstructed from the purchases they funded. The "
        "discount to face is the return on the bill.",
        len(redemptions),
        sum(t.amount for t in redemptions),  # type: ignore[misc]
    )
    return redemptions


def _with_tbill_redemptions(
    transactions: list[BrokerTransaction], today: date | None = None
) -> list[BrokerTransaction]:
    """Merge reconstructed redemptions in, each ahead of same-day rows.

    A bill's face value pays for the next bill on the day it matures, so the
    redemption has to be seen before the purchase it funds; the calculator's
    sort is stable, so this order survives it.
    """
    pending = sorted(
        _tbill_redemptions(transactions, today or date.today()), key=lambda t: t.date
    )
    if not pending:
        return transactions
    merged: list[BrokerTransaction] = []
    index = 0
    for transaction in transactions:
        while index < len(pending) and pending[index].date <= transaction.date:
            merged.append(pending[index])
            index += 1
        merged.append(transaction)
    merged.extend(pending[index:])
    return merged


def _withheld_tax_in_gbp(row: dict[str, str]) -> Decimal:
    """Tax withheld at source on an income row, 0 where the column is blank."""
    if row[FreetradeColumn.DIVIDEND_WITHHELD_AMOUNT] == "":
        return Decimal(0)
    return _dividend_amount_in_gbp(row, FreetradeColumn.DIVIDEND_WITHHELD_AMOUNT)


# The tax-at-source action that pairs with each income action.
TAX_ACTIONS: Final[dict[ActionType, ActionType]] = {
    ActionType.DIVIDEND: ActionType.DIVIDEND_TAX,
    ActionType.INTEREST: ActionType.INTEREST_TAX,
    ActionType.OTHER_INCOME: ActionType.OTHER_INCOME_TAX,
}


def _tax_at_source_transaction(
    transaction: FreetradeTransaction, row: dict[str, str]
) -> BrokerTransaction | None:
    """Create the tax-at-source cash movement carried on an income row."""
    tax_action = TAX_ACTIONS.get(transaction.action)
    if tax_action is None:
        return None

    amount = _withheld_tax_in_gbp(row)
    if amount == 0:
        return None

    return BrokerTransaction(
        date=transaction.date,
        action=tax_action,
        symbol=transaction.symbol,
        description=f"{row[FreetradeColumn.TITLE]} {tax_action}",
        quantity=None,
        price=None,
        fees=Decimal(0),
        amount=-amount,
        currency=CurrencyCode("GBP"),
        broker=BROKER_NAME,
        isin=transaction.isin,
    )


class FreetradeParser(BaseSingleFileParser):
    """Parser for Freetrade transaction files."""

    arg_name = "freetrade"
    pretty_name = "Freetrade"
    format_name = "CSV"
    deprecated_flags: ClassVar[list[str]] = ["--freetrade"]

    @classmethod
    @override
    def read_transactions(
        cls, file: TextIO, file_path: Path
    ) -> list[BrokerTransaction]:
        """Parse Freetrade transactions from a CSV file."""
        lines = list(csv.reader(file))
        if not lines:
            raise ParsingError(file_path, "Freetrade CSV file is empty")
        header = [COLUMN_ALIASES.get(column, column) for column in lines[0]]
        cls._validate_header(header, file_path)
        lines = lines[1:]
        indexed_rows = list(enumerate(lines, start=2))
        # HACK: reverse transactions to avoid negative balance issues
        # the proper fix would be to use datetime in BrokerTransaction
        indexed_rows.reverse()
        transactions: list[BrokerTransaction] = []
        for index, row_raw in indexed_rows:
            # Exports may end with a blank line, which csv reads as an
            # empty row rather than a transaction.
            if not any(row_raw):
                continue
            row = dict(zip(header, row_raw, strict=False))
            action_type = row.get(FreetradeColumn.TYPE)
            if action_type in IGNORED_TYPES:
                LOGGER.debug(
                    "Skipping non-transaction Freetrade row %d (%s)",
                    index,
                    action_type,
                )
                continue
            try:
                transaction = FreetradeTransaction(row, file_path)
                transactions.append(transaction)
                tax_at_source = _tax_at_source_transaction(transaction, row)
                if tax_at_source is not None:
                    transactions.append(tax_at_source)
            except ParsingError as err:
                err.add_row_context(index)
                raise
            except ValueError as err:
                raise ParsingError(file_path, str(err), row_index=index) from err
        return _with_tbill_redemptions(transactions)

    @staticmethod
    def _validate_header(header: list[str], file: Path) -> None:
        """Check if header is valid."""
        provided = set(header)
        missing = REQUIRED_COLUMNS - provided
        if missing:
            missing_columns = ", ".join(sorted(missing))
            raise ParsingError(file, f"Missing columns: {missing_columns}", row_index=1)

        unknown = {
            column
            for column in provided - REQUIRED_COLUMNS
            if not column.startswith(STOCK_SPLIT_PREFIX)
        }
        if unknown:
            unknown_columns = ", ".join(sorted(unknown))
            LOGGER.warning("Unknown columns in %s: %s", file, unknown_columns)
