"""Unit tests for the Plaid → Transaction transformation under Option 5.

No live Plaid API is touched. We feed synthetic transaction dicts (matching the
shape of the Plaid Python SDK's response) into the pure helpers and assert the
resulting Transaction matches the Option-5 contract:

  - amount is the magnitude (always >= 0)
  - section_type is set from the closed enum, never empty for non-zero amounts
  - flow_type is one of the six allowed values
  - notes captures meaningful source detail (check #, currency, pending, ...)
  - category snaps to our preferred list when Plaid's PFC maps to one
"""
import datetime
import pytest

from src.plaid_client import (
    _plaid_section_type,
    _plaid_flow_type,
    _plaid_to_category,
    _plaid_to_notes,
    _plaid_to_transaction,
)


class TestSectionType:
    """Plaid sign + account_type + PFC → our section_type enum."""

    # ---- credit card ----
    def test_credit_positive_amount_default_is_purchase(self):
        # Money out of CC account = purchase (balance owed up)
        assert _plaid_section_type(
            amount=42.50, account_type="credit",
            pfc_primary="FOOD_AND_DRINK", transaction_code=""
        ) == "purchase"

    def test_credit_positive_bank_fees_is_fee(self):
        assert _plaid_section_type(
            amount=39.00, account_type="credit",
            pfc_primary="BANK_FEES", transaction_code=""
        ) == "fee"

    def test_credit_positive_interest_is_interest_charged(self):
        assert _plaid_section_type(
            amount=12.34, account_type="credit",
            pfc_primary="INTEREST_CHARGES", transaction_code=""
        ) == "interest_charged"

    def test_credit_negative_cc_payment_is_payment(self):
        # Money in to CC = payment received
        assert _plaid_section_type(
            amount=-500.00, account_type="credit",
            pfc_primary="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", transaction_code=""
        ) == "payment"

    def test_credit_negative_default_is_refund(self):
        # A negative on a CC that isn't a payment is treated as a refund.
        assert _plaid_section_type(
            amount=-20.00, account_type="credit",
            pfc_primary="GENERAL_MERCHANDISE", transaction_code=""
        ) == "refund"

    # ---- checking / savings ----
    def test_checking_positive_default_is_withdrawal(self):
        assert _plaid_section_type(
            amount=80.00, account_type="checking",
            pfc_primary="FOOD_AND_DRINK", transaction_code=""
        ) == "withdrawal"

    def test_checking_positive_check_code_is_check(self):
        assert _plaid_section_type(
            amount=200.00, account_type="checking",
            pfc_primary="", transaction_code="check"
        ) == "check"

    def test_checking_positive_bank_fee_is_fee(self):
        assert _plaid_section_type(
            amount=12.00, account_type="checking",
            pfc_primary="BANK_FEES", transaction_code=""
        ) == "fee"

    def test_checking_negative_default_is_deposit(self):
        assert _plaid_section_type(
            amount=-2500.00, account_type="checking",
            pfc_primary="INCOME_WAGES", transaction_code=""
        ) == "deposit"

    def test_checking_negative_interest_is_interest_credited(self):
        assert _plaid_section_type(
            amount=-0.42, account_type="checking",
            pfc_primary="INCOME_INTEREST_EARNED", transaction_code=""
        ) == "interest_credited"

    # ---- edge ----
    def test_zero_amount_returns_empty(self):
        assert _plaid_section_type(
            amount=0.0, account_type="credit",
            pfc_primary="FOOD_AND_DRINK", transaction_code=""
        ) == ""


class TestPlaidToCategory:
    def test_food_and_drink_maps_to_dining(self):
        assert _plaid_to_category("FOOD_AND_DRINK") == "Dining"

    def test_groceries_via_detailed(self):
        assert _plaid_to_category(
            "FOOD_AND_DRINK", pfc_detailed="FOOD_AND_DRINK_GROCERIES"
        ) == "Groceries"

    def test_transportation_preferred(self):
        assert _plaid_to_category("TRANSPORTATION") == "Transportation"

    def test_credit_card_payment_via_loan_payments(self):
        assert _plaid_to_category(
            "LOAN_PAYMENTS", pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
        ) == "Payment"

    def test_income_wages_maps_to_salary(self):
        assert _plaid_to_category(
            "INCOME", pfc_detailed="INCOME_WAGES"
        ) == "Salary"

    def test_unknown_pfc_humanized(self):
        # If Plaid invents a new primary we don't know, surface a humanized form.
        assert _plaid_to_category("NEW_WEIRD_THING") == "New Weird Thing"

    def test_empty_returns_empty(self):
        assert _plaid_to_category("") == ""


class TestPlaidToNotes:
    def test_check_number(self):
        assert _plaid_to_notes({
            "payment_meta": {"check_number": "1234"},
            "iso_currency_code": "USD",
        }) == "check #1234"

    def test_pending(self):
        assert _plaid_to_notes({"pending": True, "iso_currency_code": "USD"}) == "pending"

    def test_non_usd_currency(self):
        assert _plaid_to_notes({"iso_currency_code": "EUR"}) == "currency: EUR"

    def test_authorized_date_distinct(self):
        out = _plaid_to_notes({
            "date": datetime.date(2026, 3, 12),
            "authorized_date": datetime.date(2026, 3, 10),
        })
        assert "authorized 2026-03-10" in out

    def test_authorized_same_date_omitted(self):
        out = _plaid_to_notes({
            "date": datetime.date(2026, 3, 12),
            "authorized_date": datetime.date(2026, 3, 12),
        })
        assert "authorized" not in out

    def test_multiple_artifacts_joined(self):
        out = _plaid_to_notes({
            "payment_meta": {"check_number": "777"},
            "pending": True,
            "iso_currency_code": "USD",
        })
        # Order: check first, then pending; semicolon-joined.
        assert out == "check #777; pending"

    def test_no_notable_artifacts_returns_empty(self):
        assert _plaid_to_notes({
            "date": datetime.date(2026, 3, 12),
            "iso_currency_code": "USD",
        }) == ""


class TestPlaidToTransaction:
    """End-to-end transformation: one Plaid response → one Option-5 Transaction."""

    ACCT_MAP = {
        "acc_cc": ("our_cc_id", "credit"),
        "acc_chk": ("our_chk_id", "checking"),
    }

    def test_cc_purchase(self):
        plaid_txn = {
            "transaction_id": "tx1",
            "account_id": "acc_cc",
            "date": datetime.date(2026, 3, 11),
            "amount": 177.64,   # positive = money out of CC = purchase
            "name": "LS Alpine Inn Restaurant",
            "merchant_name": "LS Alpine Inn",
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_RESTAURANTS",
            },
            "iso_currency_code": "USD",
            "pending": False,
        }
        t = _plaid_to_transaction(plaid_txn, self.ACCT_MAP, user_id="alex")
        assert t.amount == pytest.approx(177.64) and t.amount >= 0
        assert t.section_type == "purchase"
        assert t.flow_type == "spending"
        assert t.category == "Dining"
        assert t.account_type == "credit"
        assert t.account_id == "our_cc_id"
        assert t.source == "plaid"
        assert t.notes == ""
        assert t.date == "2026-03-11"

    def test_cc_payment_received(self):
        plaid_txn = {
            "transaction_id": "tx2",
            "account_id": "acc_cc",
            "date": datetime.date(2026, 3, 1),
            "amount": -197.50,   # negative = money in to CC = payment received
            "name": "PAYMENT FROM CHK 0790",
            "merchant_name": None,
            "personal_finance_category": {
                "primary": "LOAN_PAYMENTS",
                "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            },
            "iso_currency_code": "USD",
        }
        t = _plaid_to_transaction(plaid_txn, self.ACCT_MAP, user_id="alex")
        assert t.amount == pytest.approx(197.50)
        assert t.section_type == "payment"
        assert t.flow_type == "transfer"
        assert t.category == "Payment"

    def test_checking_deposit(self):
        plaid_txn = {
            "transaction_id": "tx3",
            "account_id": "acc_chk",
            "date": datetime.date(2026, 2, 27),
            "amount": -6073.87,   # negative = money in = deposit
            "name": "SFDC PAYROLL",
            "merchant_name": "Salesforce",
            "personal_finance_category": {
                "primary": "INCOME",
                "detailed": "INCOME_WAGES",
            },
        }
        t = _plaid_to_transaction(plaid_txn, self.ACCT_MAP, user_id="alex")
        assert t.amount == pytest.approx(6073.87)
        assert t.section_type == "deposit"
        assert t.flow_type == "income"
        assert t.category == "Salary"

    def test_checking_check_payment(self):
        plaid_txn = {
            "transaction_id": "tx4",
            "account_id": "acc_chk",
            "date": datetime.date(2026, 3, 2),
            "amount": 180.00,     # positive = money out
            "name": "Check 546",
            "merchant_name": None,
            "personal_finance_category": None,
            "transaction_code": "check",
            "payment_meta": {"check_number": "546"},
        }
        t = _plaid_to_transaction(plaid_txn, self.ACCT_MAP, user_id="alex")
        assert t.amount == pytest.approx(180.00)
        assert t.section_type == "check"
        assert t.notes == "check #546"

    def test_foreign_currency_pending(self):
        plaid_txn = {
            "transaction_id": "tx5",
            "account_id": "acc_cc",
            "date": datetime.date(2026, 4, 2),
            "amount": 147.96,
            "name": "Chevron NORTH VANCOUVER",
            "merchant_name": "Chevron",
            "personal_finance_category": {
                "primary": "TRANSPORTATION",
                "detailed": "TRANSPORTATION_GAS",
            },
            "iso_currency_code": "CAD",
            "pending": True,
        }
        t = _plaid_to_transaction(plaid_txn, self.ACCT_MAP, user_id="alex")
        assert t.section_type == "purchase"
        assert t.category == "Transportation"
        # Both pending and non-USD currency should be captured.
        assert "pending" in t.notes
        assert "currency: CAD" in t.notes


class TestStorageRoundTripFromPlaid:
    """Plaid-built Transactions go through the same upsert path as PDF ones."""

    def test_upsert_plaid_built_transactions(self, tmp_data):
        # Build a Plaid-style transaction, push it through TransactionStore,
        # read it back, confirm the Option-5 fields survive the round trip.
        import datetime
        from src.plaid_client import _plaid_to_transaction
        from src.storage import TransactionStore

        plaid_txn = {
            "transaction_id": "plaid_roundtrip_1",
            "account_id": "acc_chk",
            "date": datetime.date(2026, 3, 12),
            "amount": -6073.87,
            "name": "SFDC PAYROLL",
            "merchant_name": "Salesforce",
            "personal_finance_category": {
                "primary": "INCOME",
                "detailed": "INCOME_WAGES",
            },
        }
        acct_map = {"acc_chk": ("our_chk", "checking")}
        tx = _plaid_to_transaction(plaid_txn, acct_map, user_id="alex")

        TransactionStore.upsert_transactions([tx])
        rows = TransactionStore.query_all()
        assert len(rows) == 1
        r = rows[0]
        assert r.amount == pytest.approx(6073.87)
        assert r.amount >= 0
        assert r.section_type == "deposit"
        assert r.flow_type == "income"
        assert r.category == "Salary"
        assert r.source == "plaid"
