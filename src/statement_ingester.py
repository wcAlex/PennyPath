import csv
import hashlib
import json
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple

import pymupdf4llm

from src.models import Account, Transaction

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
]

_CSV_EXPORT_INSTRUCTIONS = (
    "Could not extract transactions from this PDF. "
    "Export a CSV from your bank instead:\n"
    "  Chase → Accounts → Download → CSV\n"
    "  Citi → View Statements → Download Activity CSV\n"
    "  Discover → Manage → Download All Transactions → CSV\n"
    "  BofA → Download → Microsoft Excel Format (CSV)"
)


def _normalize_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(raw: str) -> Optional[float]:
    cleaned = raw.strip().replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _user_id() -> str:
    """Slugified user name from config; used to scope accounts and transactions."""
    from src.storage import UserConfigStore
    name = (UserConfigStore.load().name or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "local_user"


def _account_id(user_id: str, account_type: str, mask: str) -> str:
    """Deterministic account id so re-ingesting the same account links to one row."""
    key = f"{user_id}|{account_type}|{mask}".lower()
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _normalize_account_type(raw: str) -> str:
    t = (raw or "").strip().lower()
    if t in ("checking", "credit", "savings"):
        return t
    if "check" in t:
        return "checking"
    if any(k in t for k in ("credit", "card", "visa", "mastercard", "amex")):
        return "credit"
    if "saving" in t:
        return "savings"
    return "unknown"


def _mask_from_filename(name: str) -> str:
    """Fallback last-4 from a filename, e.g. '...-statements-0418-.pdf' → '0418'.

    Skips 4-digit groups that look like a year (19xx / 20xx). Kept around as a
    sanity cross-check against the LLM-extracted mask, not as the primary path.
    """
    for m in re.finditer(r"[-_](\d{4})(?=[-_.])", name):
        digits = m.group(1)
        if not (digits.startswith("19") or digits.startswith("20")):
            return digits
    return ""


def _last4(s: str) -> str:
    """Last-4 digits of the trailing numeric group in an account-number string.

    The LLM returns the account number as written in the document, e.g.
    'XXXX XXXX XXXX 7370', 'Account ending in 0418', or '123456789012'.
    We grab the last contiguous run of digits and take its trailing 4.
    Returns '' if no 4+ digit group is found.
    """
    if not s:
        return ""
    groups = re.findall(r"\d{4,}", s)
    return groups[-1][-4:] if groups else ""


def _chunk_text(text: str, max_chars: int = 100_000) -> List[str]:
    """Split at max_chars, never mid-table.

    pymupdf4llm renders tables as markdown pipe rows (lines starting with '|').
    Walking backward to the last newline before a non-'|' line ensures we always
    split between sections, not inside a transaction row.
    """
    chunks = []
    while len(text) > max_chars:
        split_at = max_chars  # fallback: hard split if entire window is one table
        pos = max_chars
        while pos > max_chars // 2:
            nl = text.rfind("\n", 0, pos)
            if nl == -1:
                break
            after = nl + 1
            if after >= len(text) or text[after] != "|":
                split_at = nl
                break
            pos = nl  # this newline is inside a table row; keep walking back
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


def _llm_json(prompt: str, max_tokens: int = 8192):
    """Send a prompt to the LLM and return the parsed JSON response.

    max_tokens defaults to 8192 because a JSON array of ~150 transaction rows
    (the largest single chunk we extract — see _parse_pdf_v2 chunk_size) is
    about 6–7K output tokens. Default API limits (4K) truncate that and the
    truncated response fails to parse as JSON, losing the entire chunk.

    Uses an explicit 120s timeout with 2 retries on timeout/connection errors.
    Without this, a stalled TCP socket can hang the ingest indefinitely.
    """
    from openai import APIConnectionError, APITimeoutError

    from src.llm_orchestrator import _client, _model
    client = _client().with_options(timeout=120.0)

    last_err = None
    for attempt in range(1, 4):
        try:
            response = client.chat.completions.create(
                model=_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            break
        except (APITimeoutError, APIConnectionError) as e:
            last_err = e
            print(f"Warning: LLM call timed out/disconnected, attempt {attempt}/3: {type(e).__name__}")
            if attempt == 3:
                raise
    raw = response.choices[0].message.content.strip()
    # strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def _extract_statement_metadata(header: str) -> dict:
    """Extract account metadata and summary totals from a statement's first 2 pages."""
    prompt = (
        "From this bank or credit-card statement text (the first two pages), "
        "extract account metadata and the statement summary totals.\n"
        "Return ONLY a valid JSON object — no explanation, no markdown.\n\n"
        "Fields:\n"
        '  bank: issuing bank / institution name (e.g. "Bank of America"), or ""\n'
        '  account_name: product / account name '
        '(e.g. "Adv Plus Banking", "Customized Cash Rewards Visa"), or ""\n'
        '  account_number: the account number AS WRITTEN in the document.\n'
        "                  Look in the account-summary block for labels such as:\n"
        '                  "Account Number:", "Account ending in:", "Account #",\n'
        "                  or a masked pattern like 'XXXX XXXX XXXX NNNN'.\n"
        "                  Return the raw string verbatim, e.g.\n"
        '                  "XXXX XXXX XXXX 7370" or "123456789012". Do NOT trim,\n'
        "                  re-format, or pull out only the last 4 digits — we will\n"
        "                  derive the last-4 ourselves.\n"
        "                  IGNORE: page numbers (e.g. 'Page 1 of 6'), customer IDs,\n"
        "                  phone numbers, statement reference codes / barcodes,\n"
        "                  routing numbers, and any random 4-digit groups that are\n"
        "                  not labeled as an account number.\n"
        '                  Use "" if no account number is clearly identifiable.\n'
        '  account_number_count: integer — count of DISTINCT account numbers that\n'
        "                  appear in this text. Normally 1. If the statement covers\n"
        "                  multiple accounts (combined statement), return >1.\n"
        '  account_type: one of "checking", "credit", "savings", "unknown"\n'
        '  period_start: statement period start "YYYY-MM-DD", or ""\n'
        '  period_end: statement period end "YYYY-MM-DD", or ""\n'
        "  previous_balance: opening/previous balance as a signed float — preserve the\n"
        "                    sign exactly as on the statement (e.g. -154.14 for a credit\n"
        "                    balance where the bank owes the user). Or null.\n"
        "  new_balance: closing/new balance as a signed float (same sign rule). Or null.\n"
        "  total_purchases: ONLY for credit-card statements — the statement's\n"
        "                   'Total Purchases' figure as a positive float. Return\n"
        "                   null for checking/savings statements (they do not\n"
        "                   have a 'purchases' total — debit-card purchases are\n"
        "                   lumped into 'Withdrawals and other subtractions').\n"
        "  total_payments:  ONLY for credit-card statements — the statement's\n"
        "                   'Payments and Other Credits' / 'Total Payments'\n"
        "                   figure as a positive float (money paid TOWARD the\n"
        "                   card). Return null for checking/savings statements —\n"
        "                   deposits on a checking statement are NOT 'payments'\n"
        "                   in this sense and must NOT be mapped to this field.\n"
        "  total_fees:      total bank-imposed fees charged as a positive float,\n"
        "                   or null if the statement reports no fees total.\n"
        "  total_interest:  total interest charged (CC) or interest credited\n"
        "                   (checking) as a positive float, or null.\n"
        "  total_cash_advances: ONLY for credit-card statements — the statement's\n"
        "                   'Cash Advances' figure as a positive float, or null.\n"
        "                   Cash advances are tracked separately from purchases on\n"
        "                   a CC statement (different APR, no grace period).\n\n"
        "Use null for any field you cannot find — do not guess.\n\n"
        f"Statement text:\n{header}"
    )
    try:
        meta = _llm_json(prompt)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


# Reconciliation thresholds. Per-comparison: gap = abs(parsed - expected).
#   gap <= RECON_CLEAN_DOLLAR                    -> clean (no warning)
#   gap <= RECON_WARN_DOLLAR OR <= RECON_WARN_PCT * abs(expected) -> warn
#   above both                                   -> fail (promotes file to parse_error)
# See design/storage.md Step 5.
RECON_CLEAN_DOLLAR = 0.01
RECON_WARN_DOLLAR = 5.00
RECON_WARN_PCT = 0.05


def _reconcile(transactions: List, meta: dict) -> Tuple[Optional[str], Optional[str]]:
    """Compare per-section parsed sums against statement-reported totals.

    Buckets are keyed by section_type — the structural label the LLM extracted
    from the section heading. `amount` is always a positive magnitude, so bucket
    sums are simple sums.

    Statement totals map to section buckets:
      total_purchases      ↔ section_type='purchase'
      total_payments       ↔ section_type='payment'  (CC payments received)
      total_fees           ↔ section_type='fee'
      total_interest       ↔ section_type='interest_charged' (CC) or 'interest_credited' (checking)
      total_cash_advances  ↔ section_type='cash_advance' (CC only)
      new_balance − previous_balance ↔ per-account balance flow, see below

    Per-account balance flow uses SECTION_DIRECTION as the user-benefit map,
    then flips for credit-card balance accounting (where 'out' means balance
    owed went up). See storage.py v_transactions_signed for the matching view.

    Returns (warning, error). Three outcomes:
      (None, None)  — every comparison passed clean (gap ≤ $0.01)
      (str,  None)  — at least one comparison in the warn band; file still saves
      (None, str)   — at least one comparison failed; file is refused. If both
                      warnings and errors exist, errors absorb them in the message.

    Comparisons whose expected value is null on the statement are skipped —
    we can't reconcile what the source didn't report. Unknown-section_type or
    unknown-flow_type rows are flagged as warnings (never errors): the row's
    amount is still captured, but it can't contribute to per-section recon.
    """
    warnings: List[str] = []
    errors: List[str] = []

    def _as_float(val) -> Optional[float]:
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _classify(name: str, parsed: float, expected: float) -> None:
        gap = abs(parsed - expected)
        if gap <= RECON_CLEAN_DOLLAR:
            return
        msg = f"{name}: parsed ${parsed:.2f}, statement says ${expected:.2f}"
        if gap <= RECON_WARN_DOLLAR or gap <= RECON_WARN_PCT * abs(expected):
            warnings.append(msg)
        else:
            errors.append(msg)

    account_type = str(meta.get("account_type", "")).lower()

    # Bucket magnitudes by section_type. All values are positive.
    section_sums: dict = {st: 0.0 for st in _ALLOWED_SECTION_TYPES}
    section_sums[""] = 0.0  # for rows the LLM didn't tag
    for t in transactions:
        st = getattr(t, "section_type", "") or ""
        if st not in section_sums:
            section_sums[st] = 0.0
        section_sums[st] += t.amount

    total_purchases     = _as_float(meta.get("total_purchases"))
    total_payments      = _as_float(meta.get("total_payments"))
    total_interest      = _as_float(meta.get("total_interest"))
    total_fees          = _as_float(meta.get("total_fees"))
    total_cash_advances = _as_float(meta.get("total_cash_advances"))
    prev_bal            = _as_float(meta.get("previous_balance"))
    new_bal             = _as_float(meta.get("new_balance"))

    if total_purchases is not None:
        _classify("purchases", section_sums["purchase"], total_purchases)

    if total_cash_advances is not None:
        _classify("cash advances", section_sums["cash_advance"], total_cash_advances)

    if total_payments is not None:
        # Statement's "Payments and Other Credits" total covers both card
        # payments and any merchant refunds posted in the same section.
        parsed_payments = section_sums["payment"] + section_sums["refund"]
        _classify("payments", parsed_payments, total_payments)

    if total_interest is not None:
        # Credit cards report total_interest as interest CHARGED (a debit).
        # Checking statements report total_interest as interest CREDITED (paid to you).
        if account_type == "credit":
            _classify("interest", section_sums["interest_charged"], total_interest)
        else:
            _classify("interest", section_sums["interest_credited"], total_interest)

    if total_fees is not None:
        _classify("fees", section_sums["fee"], total_fees)

    # Per-account balance flow: derive signed account_flow from section_type
    # using the same convention as the v_transactions_signed view in storage.py.
    if prev_bal is not None and new_bal is not None:
        if account_type == "credit":
            # Balance OWED rises with purchase/cash_advance/interest_charged/fee,
            # falls with payment/refund/interest_credited.
            up_set   = ("purchase", "cash_advance", "interest_charged", "fee")
            down_set = ("payment", "refund", "interest_credited")
        else:
            # Balance rises with deposit/interest_credited/refund, falls with withdrawal/check/fee/interest_charged.
            up_set   = ("deposit", "interest_credited", "refund")
            down_set = ("withdrawal", "check", "fee", "interest_charged")
        parsed_net = sum(section_sums[s] for s in up_set) - sum(section_sums[s] for s in down_set)
        expected_net = new_bal - prev_bal
        _classify("net change", parsed_net, expected_net)

    untagged_section = sum(
        1 for t in transactions
        if not (getattr(t, "section_type", "") or "")
    )
    if untagged_section:
        warnings.append(f"{untagged_section} row(s) missing section_type")

    unknown_flow = sum(
        1 for t in transactions
        if (getattr(t, "flow_type", "") or "") == "unknown"
    )
    if unknown_flow:
        warnings.append(f"{unknown_flow} row(s) unclassified (flow_type=unknown)")

    if errors:
        msg = "; ".join(errors)
        if warnings:
            msg += f"; also: {'; '.join(warnings)}"
        return None, msg
    return ("; ".join(warnings) if warnings else None), None


_ALLOWED_FLOW_TYPES = {"spending", "transfer", "interest", "fee", "refund", "income"}

# Closed enum of section labels we expect to find on a statement. The LLM picks
# one of these for every row, based on the section heading the row appears under.
# Direction is purely a per-row property derived from section_type — the LLM
# never computes signs. See design/storage.md.
_ALLOWED_SECTION_TYPES = {
    "deposit",            # checking: money in (e.g. "Deposits and other additions")
    "withdrawal",         # checking: money out (e.g. "Withdrawals and other subtractions")
    "check",              # checking: money out via check
    "fee",                # bank-imposed fee on either account type
    "interest_charged",   # CC: interest owed; checking: interest deducted (rare)
    "interest_credited",  # checking: interest deposited
    "payment",            # CC: payment received toward balance owed
    "purchase",           # CC: merchant purchase
    "cash_advance",       # CC: cash advance — increases balance owed (separate from purchase)
    "refund",             # CC or checking: merchant refund / reversal
}

# Direction of each section_type from the **user-benefit** perspective:
#   "in"  — money flows TO the user (deposit, refund, interest credit, CC payment)
#   "out" — money flows FROM the user (withdrawal, purchase, fee, interest charge)
# Per-account *balance* direction is different for credit vs checking — see the
# v_transactions_signed view in storage.py for that mapping.
SECTION_DIRECTION = {
    "deposit":           "in",
    "interest_credited": "in",
    "payment":           "in",
    "refund":            "in",
    "withdrawal":        "out",
    "check":             "out",
    "fee":               "out",
    "interest_charged":  "out",
    "purchase":          "out",
    "cash_advance":      "out",
}


# Defensive filter: descriptions that are ALWAYS summary/total rows, never
# real transactions. The LLM prompt tells it not to extract these, but the
# Chase CC Account Summary table reliably trips it up — the summary rows have
# dates and amounts and look transaction-like.
_SUMMARY_ROW_DESCRIPTIONS = {
    "previous balance", "new balance", "beginning balance", "ending balance",
    "total purchases", "total payments", "total fees", "total interest",
    "total payments and other credits", "total fees charged",
    "total interest charged", "total deposits and other additions",
    "total withdrawals and other subtractions", "total checks",
    "total service fees", "total deposits", "total withdrawals",
    "fees charged", "interest charged", "cash advances", "balance transfers",
    "payments, credits", "payment, credits", "purchases", "payments",
    "deposits and additions", "electronic withdrawals", "atm & debit card withdrawals",
}


def _is_summary_row(description: str) -> bool:
    """True if the description matches a known summary/total label.

    Trims whitespace, lowercases, and strips trailing punctuation.
    """
    d = (description or "").strip().lower().rstrip(":.")
    return d in _SUMMARY_ROW_DESCRIPTIONS


def _parse_magnitude(raw_amount) -> Optional[float]:
    """Parse a raw amount string/number and return its absolute magnitude.

    The LLM passes amounts verbatim from the statement (e.g. "6,391.79",
    "-788.29", "$1,234.56", "(45.00)"). We strip dollar signs, commas,
    accounting parentheses, and any leading sign — the sign is determined
    by section_type, not by the raw text.
    """
    if raw_amount is None:
        return None
    s = str(raw_amount).strip()
    if not s:
        return None
    # Accounting style: (45.00) → -45.00 (we'll abs it below anyway)
    s = s.replace("(", "-").replace(")", "")
    # Drop $, commas, AND all whitespace — Chase prints checking-account debits
    # like "- 10,000.00" with a space between the minus sign and the digits,
    # which makes float() fail. Stripping all whitespace handles that case.
    s = s.replace("$", "").replace(",", "")
    s = "".join(s.split())
    try:
        return abs(float(s))
    except (TypeError, ValueError):
        return None


def _extract_via_llm(chunk: str) -> List[dict]:
    """Send one text chunk to the LLM and return a list of raw transaction dicts.

    The LLM never computes signs. It returns each row's raw amount string AS
    WRITTEN in the source, and a section_type label naming which section heading
    the row appeared under. Our code derives the magnitude and direction.
    """
    prompt = (
        "Extract every transaction from this bank or credit-card statement text.\n"
        "Return ONLY a valid JSON array — no explanation, no markdown, no other text.\n\n"
        "Each element must have exactly these fields:\n"
        '  date: "YYYY-MM-DD"\n'
        '  raw_amount: the amount AS WRITTEN in the source, verbatim, as a string.\n'
        '              Examples: "6,391.79", "-788.29", "$1,234.56", "(45.00)".\n'
        '              Preserve dollar signs, commas, minus signs, or accounting\n'
        '              parentheses exactly as they appear. The CODE strips signs\n'
        '              and parses the number — you do NOT decide the sign.\n'
        '  description: merchant name or payee, verbatim from the statement\n'
        '  section_type: which section heading the row appears under (closed enum,\n'
        "                see SECTION_TYPE below)\n"
        '  flow_type: one of "spending" | "transfer" | "interest" | "fee" | "refund" | "income"\n'
        '  category: see CATEGORY section below\n'
        "  notes: free-text capturing source detail that does NOT fit the other\n"
        '         fields. "" (empty string) if nothing notable. See NOTES section.\n\n'
        "SECTION_TYPE (closed enum — pick exactly one per row by reading the\n"
        "section heading the row appears under in the source markdown):\n\n"
        "  *** CRITICAL: section_type is per the STATEMENT'S account perspective. ***\n"
        "  On a CHECKING / SAVINGS statement, the ONLY allowed values are:\n"
        "    deposit, withdrawal, check, fee, interest_credited, interest_charged, refund.\n"
        "  Specifically: a row labeled 'Payment to Chase Card', 'Online Payment',\n"
        "  'Bill Pay', 'ACH Debit', or any other money-LEAVING-this-bank-account row\n"
        "  is ALWAYS section_type='withdrawal' (set flow_type='transfer' if the money\n"
        "  is going to another of the user's own accounts). NEVER use section_type=\n"
        "  'payment' or 'purchase' on a checking/savings statement — those values\n"
        "  only exist on credit-card statements.\n\n"
        "  On a CREDIT CARD statement, the ONLY allowed values are:\n"
        "    purchase, payment, refund, fee, interest_charged, interest_credited,\n"
        "    cash_advance.\n"
        "  NEVER use 'deposit', 'withdrawal', or 'check' on a CC statement.\n\n"
        "  deposit            — checking/savings only: under 'Deposits and other\n"
        "                       additions', 'ACH Credits', 'Direct Deposits', etc.\n"
        "  withdrawal         — checking/savings only: under 'Withdrawals and other\n"
        "                       subtractions', 'ACH Debits', 'Electronic Withdrawals',\n"
        "                       'Online Payments', 'Bill Payments', or anywhere money\n"
        "                       leaves the bank account — INCLUDING payments to a\n"
        "                       credit card, transfers to savings, ATM withdrawals,\n"
        "                       debit-card purchases, and Zelle sends.\n"
        "  check              — checking only: under 'Checks' / 'Checks paid'.\n"
        "  fee                — bank-imposed fee on either account type, under\n"
        "                       'Service fees', 'FEES CHARGED', etc. (Embedded fees\n"
        "                       like CONVENFEE on a transaction line are part of a\n"
        "                       purchase — classify as withdrawal/purchase with\n"
        "                       flow_type=spending.)\n"
        "  interest_charged   — CC: under 'INTEREST CHARGED' — interest you OWE.\n"
        "  interest_credited  — checking: under 'Interest paid' — interest the bank\n"
        "                       deposited TO your account.\n"
        "  payment            — CC only: under 'PAYMENTS AND OTHER CREDITS' — a\n"
        "                       payment you made TOWARD the card. (The mirroring row\n"
        "                       on the funding checking statement is a 'withdrawal'.)\n"
        "  purchase           — CC only: under 'PURCHASE' / 'PURCHASES'.\n"
        "  cash_advance       — CC only: under 'CASH ADVANCES' — withdrawing cash\n"
        "                       against the card's line. Increases balance owed but\n"
        "                       tracked separately from purchases (different APR).\n"
        "  refund             — merchant refund, reversal, or chargeback. On a CC\n"
        "                       statement often appears under 'PAYMENTS AND OTHER\n"
        "                       CREDITS' but is NOT a card payment.\n\n"
        "flow_type meaning (semantic — what kind of money movement, not where it\n"
        "appeared in the statement):\n"
        "  spending  — a real purchase from a merchant\n"
        "  transfer  — internal money movement between the user's own accounts.\n"
        '              On a CC statement: section_type=payment is always transfer.\n'
        "              On a checking statement: a withdrawal that goes to another\n"
        "              of the user's own accounts (e.g. paying off a credit card,\n"
        "              transferring to savings).\n"
        "  interest  — interest charged or credited (mirrors section_type=interest_*)\n"
        "  fee       — BANK-IMPOSED fees only (mirrors section_type=fee)\n"
        "  refund    — a merchant refund, reversal, or chargeback\n"
        "  income    — salary, dividend, deposit on a checking/savings statement\n"
        "              (most section_type=deposit rows are income; an exception is\n"
        "              a transfer FROM another of the user's own accounts)\n\n"
        "CATEGORY (soft enum):\n"
        "  When flow_type=spending, pick from this preferred list if one fits:\n"
        "    Dining, Groceries, Transportation, Travel, Entertainment, Shopping,\n"
        "    Utilities, Healthcare, Insurance, Housing, Personal Care, Education,\n"
        "    Subscriptions, Sports & Recreation, Childcare\n"
        "  If none of these fit, invent a new short category name — do not force-fit.\n"
        "  For other flow_types, use a natural label:\n"
        "    transfer → \"Payment\" or \"Transfer\"\n"
        "    interest → \"Interest\"\n"
        "    fee      → \"Bank Fees\"\n"
        "    refund   → original category if knowable (e.g. \"Shopping\" for a store\n"
        "               return), else \"Refund\"\n"
        "    income   → \"Salary\", \"Investment Income\", \"Refund\", etc.\n\n"
        "NOTES — capture source detail that does not fit the other columns. Use \"\"\n"
        "when nothing notable applies. Multiple artifacts: semicolon-join.\n"
        "  Foreign-currency conversion → 'NZD 6.61 @ 0.593040847'\n"
        "  Posting date distinct from transaction date → 'posted 08/06'\n"
        "  Bank reference / transaction ID → 'ref: 24710000120007316406'\n"
        "  Zelle / wire / ACH memo line → 'memo: rent July'\n"
        "  Check number → 'check #1234'\n"
        "  Combined → 'NZD 6.61 @ 0.593; posted 08/06'\n\n"
        "Do NOT extract rows that are SUMMARY or TOTAL lines. These are NOT\n"
        "transactions even though they have dollar amounts:\n"
        "  - Balance snapshots: 'Daily Ledger Balance', 'Beginning balance',\n"
        "    'Ending balance', 'Previous balance', 'New balance'.\n"
        "  - Section totals: 'Total purchases', 'Total payments and other credits',\n"
        "    'Total interest charged', 'Total fees charged', 'Total deposits and\n"
        "    other additions', 'Total withdrawals and other subtractions',\n"
        "    'Total checks', 'Total service fees', and any other 'Total <category>'\n"
        "    line that summarizes a section.\n"
        "  - Per-category breakdowns on the summary / front page, such as an\n"
        "    'AT A GLANCE', 'SPENDING BY CATEGORY', or 'Account Summary' table\n"
        "    listing one $ figure per category (Dining $225, Travel $500, etc.).\n"
        "    Those are roll-ups of the individual purchases that appear in detail\n"
        "    later — extracting them double-counts.\n"
        "  - Period summaries: 'Year-to-date', 'YTD totals', 'Statement period\n"
        "    summary'.\n"
        "Only extract individual transaction LINES — each with a specific\n"
        "transaction date, a specific merchant or payee, and an amount that\n"
        "represents ONE money-movement event.\n\n"
        "If no transactions are found, return an empty array: []\n\n"
        f"Statement text:\n{chunk}"
    )
    rows = _llm_json(prompt)
    return rows if isinstance(rows, list) else []


def _normalize_flow_type(raw) -> str:
    t = str(raw or "").strip().lower()
    return t if t in _ALLOWED_FLOW_TYPES else "unknown"


# Defensive remap: if the LLM puts a CC-only section_type on a checking row
# (or vice-versa), translate to the right side based on direction. This is a
# safety net; the prompt is the primary defense. See storage.md.
_CHECKING_SECTION_REMAP = {
    "payment":  "withdrawal",   # CC payment received → money LEFT the checking account
    "purchase": "withdrawal",   # CC purchase (shouldn't appear here) → spending out
}
_CC_SECTION_REMAP = {
    "deposit":    "payment",    # money "deposited" on a CC = payment received
    "withdrawal": "purchase",   # money "withdrawn" on a CC = purchase
    "check":      "purchase",
}


def _normalize_section_type(raw, account_type: str = "") -> str:
    """Coerce LLM's section_type to our closed enum; '' if not recognized.

    If account_type is provided, also corrects cross-domain misuse (e.g. a
    'payment' label on a checking statement → 'withdrawal').
    """
    t = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    t = t if t in _ALLOWED_SECTION_TYPES else ""
    if not t or not account_type:
        return t
    if account_type in ("checking", "savings"):
        return _CHECKING_SECTION_REMAP.get(t, t)
    if account_type == "credit":
        return _CC_SECTION_REMAP.get(t, t)
    return t


def _parse_csv(path: str, user_id: str) -> List[Transaction]:
    from src.storage import TransactionStore

    source_file = os.path.basename(path)
    transactions: List[Transaction] = []
    accounts: dict = {}

    # CSVs don't have statement-section context. We map flow_type → a best-effort
    # section_type so the magnitude-only storage contract still holds.
    _CSV_FLOW_TO_SECTION = {
        "spending":  "purchase",     # close enough for CC-style CSVs
        "transfer":  "payment",
        "interest":  "interest_charged",
        "fee":       "fee",
        "refund":    "refund",
        "income":    "deposit",
    }

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, start=2):
            date = _normalize_date(row.get("date", "") or "")
            magnitude = _parse_magnitude(row.get("amount", "") or "")
            description = (row.get("description", "") or "").strip()
            category = (row.get("category", "") or "").strip()
            account_type = _normalize_account_type(row.get("account_type", "") or "")
            mask = (row.get("account_mask", "") or "").strip()
            bank = (row.get("bank", "") or "").strip()
            flow_type = _normalize_flow_type(row.get("flow_type"))
            if flow_type == "unknown":
                flow_type = "spending"  # most CSV rows are spending; explicit override wins
            # Allow explicit section_type column; otherwise derive from flow_type.
            section_type = _normalize_section_type(row.get("section_type"), account_type)
            if not section_type:
                section_type = _CSV_FLOW_TO_SECTION.get(flow_type, "")

            if date is None or magnitude is None or not description:
                print(f"Warning: skipping row {lineno} in {source_file}: could not parse")
                continue
            if account_type == "unknown":
                print(f"Warning: skipping row {lineno} in {source_file}: invalid account_type")
                continue

            account_id = _account_id(user_id, account_type, mask)
            if account_id not in accounts:
                accounts[account_id] = Account(
                    id=account_id, user_id=user_id, bank=bank, name="",
                    mask=mask, type=account_type, source="statement",
                )
            transactions.append(
                Transaction(
                    id="",
                    date=date,
                    amount=magnitude,
                    description=description,
                    category=category,
                    account_type=account_type,
                    source_file=source_file,
                    user_id=user_id,
                    account_id=account_id,
                    source="statement_csv",
                    flow_type=flow_type,
                    notes=(row.get("notes", "") or "").strip(),
                    section_type=section_type,
                )
            )

    for account in accounts.values():
        TransactionStore.upsert_account(account)
    for i, t in enumerate(transactions):
        t.id = f"{source_file}#{i}"
    return transactions


def _parse_pdf_v2(path: str, user_id: str) -> Tuple[List[Transaction], str, Optional[str], Optional[str]]:
    """Parse one PDF statement into Transactions.

    Returns (transactions, parse_method, error_message, recon_warning).
      error_message is None on success.
      recon_warning is None when totals reconcile cleanly.
    On error, transactions is empty and the caller MUST NOT touch stored rows
    for this file — that preserves any previously-ingested good data.

    Pipeline (see design/storage.md):
      1a. Render full document for activity extraction.
      1b. Render first 2 pages for metadata extraction.
      2.  LLM: account identifier + summary totals.
      3.  Multi-account guard.
      4.  Derive last-4 mask; upsert account.
      5.  LLM: chunked activity extraction.
      6.  Two-tier reconciliation against summary.
    """
    from src.storage import TransactionStore

    source_file = os.path.basename(path)

    # 1a. Full-document render.
    try:
        raw_text = pymupdf4llm.to_markdown(path)
    except Exception as e:
        return [], "llm", f"{_CSV_EXPORT_INSTRUCTIONS}\n(PDF read error: {e})", None
    if not raw_text or not raw_text.strip():
        return [], "llm", _CSV_EXPORT_INSTRUCTIONS, None

    # 1b. First-2-pages render for metadata. Two pages because some banks push
    # the account-summary box past the address block onto page 2.
    try:
        header_text = pymupdf4llm.to_markdown(path, pages=[0, 1])
        if not header_text or not header_text.strip():
            header_text = raw_text[:8000]
    except Exception:
        # If page-restricted render fails for any reason, fall back to first 8K
        # chars of the full render — same shape, slightly noisier.
        header_text = raw_text[:8000]

    # 2. Extract account identifier + statement summary.
    meta = _extract_statement_metadata(header_text)

    # 3. Multi-account guard — refuse combined statements before doing any
    # activity work. They would silently corrupt the per-account aggregates.
    try:
        account_count = int(meta.get("account_number_count") or 1)
    except (TypeError, ValueError):
        account_count = 1
    if account_count > 1:
        return [], "llm", (
            f"multi-account statement not supported "
            f"({account_count} accounts detected on first 2 pages)"
        ), None

    # 4. Resolve account: derive last-4 deterministically from the raw account
    # number string the LLM returned. This removes "which 4 digits?" ambiguity.
    account_type = _normalize_account_type(str(meta.get("account_type", "")))
    account_number = str(meta.get("account_number", "") or "").strip()
    mask = _last4(account_number)

    # Cross-check against the filename mask (where present). Disagreement isn't
    # fatal — we trust the LLM — but worth flagging so the user can investigate.
    filename_mask = _mask_from_filename(source_file)
    mask_disagreement: Optional[str] = None
    if filename_mask and mask and filename_mask != mask:
        mask_disagreement = (
            f"mask disagreement: filename says '{filename_mask}', "
            f"LLM extracted '{mask}' from account_number='{account_number}'"
        )

    bank = str(meta.get("bank", "") or "").strip()
    account_name = str(meta.get("account_name", "") or "").strip()

    account_id = _account_id(user_id, account_type, mask)
    TransactionStore.upsert_account(
        Account(
            id=account_id, user_id=user_id, bank=bank, name=account_name,
            mask=mask, type=account_type, source="statement",
        )
    )

    # 5. Activity extraction.
    # Chunk size 10K chars: FX-heavy rows (foreign-currency triple lines) push
    # output density above what 15K chunks could survive without truncation
    # under deepseek-chat. 10K input → ~40 rows × ~250 chars JSON each = ~10K
    # output chars / ~2.5K tokens — comfortable margin under the 8K cap.
    chunks = _chunk_text(raw_text, max_chars=10000)
    all_rows: List[dict] = []
    for chunk_idx, chunk in enumerate(chunks):
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                all_rows.extend(_extract_via_llm(chunk))
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(
                    f"Warning: LLM extraction failed on chunk {chunk_idx+1}/{len(chunks)} "
                    f"of {source_file}, attempt {attempt+1}/3: {type(e).__name__}: {e}"
                )
        if last_err is not None:
            # Refuse to record a partial result — incomplete extraction would silently
            # under-count and corrupt downstream aggregates. Propagate so the caller
            # logs a parse_error and keeps any previously-ingested rows intact.
            raise RuntimeError(
                f"LLM extraction failed on chunk {chunk_idx+1}/{len(chunks)} "
                f"after 3 attempts: {last_err}"
            ) from last_err

    transactions: List[Transaction] = []
    for row in all_rows:
        date = _normalize_date(str(row.get("date", "")))
        # Option 5 contract: LLM returns raw_amount as a verbatim string.
        # Code derives magnitude (always positive) and direction (from section_type).
        # Backward-compat: also accept legacy 'amount' key.
        raw_amount = row.get("raw_amount", row.get("amount"))
        magnitude = _parse_magnitude(raw_amount)
        description = str(row.get("description", "")).strip()
        category = str(row.get("category", "")).strip()
        flow_type = _normalize_flow_type(row.get("flow_type"))
        section_type = _normalize_section_type(row.get("section_type"), account_type)
        notes = str(row.get("notes", "") or "").strip()

        if date is None or magnitude is None or not description:
            continue
        if _is_summary_row(description):
            continue  # account-summary / section-total row mislabeled as a transaction

        transactions.append(
            Transaction(
                id="",
                date=date,
                amount=magnitude,
                description=description,
                category=category,
                account_type=account_type,
                source_file=source_file,
                user_id=user_id,
                account_id=account_id,
                source="statement_pdf",
                flow_type=flow_type,
                notes=notes,
                section_type=section_type,
            )
        )

    for i, t in enumerate(transactions):
        t.id = f"{source_file}#{i}"

    # 6. Two-tier reconciliation.
    recon_warning, recon_error = _reconcile(transactions, meta)
    if recon_error:
        print(f"Refusing {source_file}: reconciliation failed: {recon_error}")
        return [], "llm", f"reconciliation failed: {recon_error}", None

    # Surface mask disagreement as a recon-band warning so it shows up in
    # `ingest` output and the `file_sources` row.
    if mask_disagreement:
        recon_warning = (
            f"{mask_disagreement}; {recon_warning}" if recon_warning else mask_disagreement
        )

    if recon_warning:
        print(f"Warning: reconciliation issues in {source_file}: {recon_warning}")

    return transactions, "llm", None, recon_warning


def ingest_statements(directory: str = "data/statements") -> List[Transaction]:
    from src.storage import TransactionStore
    TransactionStore.init_db()
    user_id = _user_id()
    # Cutoff for the rule-application hook at the bottom — only rows ingested
    # during this run are scanned. ISO timestamps compare lexically.
    _ingest_run_started = datetime.now().isoformat()

    try:
        entries = sorted(os.listdir(directory))
    except OSError as e:
        print(f"Warning: cannot read directory {directory}: {e}")
        return TransactionStore.query_all()

    for name in entries:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue

        mtime = os.path.getmtime(path)
        existing = TransactionStore.get_file_source(user_id, path)
        if (
            existing
            and existing["file_mtime"] == mtime
            and not existing.get("parse_error")
        ):
            continue  # already parsed cleanly and unchanged
        if existing and existing.get("parse_error"):
            print(f"Retrying previously-failed file: {name}")

        ext = os.path.splitext(name)[1].lower()
        transactions: List[Transaction] = []
        parse_method = "unknown"
        error: Optional[str] = None

        recon_warning: Optional[str] = None
        try:
            if ext == ".csv":
                transactions = _parse_csv(path, user_id)
                parse_method = "csv"
            elif ext == ".pdf":
                transactions, parse_method, error, recon_warning = _parse_pdf_v2(path, user_id)
            else:
                continue
        except Exception as e:
            error = str(e)
            print(f"Warning: failed to parse {name}: {e}")

        # Only touch stored rows on a successful parse — a transient failure
        # must not wipe a file's previously-ingested transactions.
        if error is None:
            TransactionStore.replace_file_transactions(name, transactions)
        TransactionStore.upsert_file_source(
            user_id, name, path, mtime, parse_method, len(transactions), error, recon_warning
        )

    # Raw is now current; rematerialize the reconciliation layer for this user
    # so the dashboard / chat / analysis all read corrected, paired data.
    from src.reconciler import rebuild_recon
    rebuild_recon(user_id)

    # Apply the user's active category rules to the rows freshly ingested in
    # this run, so a rule the user set previously keeps fixing future
    # transactions automatically. See design/overrides.md → Ingest hook.
    from src.overrides import apply_rules_to_new
    apply_rules_to_new(user_id, since_ingested_at=_ingest_run_started)

    return TransactionStore.query_all()
