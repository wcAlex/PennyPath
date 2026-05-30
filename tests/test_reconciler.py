"""Tests for src/reconciler.py — the reconciliation layer.

`reconcile()` is a pure function, so most cases need no DB. A couple of
real-DB cases (via the `real_db` fixture) confirm the materialized table fixes
the documented self-Zelle income mislabel.
"""

from datetime import date
from pathlib import Path

import pytest

from src import reconciler


def _raw(id, date, amount, description, account_type, section_type, flow_type,
         user_id="u1", category="", account_id="acct", source="statement_pdf",
         dedup_hash=""):
    return {
        "id": id, "user_id": user_id, "date": date, "amount": amount,
        "description": description, "category": category,
        "account_type": account_type, "account_id": account_id,
        "section_type": section_type, "flow_type": flow_type,
        "source": source, "dedup_hash": dedup_hash,
    }


class TestSignedAmount:
    def test_credit_purchase_is_positive_balance_flow(self):
        # Credit purchase raises balance owed → positive account flow.
        assert reconciler._signed_amount("credit", "purchase", 50.0) == 50.0

    def test_credit_payment_is_negative(self):
        assert reconciler._signed_amount("credit", "payment", 200.0) == -200.0

    def test_checking_deposit_positive_withdrawal_negative(self):
        assert reconciler._signed_amount("checking", "deposit", 100.0) == 100.0
        assert reconciler._signed_amount("checking", "withdrawal", 100.0) == -100.0


class TestCreditCardPairing:
    def test_payment_and_withdrawal_pair_and_flag(self):
        rows = [
            _raw("c1", "2026-04-28", 551.73, "Payment Thank You-Mobile", "credit", "payment", "transfer"),
            _raw("b1", "2026-04-28", 551.73, "Payment To Chase Card Ending IN 0418", "checking", "withdrawal", "transfer"),
            _raw("p1", "2026-04-15", 40.00, "Chipotle", "credit", "purchase", "spending"),
        ]
        recon = {r["id"]: r for r in reconciler.reconcile(rows)}
        assert recon["c1"]["is_internal_transfer"] == 1
        assert recon["b1"]["is_internal_transfer"] == 1
        assert recon["c1"]["transfer_group_id"] == recon["b1"]["transfer_group_id"]
        # The unrelated purchase stays put.
        assert recon["p1"]["is_internal_transfer"] == 0
        assert recon["p1"]["flow_type_recon"] == "spending"


class TestSelfTransferCorrection:
    def test_zelle_from_self_deposit_reclassified_to_transfer(self):
        # The documented bug: outgoing leg already 'transfer', incoming leg leaked
        # into 'income'. Pairing + keyword guard fixes the incoming leg.
        rows = [
            _raw("out", "2026-04-28", 3500.0, "Zelle payment to Chi Wang Conf# te6", "checking", "withdrawal", "transfer"),
            _raw("in", "2026-04-28", 3500.0, "Zelle Payment From Chi Wang Bacte6", "checking", "deposit", "income"),
        ]
        recon = {r["id"]: r for r in reconciler.reconcile(rows)}
        assert recon["in"]["flow_type_recon"] == "transfer"   # corrected from income
        assert recon["in"]["is_internal_transfer"] == 1
        assert recon["out"]["is_internal_transfer"] == 1
        assert recon["in"]["transfer_group_id"] == recon["out"]["transfer_group_id"]

    def test_payroll_deposit_is_not_mistaken_for_transfer(self):
        # A real paycheck of the same amount must NOT pair with an unrelated
        # outgoing transfer — no transfer keyword on the payroll memo.
        rows = [
            _raw("pay", "2026-04-15", 3500.0, "100-SFDC INC. DES:PAYROLL INDN:CHI WANG", "checking", "deposit", "income"),
            _raw("zelle_out", "2026-04-17", 3500.0, "Zelle payment to Pei Wu", "checking", "withdrawal", "transfer"),
        ]
        recon = {r["id"]: r for r in reconciler.reconcile(rows)}
        assert recon["pay"]["flow_type_recon"] == "income"      # untouched
        assert recon["pay"]["is_internal_transfer"] == 0


class TestDedup:
    def test_cross_source_duplicate_flagged_statement_wins(self):
        rows = [
            _raw("s1", "2026-04-10", 22.07, "NETFLIX", "credit", "purchase", "spending",
                 source="statement_pdf", dedup_hash="H"),
            _raw("p1", "2026-04-10", 22.07, "NETFLIX", "credit", "purchase", "spending",
                 source="plaid", dedup_hash="H"),
        ]
        recon = {r["id"]: r for r in reconciler.reconcile(rows)}
        assert recon["s1"]["is_duplicate"] == 0   # statement kept
        assert recon["p1"]["is_duplicate"] == 1   # plaid flagged

    def test_same_source_repeats_are_not_dedup(self):
        # Two identical $5 coffees, same source → both real, neither flagged.
        rows = [
            _raw("a", "2026-04-10", 5.0, "COFFEE", "credit", "purchase", "spending",
                 source="statement_pdf", dedup_hash="H"),
            _raw("b", "2026-04-10", 5.0, "COFFEE", "credit", "purchase", "spending",
                 source="statement_pdf", dedup_hash="H"),
        ]
        recon = {r["id"]: r for r in reconciler.reconcile(rows)}
        assert recon["a"]["is_duplicate"] == 0
        assert recon["b"]["is_duplicate"] == 0


REAL_DB = Path(__file__).resolve().parent.parent / "data" / "transactions.db"


@pytest.fixture
def real_db(monkeypatch):
    from src.storage import TransactionStore
    if not REAL_DB.exists():
        pytest.skip("real data/transactions.db not present")
    monkeypatch.setattr(TransactionStore, "DB_PATH", REAL_DB)
    # dashboard_queries caches the initialized path; reset so it re-inits.
    import src.dashboard_queries as dq
    dq._initialized_path = None
    yield


class TestRealDataSelfTransfer:
    def test_april_income_excludes_self_zelle(self, real_db):
        from datetime import date
        from src import dashboard_queries as dq
        from src.reconciler import rebuild_recon
        rebuild_recon("chi")
        res = dq.income_breakdown("chi", date(2026, 4, 1), date(2026, 4, 30))
        names = [s["name"] for s in res["subcategories"]]
        # The $7,000 self-Zelle must not show up as income.
        assert "Transfer" not in names
