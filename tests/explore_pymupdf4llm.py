"""Exploration script: parsing the same statement with PyMuPDF4LLM.

Companion to ``explore_pdfplumber.py``. Same PDF, same four target fields
(bank, last 4, account type, transactions), same reconciliation against the
statement's own ACCOUNT SUMMARY. The point is the *contrast*:

  * pdfplumber gives you positioned text and (optionally) a grid of cells.
    You drive the layout interpretation.
  * PyMuPDF4LLM gives you a Markdown document with bold, headings and tables
    already reconstructed. It is opinionated and LLM-friendly out of the box,
    at the cost of artifacts injected by that reconstruction.

Run it:

    pip install pymupdf4llm
    python tests/explore_pymupdf4llm.py
"""

import re
import sys
from pathlib import Path

import pymupdf
import pymupdf4llm

PDF_PATH = Path(__file__).resolve().parent.parent / "data" / "statements" / "20250104-statements-0418-.pdf"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_amount(raw: str) -> float:
    return float(raw.replace(",", "").replace("$", ""))


def strip_md(s: str) -> str:
    """Remove the Markdown emphasis wrappers pymupdf4llm sprinkles in."""
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)   # **bold**
    s = re.sub(r"~~(.*?)~~", r"\1", s)        # ~~strike~~
    s = re.sub(r"<br\s*/?>", " ", s)          # <br> from cells
    return s.strip()


# --------------------------------------------------------------------------
# PART 1 — to_markdown(): see what structure pymupdf4llm preserves
# --------------------------------------------------------------------------

def part1_show_markdown(md: str):
    print("=" * 78)
    print("PART 1 — pymupdf4llm.to_markdown()")
    print("=" * 78)
    print(f"\nTotal markdown size: {len(md):,} chars")

    head = md[:600]
    print("\n--- head of the document (first 600 chars) ---")
    print(head)

    # Find an interesting transaction-table excerpt.
    i = md.find("PURCHASE")
    print("\n--- excerpt around the PURCHASE section ---")
    print(md[i:i + 700])

    print(
        "\nObservations:\n"
        "  * NO doubled-glyph artifact. 'Manage your account' comes out clean.\n"
        "    PyMuPDF4LLM detects the faux-bold overprinting and folds it back.\n"
        "  * Bold survives as Markdown: '**Download the**', '**ACCOUNT SUMMARY**'.\n"
        "  * Strikethrough '~~...~~' appears around the calendar grid — pymupdf4llm\n"
        "    flags text that overlaps another text object as 'struck through'.\n"
        "  * Transactions are emitted as a real Markdown table:\n"
        "        |12/04|TASTE OF XI'AN BELLEVUE BELLEVUE WA|||||35.22|\n"
        "    pdfplumber gave us positioned lines; pymupdf4llm gives us a grid.\n"
        "  * Images are noted in-place: '==> picture [145 x 36] intentionally omitted <=='.\n"
        "  * Some phrases are duplicated ('Manage your account online at:Manage your...')\n"
        "    because bold is implemented in the source PDF by drawing the word twice;\n"
        "    pymupdf4llm de-doubles individual glyphs but keeps the word-level repeat."
    )


# --------------------------------------------------------------------------
# PART 2 — find_tables() under the hood + the LLM-friendly alternative
# --------------------------------------------------------------------------

def part2_show_tables():
    print("\n" + "=" * 78)
    print("PART 2 — PyMuPDF.find_tables() vs the to_markdown() pipeline")
    print("=" * 78)

    doc = pymupdf.open(PDF_PATH)
    try:
        print("\n[2a] page.find_tables() with each strategy on the activity page (p.3):")
        page = doc[2]
        for strat in ("lines", "lines_strict", "text"):
            tabs = page.find_tables(strategy=strat)
            print(f"  strategy={strat:<13} -> {len(tabs.tables)} table(s)")
            for ti, t in enumerate(tabs.tables):
                rows = t.extract()
                print(f"    table[{ti}]  {len(rows)} rows x "
                      f"{len(rows[0]) if rows else 0} cols")
                for row in rows[1:4]:
                    print("       ", row)
        print(
            "  -> Same story as pdfplumber: 'lines' / 'lines_strict' find NOTHING\n"
            "     because the activity table has no ruled cells. 'text' creates\n"
            "     a grid but slices columns in the wrong places. The underlying\n"
            "     table detector is the same kind of algorithm."
        )
    finally:
        doc.close()

    print(
        "\n[2b] But to_markdown() still produces a Markdown table for that\n"
        "     section. How? It runs its own pipeline on top of PyMuPDF:\n"
        "       * cluster spans into rows by y-coordinate,\n"
        "       * cluster columns by gaps in x-coordinate runs across rows,\n"
        "       * emit '|...|' rows so an LLM can consume the layout.\n"
        "     The result is over-columned (lots of empty '||' cells) but the\n"
        "     row identity is preserved — easy to recover transactions from.\n"
        "\nNet: for a borderless statement, PyMuPDF4LLM's *markdown* is the only\n"
        "table-like output you'll get from either library out of the box. To\n"
        "trust a raw find_tables() call you need a PDF with actual cell borders."
    )


# --------------------------------------------------------------------------
# PART 3 — extract the four target fields from the markdown
# --------------------------------------------------------------------------

_BANKS = [
    ("chase.com", "Chase"),
    ("bankofamerica.com", "Bank of America"),
    ("citi.com", "Citi"),
    ("discover.com", "Discover"),
    ("wellsfargo.com", "Wells Fargo"),
    ("americanexpress.com", "American Express"),
]


def detect_bank(md: str) -> str:
    low = md.lower()
    for needle, name in _BANKS:
        if needle in low:
            return name
    return "UNKNOWN"


def detect_mask(md: str) -> str:
    """'Account Number: XXXX XXXX XXXX 0418' — possibly wrapped in **/~~."""
    plain = strip_md(md)
    m = re.search(r"Account Number:\s*[X\s]*(\d{4})\b", plain)
    return m.group(1) if m else "UNKNOWN"


def detect_account_type(md: str) -> str:
    low = strip_md(md).lower()
    credit_signals = [
        "minimum payment", "credit access line", "cash advances",
        "annual percentage rate", "balance transfers", "available credit",
    ]
    checking_signals = [
        "deposits and additions", "checking summary", "beginning balance",
        "ending balance", "atm withdrawal", "debit card",
    ]
    savings_signals = ["savings summary", "annual percentage yield", "interest paid this period"]
    scored = [
        (sum(s in low for s in credit_signals), "credit"),
        (sum(s in low for s in checking_signals), "checking"),
        (sum(s in low for s in savings_signals), "savings"),
    ]
    best = max(scored)
    return best[1] if best[0] > 0 else "unknown"


def detect_period(md: str):
    plain = strip_md(md)
    m = re.search(
        r"Opening/Closing Date\s*(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})",
        plain,
    )
    return (m.group(1), m.group(2)) if m else (None, None)


# Sections appear as their own rows: '|FEES CHARGED|||||'
_SECTIONS = {
    "PAYMENTS AND OTHER CREDITS",
    "PURCHASE",
    "FEES CHARGED",
    "INTEREST CHARGED",
}

_DATE_RE = re.compile(r"^\d{2}/\d{2}$")
_AMOUNT_RE = re.compile(r"^-?\$?[\d,]*\.\d{2}$")


def parse_transactions(md: str, closing_date: str):
    """Walk the markdown table rows and recover each transaction.

    For every '|...|' row we strip markdown, split on '|', drop empty cells,
    and look for the shape [date, ...description..., amount]. Section header
    rows ('|FEES CHARGED|||') flip the current section.
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
    section = None

    for raw_line in md.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [strip_md(c) for c in line.strip("|").split("|")]
        cells = [c for c in cells if c]  # drop empties
        if not cells:
            continue

        # Section header row. pymupdf4llm sometimes splits the header across
        # cells ('PAYMENTS AND | OTHER CREDITS'), so test the joined form too.
        joined = " ".join(cells).upper()
        if joined in _SECTIONS:
            section = joined
            continue

        # Transaction row: first cell is MM/DD, last is an amount.
        if _DATE_RE.match(cells[0]) and _AMOUNT_RE.match(cells[-1]):
            md_str, amount_str = cells[0], cells[-1]
            description = " ".join(cells[1:-1]).strip()
            if not description:
                continue
            month, day = (int(x) for x in md_str.split("/"))
            year = resolve_year(month)
            txns.append({
                "date": f"{year:04d}-{month:02d}-{day:02d}" if year else md_str,
                "section": section or "?",
                "description": description,
                "amount": parse_amount(amount_str),
            })
    return txns


def part3_extract_fields(md: str):
    print("\n" + "=" * 78)
    print("PART 3 — extracting the target fields from the markdown")
    print("=" * 78)

    bank = detect_bank(md)
    mask = detect_mask(md)
    acct_type = detect_account_type(md)
    period_start, period_end = detect_period(md)

    print(f"\n  Bank name        : {bank}")
    print(f"  Account last 4   : {mask}")
    print(f"  Account type     : {acct_type}")
    print(f"  Statement period : {period_start}  ->  {period_end}")

    txns = parse_transactions(md, period_end)
    by_section = {}
    for t in txns:
        by_section[t["section"]] = by_section.get(t["section"], 0) + 1

    print(f"\n  Transactions parsed : {len(txns)}")
    for sec, n in by_section.items():
        print(f"    {sec:<28} {n}")

    print("\n  First 5 transactions:")
    for t in txns[:5]:
        print(f"    {t['date']}  {t['amount']:>11,.2f}  [{t['section']}]  {t['description']}")
    print("  Last 5 transactions:")
    for t in txns[-5:]:
        print(f"    {t['date']}  {t['amount']:>11,.2f}  [{t['section']}]  {t['description']}")

    return bank, mask, acct_type, txns


# --------------------------------------------------------------------------
# PART 4 — reconciliation against the statement's own ACCOUNT SUMMARY
# --------------------------------------------------------------------------

def _summary_value(md: str, label: str):
    plain = strip_md(md)
    m = re.search(label + r"\s+[-+]?\$?([\d,]+\.\d{2})", plain)
    return parse_amount(m.group(1)) if m else None


def part4_reconcile(md: str, txns):
    print("\n" + "=" * 78)
    print("PART 4 — reconciliation against the statement's ACCOUNT SUMMARY")
    print("=" * 78)

    previous_balance = _summary_value(md, "Previous Balance")
    new_balance = _summary_value(md, "New Balance")
    stated = {
        "PAYMENTS AND OTHER CREDITS": -(_summary_value(md, "Payment, Credits") or 0),
        "PURCHASE": _summary_value(md, "Purchases"),
        "FEES CHARGED": _summary_value(md, "Fees Charged"),
        "INTEREST CHARGED": _summary_value(md, "Interest Charged"),
    }

    parsed = {}
    for t in txns:
        parsed[t["section"]] = parsed.get(t["section"], 0.0) + t["amount"]

    all_ok = True
    print(f"\n  {'section':<28} {'parsed':>13} {'stated':>13}   match")
    for section, want in stated.items():
        got = round(parsed.get(section, 0.0), 2)
        ok = want is not None and abs(got - round(want, 2)) < 0.01
        all_ok &= ok
        want_str = f"{want:,.2f}" if want is not None else "n/a"
        print(f"  {section:<28} {got:>13,.2f} {want_str:>13}   {'OK' if ok else 'MISMATCH'}")

    if previous_balance is not None and new_balance is not None:
        computed = round(previous_balance + sum(t["amount"] for t in txns), 2)
        ok = abs(computed - new_balance) < 0.01
        all_ok &= ok
        print(f"\n  Previous balance {previous_balance:,.2f} + parsed transactions "
              f"= {computed:,.2f}")
        print(f"  Statement's New Balance                 = {new_balance:,.2f}"
              f"   {'OK' if ok else 'MISMATCH'}")

    print("\n  " + ("✅ Fully reconciled — extraction is exact."
                    if all_ok else "❌ Reconciliation FAILED."))
    return all_ok


# --------------------------------------------------------------------------
# Summary — pdfplumber vs PyMuPDF4LLM
# --------------------------------------------------------------------------

def print_comparison():
    print("\n" + "=" * 78)
    print("pdfplumber  vs  PyMuPDF4LLM  —  significant differences")
    print("=" * 78)
    print("""
  axis                         pdfplumber               PyMuPDF4LLM
  ---------------------------  -----------------------  -----------------------
  primary output               positioned text/lines    Markdown document
  bold/headings preserved      no (lost in text)        yes (**...** in MD)
  doubled-glyph artifact       yes — caller must fix    no — folded by engine
  borderless tables            no detection              reconstructed as MD
  ruled tables                 strong detection          strong detection
  images                       extractable but separate  inlined as placeholder
  best for                     custom layout parsing     feeding text to an LLM
  output is deterministic      yes                       yes, but opinionated
  speed                        fast                      fast (C-backed)

  Practical guidance for Phase 1A statement ingestion:
   * If a known issuer has a stable layout we can regex (the Chase case),
     pdfplumber + extract_text() gives the most predictable surface area.
   * If we want to dump the statement at an LLM and let it pick out fields
     (the unknown-issuer fallback in statement_ingester.py), PyMuPDF4LLM's
     Markdown is a strictly better input than raw text: it carries headings,
     emphasis, and table shape that the LLM can use as anchors.
""")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if not PDF_PATH.exists():
        sys.exit(f"PDF not found: {PDF_PATH}")

    print(f"Opening: {PDF_PATH.name}\n")
    md = pymupdf4llm.to_markdown(str(PDF_PATH), show_progress=False)

    part1_show_markdown(md)
    part2_show_tables()
    bank, mask, acct_type, txns = part3_extract_fields(md)
    reconciled = part4_reconcile(md, txns)
    print_comparison()

    print("=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  bank={bank!r}  last4={mask!r}  type={acct_type!r}  "
          f"transactions={len(txns)}  reconciled={reconciled}")

    return 0 if reconciled else 1


if __name__ == "__main__":
    sys.exit(main())
