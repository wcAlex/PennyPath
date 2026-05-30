from dataclasses import dataclass
from typing import Literal

AccountType = Literal["checking", "credit", "savings", "unknown"]


@dataclass
class Account:
    id: str                 # md5(user_id + type + mask)
    user_id: str
    bank: str = ""          # institution name, e.g. "Bank of America"
    name: str = ""          # product name, e.g. "Adv Plus Banking"
    mask: str = ""          # account number last 4, e.g. "0418"
    type: AccountType = "unknown"
    source: str = ""        # "plaid" | "statement"
    created_at: str = ""


@dataclass
class Transaction:
    id: str
    date: str  # ISO 8601, e.g. "2024-05-01"
    amount: float  # MAGNITUDE only — always >= 0. Direction is derived from section_type.
    description: str
    category: str
    account_type: AccountType
    source_file: str = ""   # basename of originating statement file
    user_id: str = ""
    account_id: str = ""
    source: str = ""        # "plaid" | "statement_pdf" | "statement_csv"
    flow_type: str = "unknown"  # spending | transfer | interest | fee | refund | income | unknown
    notes: str = ""         # free-form source detail (FX rate, posting date, ref code, memo, check #, …)
    section_type: str = ""  # deposit | withdrawal | check | fee | interest_charged | interest_credited | payment | purchase | refund
