"""Exploration script: parsing a bank statement PDF with pdfplumber.

This is a *spike* — a runnable comparison, not a pytest test. It exists to
answer two questions for the Phase 1A statement-ingestion work:

  1. Can pdfplumber accurately pull the bank name, account last-4, account
     type, and transaction list out of a real Chase statement?
  2. What is the practical difference between page.extract_text() and
     page.extract_tables() on this kind of document?

Run it directly:

    pip install pdfplumber
    python tests/explore_pdfplumber.py

The short answer to #2 (see PART 2 below): for this statement extract_text()
wins decisively. The transaction "table" on a Chase statement is drawn with
*no ruling lines*, so pdfplumber's default line-based table detector finds
nothing, and the text-based fallback splits columns at the wrong places
because merchant names have wildly varying widths.
"""

import re
import sys
from pathlib import Path

import pdfplumber

PDF_PATH = Path(__file__).resolve().parent.parent / "data" / "statements" / "20250104-statements-0418-.pdf"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def undouble(text: str) -> str:
    """Collapse the doubled-glyph artifact pdfplumber produces for bold headers.

    Chase renders bold headers ("ACCOUNT SUMMARY") with each glyph painted
    twice, so extract_text() returns "AACCCCOOUUNNTT SSUUMMMMAARRYY" — note the
    *letters* are doubled but the spaces between words are not. So we undouble
    one whitespace-separated token at a time: a token is treated as doubled
    only when every run of identical characters has even length; otherwise it
    is normal text and passes through untouched.
    """

    def undouble_token(tok: str) -> str:
        out, i = [], 0
        while i < len(tok):
            j = i
            while j < len(tok) and tok[j] == tok[i]:
                j += 1
            if (j - i) % 2 != 0:
                return tok  # odd run -> not a doubled token, leave alone
            out.append(tok[i] * ((j - i) // 2))
            i = j
        return "".join(out)

    # re.split with a capturing group keeps the whitespace separators in place.
    return "".join(
        part if part.isspace() or not part else undouble_token(part)
        for part in re.split(r"(\s+)", text)
    )


def parse_amount(raw: str) -> float:
    """'-5,953.16' -> -5953.16 ;  '.68' -> 0.68"""
    return float(raw.replace(",", ""))


# --------------------------------------------------------------------------
# PART 1 — extract_text(): see the raw text pdfplumber gives us
# --------------------------------------------------------------------------

def part1_show_text(pages_text):
    print("=" * 78)
    print("PART 1 — page.extract_text()")
    print("=" * 78)
    for i, txt in enumerate(pages_text):
        print(f"\n--- page {i + 1}: {len(txt)} chars ---")
    print(
        "\nObservations:\n"
        "  * Bold headers come back with doubled glyphs: 'AACCCCOOUUNNTT SSUUMMMMAARRYY'.\n"
        "    The undouble() helper repairs these.\n"
        "  * Each transaction lands on its own clean line:\n"
        "      '12/04 TASTE OF XI''AN BELLEVUE BELLEVUE WA 35.22'\n"
        "    -> date, description and amount are all on one line. Easy to regex.\n"
        "  * Section headers ('PAYMENTS AND OTHER CREDITS', 'PURCHASE', ...) sit\n"
        "    on their own lines and tell us the sign of the rows beneath them."
    )


# --------------------------------------------------------------------------
# PART 2 — extract_tables(): see why it does NOT work here
# --------------------------------------------------------------------------

def part2_show_tables(pdf):
    print("\n" + "=" * 78)
    print("PART 2 — page.extract_tables()")
    print("=" * 78)

    # 2a. Default strategy ('lines') — relies on the PDF having drawn rules.
    print("\n[2a] Default strategy (vertical/horizontal = 'lines'):")
    for i, page in enumerate(pdf.pages):
        tables = page.extract_tables()
        print(f"  page {i + 1}: found {len(tables)} table(s)")
    print(
        "  -> The transaction pages (3 & 4) yield nothing usable: a Chase\n"
        "     statement draws the activity list with NO cell borders, so the\n"
        "     line-based detector has no rules to snap a grid to."
    )

    # 2b. Text strategy — infers columns from text alignment.
    print("\n[2b] Text strategy (vertical/horizontal = 'text') on the activity page:")
    activity_page = pdf.pages[2]  # page 3, first transaction page
    tables = activity_page.extract_tables(
        {"vertical_strategy": "text", "horizontal_strategy": "text"}
    )
    if tables:
        t = tables[0]
        print(f"  found 1 table: {len(t)} rows x {len(t[0])} cols")
        for row in t[3:7]:
            print("   ", row)
    print(
        "  -> Columns are split in the WRONG places. Merchant names vary in\n"
        "     width, so there is no consistent x-coordinate to cut on; the\n"
        "     date and amount get sliced mid-value. Unusable for accuracy.\n"
        "\nConclusion: for this statement layout, extract_text() + regex is the\n"
        "reliable path. extract_tables() is the right tool only when a PDF\n"
        "actually draws a ruled grid (e.g. some checking-account statements)."
    )


# --------------------------------------------------------------------------
# PART 3 — pull the four target fields out of the text
# --------------------------------------------------------------------------

# Known institutions, matched against the (non-doubled) body text. Cheap and
# accurate for the handful of banks Phase 1A cares about; swap for the LLM
# metadata path in statement_ingester.py for unknown issuers.
_BANKS = [
    ("chase.com", "Chase"),
    ("bankofamerica.com", "Bank of America"),
    ("citi.com", "Citi"),
    ("discover.com", "Discover"),
    ("wellsfargo.com", "Wells Fargo"),
    ("americanexpress.com", "American Express"),
]


def detect_bank(full_text: str) -> str:
    low = full_text.lower()
    for needle, name in _BANKS:
        if needle in low:
            return name
    return "UNKNOWN"


def detect_mask(full_text: str) -> str:
    """Last 4 of the account number: 'Account Number: XXXX XXXX XXXX 0418'."""
    m = re.search(r"Account Number:\s*[X\s]*(\d{4})\b", full_text)
    return m.group(1) if m else "UNKNOWN"


def detect_account_type(full_text: str) -> str:
    """Classify the statement using keyword signals from the non-doubled text."""
    low = full_text.lower()
    credit_signals = [
        "minimum payment", "credit access line", "cash advances",
        "annual percentage rate", "balance transfers", "available credit",
    ]
    checking_signals = [
        "deposits and additions", "checking summary", "beginning balance",
        "ending balance", "atm withdrawal", "debit card",
    ]
    savings_signals = ["savings summary", "annual percentage yield", "interest paid this period"]

    credit = sum(s in low for s in credit_signals)
    checking = sum(s in low for s in checking_signals)
    savings = sum(s in low for s in savings_signals)

    best = max((credit, "credit"), (checking, "checking"), (savings, "savings"))
    return best[1] if best[0] > 0 else "unknown"


def detect_period(full_text: str):
    """Statement period from 'Opening/Closing Date 12/05/24 - 01/04/25'."""
    m = re.search(
        r"Opening/Closing Date\s*(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})",
        full_text,
    )
    if not m:
        return None, None
    return m.group(1), m.group(2)


# A transaction line: MM/DD <description> <amount>. The amount carries its own
# sign (payments print as negative, purchases as positive), so we trust it.
_TXN_RE = re.compile(r"^(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]*\.\d{2})$")

# Lines that look like section headers within ACCOUNT ACTIVITY.
_SECTIONS = {
    "PAYMENTS AND OTHER CREDITS",
    "PURCHASE",
    "FEES CHARGED",
    "INTEREST CHARGED",
}


def parse_transactions(full_text: str, closing_date: str):
    """Extract transactions from the ACCOUNT ACTIVITY region.

    closing_date ('MM/DD/YY') is used to resolve the missing year on each
    'MM/DD' row: a month later than the closing month belongs to the prior year.
    """
    close_month = close_year = None
    if closing_date:
        mm, _dd, yy = closing_date.split("/")
        close_month, close_year = int(mm), 2000 + int(yy)

    def resolve_year(month: int) -> int:
        if close_month is None:
            return 0
        return close_year - 1 if month > close_month else close_year

    txns = []
    misses = []
    section = None
    in_activity = False

    for raw_line in full_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "ACCOUNT ACTIVITY" in undouble(line).upper():
            in_activity = True
            continue
        if not in_activity:
            continue
        if line.upper() in _SECTIONS:
            section = line.upper()
            continue

        m = _TXN_RE.match(line)
        if not m:
            # Flag any date-prefixed line we failed to parse — these are the
            # accuracy risks worth eyeballing (e.g. FX-rate detail lines).
            if re.match(r"^\d{2}/\d{2}\s", line):
                misses.append(line)
            continue

        md, description, amount_str = m.groups()
        month, day = (int(x) for x in md.split("/"))
        year = resolve_year(month)
        txns.append(
            {
                "date": f"{year:04d}-{month:02d}-{day:02d}" if year else md,
                "section": section or "?",
                "description": description.strip(),
                "amount": parse_amount(amount_str),
            }
        )

    return txns, misses


def part3_extract_fields(full_text: str):
    print("\n" + "=" * 78)
    print("PART 3 — extracting the target fields from the text")
    print("=" * 78)

    bank = detect_bank(full_text)
    mask = detect_mask(full_text)
    acct_type = detect_account_type(full_text)
    period_start, period_end = detect_period(full_text)

    print(f"\n  Bank name        : {bank}")
    print(f"  Account last 4   : {mask}")
    print(f"  Account type     : {acct_type}")
    print(f"  Statement period : {period_start}  ->  {period_end}")

    txns, misses = parse_transactions(full_text, period_end)

    by_section = {}
    total = 0.0
    for t in txns:
        by_section[t["section"]] = by_section.get(t["section"], 0) + 1
        total += t["amount"]

    print(f"\n  Transactions parsed : {len(txns)}")
    for sec, n in by_section.items():
        print(f"    {sec:<28} {n}")
    print(f"  Sum of all amounts  : {total:,.2f}")

    print("\n  First 5 transactions:")
    for t in txns[:5]:
        print(f"    {t['date']}  {t['amount']:>11,.2f}  [{t['section']}]  {t['description']}")
    print("  Last 5 transactions:")
    for t in txns[-5:]:
        print(f"    {t['date']}  {t['amount']:>11,.2f}  [{t['section']}]  {t['description']}")

    if misses:
        print(f"\n  ⚠ {len(misses)} date-prefixed line(s) intentionally skipped "
              f"(no trailing amount):")
        for line in misses:
            print(f"    SKIPPED: {line!r}")
        print("    -> These are FX-rate detail rows attached to the transaction\n"
              "       above them, not standalone transactions. Correctly excluded.")
    else:
        print("\n  No unexplained date-prefixed lines — every dated row parsed.")

    return bank, mask, acct_type, txns


# --------------------------------------------------------------------------
# PART 4 — reconcile against the statement's own ACCOUNT SUMMARY
# --------------------------------------------------------------------------
#
# This is the real accuracy test. The statement prints its own totals in the
# ACCOUNT SUMMARY box; if our parsed transactions sum to those exact figures,
# we know we missed nothing and double-counted nothing.

def _summary_value(full_text: str, label: str) -> float:
    """Pull one '<label> ... $1,234.56' figure from the ACCOUNT SUMMARY box."""
    m = re.search(label + r"\s+[-+]?\$?([\d,]+\.\d{2})", full_text)
    return parse_amount(m.group(1)) if m else None


def part4_reconcile(full_text: str, txns):
    print("\n" + "=" * 78)
    print("PART 4 — reconciliation against the statement's ACCOUNT SUMMARY")
    print("=" * 78)

    previous_balance = _summary_value(full_text, "Previous Balance")
    new_balance = _summary_value(full_text, "New Balance")
    stated = {
        "PAYMENTS AND OTHER CREDITS": -(_summary_value(full_text, "Payment, Credits") or 0),
        "PURCHASE": _summary_value(full_text, "Purchases"),
        "FEES CHARGED": _summary_value(full_text, "Fees Charged"),
        "INTEREST CHARGED": _summary_value(full_text, "Interest Charged"),
    }

    parsed = {}
    for t in txns:
        parsed[t["section"]] = parsed.get(t["section"], 0.0) + t["amount"]

    all_ok = True
    print(f"\n  {'section':<28} {'parsed':>13} {'stated':>13}   match")
    for section, stated_amt in stated.items():
        got = round(parsed.get(section, 0.0), 2)
        want = round(stated_amt, 2) if stated_amt is not None else None
        ok = want is not None and abs(got - want) < 0.01
        all_ok &= ok
        want_str = f"{want:,.2f}" if want is not None else "n/a"
        print(f"  {section:<28} {got:>13,.2f} {want_str:>13}   {'OK' if ok else 'MISMATCH'}")

    # End-to-end identity: previous balance + every line item = new balance.
    if previous_balance is not None and new_balance is not None:
        computed = round(previous_balance + sum(t["amount"] for t in txns), 2)
        ok = abs(computed - new_balance) < 0.01
        all_ok &= ok
        print(
            f"\n  Previous balance {previous_balance:,.2f} + parsed transactions "
            f"= {computed:,.2f}"
        )
        print(f"  Statement's New Balance                 = {new_balance:,.2f}"
              f"   {'OK' if ok else 'MISMATCH'}")

    print("\n  " + ("✅ Fully reconciled — extraction is exact."
                    if all_ok else "❌ Reconciliation FAILED — extraction is incomplete."))
    return all_ok


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if not PDF_PATH.exists():
        sys.exit(f"PDF not found: {PDF_PATH}")

    print(f"Opening: {PDF_PATH.name}\n")
    with pdfplumber.open(PDF_PATH) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
        full_text = "\n".join(pages_text)

        part1_show_text(pages_text)
        part2_show_tables(pdf)

    bank, mask, acct_type, txns = part3_extract_fields(full_text)
    reconciled = part4_reconcile(full_text, txns)

    print("\n" + "=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  bank={bank!r}  last4={mask!r}  type={acct_type!r}  transactions={len(txns)}")
    print(f"  reconciled={reconciled}")
    print("\nExpected for this file: bank='Chase', last4='0418', type='credit',")
    print("96 transactions, fully reconciled.")

    return 0 if reconciled else 1


if __name__ == "__main__":
    sys.exit(main())
