import os
from datetime import date, timedelta
from typing import List

import plaid
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

from src.models import Account, Transaction


def _enum_value(v) -> str:
    """Return the string value of a Plaid enum (or "" for None)."""
    if v is None:
        return ""
    return v.value if hasattr(v, "value") else str(v)


def _plaid_flow_type(pfc_primary) -> str:
    """Map Plaid's personal_finance_category.primary to our flow_type enum."""
    if not pfc_primary:
        return "spending"
    p = str(pfc_primary).upper()
    if p.startswith("INCOME"):
        return "income"
    if p in ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"):
        return "transfer"
    if "CREDIT_CARD_PAYMENT" in p:
        return "transfer"
    if p == "BANK_FEES":
        return "fee"
    if "INTEREST" in p:
        return "interest"
    return "spending"


def _plaid_section_type(amount: float, account_type: str,
                       pfc_primary: str, transaction_code: str) -> str:
    """Derive section_type from Plaid's data.

    Plaid amount sign convention (from their docs):
      amount > 0  →  money OUT of this account (purchase, withdrawal, fee charged)
      amount < 0  →  money IN to this account  (refund, deposit, payment received)

    Section types are per the Option-5 contract in
    design/storage.md.
    """
    if amount == 0:
        return ""
    p = (pfc_primary or "").upper()
    code = (transaction_code or "").lower()

    if account_type == "credit":
        if amount > 0:  # balance OWED goes up
            if p == "BANK_FEES":
                return "fee"
            if "INTEREST" in p:
                return "interest_charged"
            # Plaid transaction_code 'cash' on a credit card account means
            # cash advance (ATM withdrawal against the card's credit line).
            if code == "cash":
                return "cash_advance"
            return "purchase"
        else:  # balance OWED goes down
            if "CREDIT_CARD_PAYMENT" in p or "LOAN_PAYMENTS" in p:
                return "payment"
            return "refund"

    # checking / savings / unknown
    if amount > 0:  # money leaving the bank account
        if p == "BANK_FEES":
            return "fee"
        if code == "check":
            return "check"
        return "withdrawal"
    # money entering the bank account
    if "INTEREST" in p:
        return "interest_credited"
    return "deposit"


# Map Plaid's personal_finance_category.primary to our preferred-list category.
# Anything not listed falls through to a humanized form of the Plaid enum.
_PLAID_PFC_TO_CATEGORY = {
    # spending (matches our 15-item preferred list)
    "FOOD_AND_DRINK":             "Dining",
    "TRANSPORTATION":             "Transportation",
    "TRAVEL":                     "Travel",
    "ENTERTAINMENT":              "Entertainment",
    "GENERAL_MERCHANDISE":        "Shopping",
    "RENT_AND_UTILITIES":         "Utilities",
    "MEDICAL":                    "Healthcare",
    "HOME_IMPROVEMENT":           "Housing",
    "PERSONAL_CARE":              "Personal Care",
    "GENERAL_SERVICES":           "Services",
    "GOVERNMENT_AND_NON_PROFIT":  "Government",
    # non-spending — match the natural labels used by the PDF path
    "BANK_FEES":                  "Bank Fees",
    "INTEREST_CHARGES":           "Interest",
    "INTEREST_EARNED":            "Interest",
    "TRANSFER_IN":                "Transfer",
    "TRANSFER_OUT":               "Transfer",
    "LOAN_PAYMENTS":              "Payment",
}


def _plaid_to_category(pfc_primary: str, pfc_detailed: str = "") -> str:
    """Map Plaid PFC → our soft-enum category.

    Uses the detailed PFC for a couple of common refinements (e.g., distinguishing
    Groceries from general Dining); otherwise falls back to the primary map.
    """
    if not pfc_primary:
        return ""
    p = str(pfc_primary).upper()
    d = str(pfc_detailed or "").upper()

    # Detailed refinements where the primary alone is too coarse.
    if "GROCER" in d:
        return "Groceries"
    if p.startswith("INCOME"):
        if "WAGES" in d or "SALARY" in d:
            return "Salary"
        return "Income"
    if "CREDIT_CARD_PAYMENT" in p:
        return "Payment"

    return _PLAID_PFC_TO_CATEGORY.get(p, p.replace("_", " ").title())


def _plaid_to_notes(plaid_txn) -> str:
    """Capture useful source detail from a Plaid transaction into the notes field.

    Mirrors the kinds of detail the PDF path captures: check numbers, currency
    of non-USD transactions, pending status, and authorized-vs-posted date split.
    Returns an empty string if nothing notable applies.
    """
    parts = []
    payment_meta = plaid_txn.get("payment_meta") or {}
    check_num = payment_meta.get("check_number") if hasattr(payment_meta, "get") else None
    if check_num:
        parts.append(f"check #{check_num}")

    if plaid_txn.get("pending"):
        parts.append("pending")

    currency = plaid_txn.get("iso_currency_code") or plaid_txn.get("unofficial_currency_code")
    if currency and currency != "USD":
        parts.append(f"currency: {currency}")

    authorized = plaid_txn.get("authorized_date")
    txn_date = plaid_txn.get("date")

    def _iso(d):
        return d.isoformat() if hasattr(d, "isoformat") else str(d) if d else ""

    a, t = _iso(authorized), _iso(txn_date)
    if a and t and a != t:
        parts.append(f"authorized {a}")

    return "; ".join(parts)


class PlaidError(Exception):
    """Raised when the Plaid API fails or required configuration is missing."""


class PlaidClient:
    """Client for fetching transactions from the Plaid sandbox.

    Required environment variables (loaded from .env if present):
      - PLAID_CLIENT_ID: Plaid client ID
      - PLAID_SECRET: Plaid secret key
      - PLAID_ACCESS_TOKEN: access token for the linked item
    """

    def __init__(self):
        load_dotenv()
        self.client_id = os.environ.get("PLAID_CLIENT_ID")
        self.secret = os.environ.get("PLAID_SECRET")
        self.access_token = os.environ.get("PLAID_ACCESS_TOKEN")

        missing = [
            name
            for name, value in (
                ("PLAID_CLIENT_ID", self.client_id),
                ("PLAID_SECRET", self.secret),
                ("PLAID_ACCESS_TOKEN", self.access_token),
            )
            if not value
        ]
        if missing:
            raise PlaidError(
                f"Missing required environment variable(s): {', '.join(missing)}"
            )

        configuration = plaid.Configuration(
            # host=plaid.Environment.Sandbox,
            host=plaid.Environment.Production,
            api_key={"clientId": self.client_id, "secret": self.secret},
        )
        api_client = plaid.ApiClient(configuration)
        self._api = plaid_api.PlaidApi(api_client)

    def get_transactions(self, days: int = 30) -> List[Transaction]:
        """Fetch transactions for the linked account over the past `days` days."""
        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        request = TransactionsGetRequest(
            access_token=self.access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(count=500, offset=0),
        )

        try:
            response = self._api.transactions_get(request)
        except plaid.ApiException as e:
            raise PlaidError(f"Plaid /transactions/get failed: {e.body}") from e
        except Exception as e:
            raise PlaidError(f"Unexpected error calling Plaid: {e}") from e

        from src.statement_ingester import _account_id, _normalize_account_type, _user_id
        from src.storage import TransactionStore

        user_id = _user_id()

        # Map each Plaid account to one of our deterministic account records.
        plaid_to_account: dict = {}
        for account in response["accounts"]:
            acct_type = _normalize_account_type(_enum_value(account.get("subtype")))
            if acct_type == "unknown":
                acct_type = _normalize_account_type(_enum_value(account.get("type")))
            mask = str(account.get("mask") or "")
            name = account.get("official_name") or account.get("name") or ""
            our_id = _account_id(user_id, acct_type, mask)
            plaid_to_account[account["account_id"]] = (our_id, acct_type)
            TransactionStore.upsert_account(
                Account(
                    id=our_id, user_id=user_id, bank="", name=name,
                    mask=mask, type=acct_type, source="plaid",
                )
            )

        transactions: List[Transaction] = []
        for txn in response["transactions"]:
            transactions.append(
                _plaid_to_transaction(txn, plaid_to_account, user_id)
            )
        return transactions


def _plaid_to_transaction(txn, plaid_to_account: dict, user_id: str) -> "Transaction":
    """Build an Option-5-compliant Transaction from one Plaid transaction.

    Pure transformation — no I/O — so it's easy to unit-test with synthetic
    Plaid responses (plain dicts work in place of the Plaid SDK objects).

    Plaid stores signed amounts; we store magnitudes and let section_type
    carry direction. See design/storage.md.
    """
    our_account_id, account_type = plaid_to_account.get(
        txn["account_id"], ("", "unknown")
    )

    pfc = txn.get("personal_finance_category")
    pfc_primary = pfc.get("primary") if pfc else None
    pfc_detailed = pfc.get("detailed") if pfc else None

    plaid_amount = float(txn["amount"])
    transaction_code = _enum_value(txn.get("transaction_code"))

    description = txn.get("merchant_name") or txn.get("name") or ""

    date_val = txn.get("date")
    date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val)

    return Transaction(
        id=txn["transaction_id"],
        date=date_str,
        amount=abs(plaid_amount),               # magnitude only
        description=description,
        category=_plaid_to_category(pfc_primary, pfc_detailed),
        account_type=account_type,
        user_id=user_id,
        account_id=our_account_id,
        source="plaid",
        flow_type=_plaid_flow_type(pfc_primary),
        notes=_plaid_to_notes(txn),
        section_type=_plaid_section_type(
            amount=plaid_amount,                # signed — sign drives direction
            account_type=account_type,
            pfc_primary=pfc_primary or "",
            transaction_code=transaction_code,
        ),
    )
