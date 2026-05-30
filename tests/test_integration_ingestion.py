"""End-to-end integration tests for PDF statement ingestion.

Opt-in: skipped unless RUN_INTEGRATION=1 is set in the environment.
These tests hit the real LLM, take ~30 s per file, and cost API credits.

Run with output visible (so the printed statement / activity summaries are
useful for manual verification):

    RUN_INTEGRATION=1 pytest tests/test_integration_ingestion.py -s -v

Each test ingests one fixture file in isolation (into a tmp DB) and asserts:
  - no parse_error
  - resolved mask / bank / account_type match expectations
  - at least one transaction was extracted
  - reconciliation didn't fail
  - flow_type distribution and bucket totals are printed for manual review
"""
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATEMENTS_DIR = REPO_ROOT / "data" / "statements"

INTEGRATION_ENABLED = os.environ.get("RUN_INTEGRATION") == "1"

# (filename, bank, account_type, expected last-4 mask)
FIXTURES = [
    ("20250124-statements-7370-.pdf", "Chase",           "checking", "7370"),
    ("20260404-statements-0418-.pdf", "Chase",           "credit",   "0418"),
    ("eStmt_2026-03-23.pdf",          "Bank of America", "credit",   "8373"),
    ("eStmt_2026-03-12.pdf",          "Bank of America", "checking", "0790"),
]


def _print_summary(filename, transactions, header_text):
    """Dump enough information that the user can eyeball the result."""
    print()
    print("=" * 78)
    print(f"FILE: {filename}")
    print("=" * 78)
    print("--- HEADER (first 1200 chars rendered) ---")
    print(header_text[:1200])
    print(f"\n--- {len(transactions)} TRANSACTIONS PARSED ---")

    flow_buckets: dict = {}
    for t in transactions:
        flow_buckets.setdefault(t.flow_type, []).append(t)
    for ft in sorted(flow_buckets):
        rows = flow_buckets[ft]
        total = sum(t.amount for t in rows)
        print(f"  flow_type={ft:<9s} n={len(rows):>4d}  sum=${total:>12,.2f}")

    print(f"\n--- SECTION_TYPE DISTRIBUTION (magnitudes; always positive) ---")
    section_buckets: dict = {}
    for t in transactions:
        section_buckets.setdefault(t.section_type, []).append(t)
    for st in sorted(section_buckets):
        rows = section_buckets[st]
        total = sum(t.amount for t in rows)
        print(f"  section_type={st:<18s} n={len(rows):>4d}  sum=${total:>12,.2f}")

    print(f"\n--- ROWS WITH NOTES POPULATED ---")
    with_notes = [t for t in transactions if t.notes]
    print(f"  {len(with_notes)} of {len(transactions)} rows")
    for t in with_notes[:8]:
        print(f"  {t.date}  ${t.amount:>10,.2f}  {t.description[:35]:<35s}  notes={t.notes!r}")

    print(f"\n--- CATEGORY DISTRIBUTION (spending rows) ---")
    cat_counts: dict = {}
    for t in transactions:
        if t.flow_type == "spending":
            cat_counts[t.category] = cat_counts.get(t.category, 0) + 1
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<28s} {n:>4d}")

    print(f"\n--- FIRST 12 TRANSACTIONS ---")
    for t in transactions[:12]:
        print(
            f"  {t.date}  ${t.amount:>10,.2f}  section={t.section_type:<16s}  "
            f"flow={t.flow_type:<9s}  cat={t.category[:20]:<20s}  {t.description[:40]}"
        )
    print("=" * 78)


@pytest.mark.skipif(not INTEGRATION_ENABLED,
                    reason="Integration tests opt-in: set RUN_INTEGRATION=1")
@pytest.mark.parametrize("filename,expected_bank,expected_type,expected_mask", FIXTURES)
def test_ingest_fixture_statement(filename, expected_bank, expected_type,
                                  expected_mask, tmp_path, monkeypatch):
    """Ingest one fixture statement in isolation and assert basic correctness."""
    src = STATEMENTS_DIR / filename
    if not src.exists():
        pytest.skip(f"fixture missing: {src}")

    # Stage the fixture in a tmp statements dir so the ingester sees only it.
    stmts = tmp_path / "statements"
    stmts.mkdir()
    shutil.copy(src, stmts / filename)

    # Redirect all storage writes to tmp_path.
    import src.storage as storage_module
    monkeypatch.setattr(storage_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage_module.UserConfigStore, "PATH", tmp_path / "config.json")
    monkeypatch.setattr(storage_module.TransactionStore, "DB_PATH", tmp_path / "transactions.db")

    # Seed a config so _user_id() resolves deterministically.
    from src.storage import UserConfigStore, UserConfig
    UserConfigStore.save(UserConfig(name="Integration Test", onboarding_complete=True))

    from src.statement_ingester import ingest_statements
    transactions = ingest_statements(str(stmts))

    # Render header for the printed summary (caller wants to eyeball).
    import pymupdf4llm
    try:
        header_text = pymupdf4llm.to_markdown(str(src), pages=[0, 1])
    except Exception:
        header_text = pymupdf4llm.to_markdown(str(src))[:8000]

    _print_summary(filename, transactions, header_text)

    # --- assertions ---
    from src.storage import TransactionStore
    from src.statement_ingester import _user_id
    fs = TransactionStore.get_file_source(_user_id(), str(stmts / filename))
    assert fs is not None, "file_sources row should exist after ingest"
    assert fs["parse_error"] is None, f"parse_error: {fs['parse_error']}"

    accounts = TransactionStore.query_accounts()
    assert len(accounts) == 1, (
        f"expected exactly 1 account for a single-account statement; "
        f"got {len(accounts)}: {[a['mask'] for a in accounts]}"
    )
    account = accounts[0]
    assert account["mask"] == expected_mask, (
        f"mask mismatch: got '{account['mask']}', expected '{expected_mask}'"
    )
    assert account["type"] == expected_type, (
        f"type mismatch: got '{account['type']}', expected '{expected_type}'"
    )
    # Bank match is fuzzy — different statements word it slightly differently
    # ("Chase" vs "JPMorgan Chase Bank, N.A."). We just want the institution
    # to be in the expected family.
    bank_norm = (account["bank"] or "").lower()
    expected_norm = expected_bank.lower().split()[0]  # "Chase" / "Bank"
    assert expected_norm in bank_norm, (
        f"bank '{account['bank']}' doesn't contain expected '{expected_bank}'"
    )

    assert len(transactions) > 0, "no transactions extracted"
    # Nothing should be left unclassified post-fix — the prompt always assigns a flow_type.
    unknown_rows = [t for t in transactions if t.flow_type == "unknown"]
    assert not unknown_rows, (
        f"{len(unknown_rows)} rows have flow_type=unknown; first: {unknown_rows[0]}"
    )
    # Option 5 contract: amounts are magnitudes — always positive.
    negative = [t for t in transactions if t.amount < 0]
    assert not negative, (
        f"{len(negative)} rows have negative amount; first: {negative[0]}"
    )
    # Every row must carry a section_type.
    untagged = [t for t in transactions if not t.section_type]
    assert not untagged, (
        f"{len(untagged)} rows have no section_type; first: {untagged[0]}"
    )

    # Print recon outcome (warning or clean) for review.
    if fs.get("recon_warning"):
        print(f"\nRECON WARNING (file still saved): {fs['recon_warning']}")
    else:
        print("\nReconciliation: clean")
