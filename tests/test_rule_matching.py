"""Tests for src/overrides.py — the rule matcher, materializer, and
unmaterializer, plus the ingest hook.

Uses the conftest tmp DB. Seeds raw rows directly via SQL so the tests don't
depend on Plaid / statement ingestion paths.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import pytest

from src.overrides import (
    apply_rules_to_new,
    match_rule,
    materialize_rule,
    preview_rule_matches,
    unmaterialize_rule,
)
from src.storage import (
    AUDIT_RULE_MATERIALIZE,
    AUDIT_RULE_UNMATERIALIZE,
    AuditStore,
    OverrideStore,
    RuleStore,
    SOURCE_KIND_RULE,
    SOURCE_KIND_USER_MANUAL,
    TransactionStore,
)


def _seed_raw(
    user_id: str = "u1",
    tx_id: str = "tx-1",
    *,
    description: str = "PACIFIC TABLE #4421 NY",
    category: str = "Dining",
    flow_type: str = "spending",
    amount: float = 42.0,
    date: str = "2026-04-15",
    ingested_at: str | None = None,
) -> None:
    TransactionStore.init_db()
    now = ingested_at or datetime.now().isoformat()
    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO transactions "
            "(id, date, amount, description, category, account_type, "
            "source_file, user_id, account_id, source, dedup_hash, flow_type, "
            "notes, section_type, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, 'credit', '', ?, 'acc-1', 'test', '', ?, '', "
            "'purchase', ?)",
            (tx_id, date, amount, description, category, user_id, flow_type, now),
        )
        conn.commit()


# --- Pure matcher ------------------------------------------------------------


class TestMatcher:
    def test_description_exact(self):
        row = {"description": "PACIFIC TABLE NY"}
        assert match_rule(row, {
            "match_type": "description_exact",
            "match_value": "pacific table ny",
        })
        # Case-insensitive but otherwise literal — store # makes it miss.
        assert not match_rule(row, {
            "match_type": "description_exact",
            "match_value": "PACIFIC TABLE",
        })

    def test_description_substring_finds_partial(self):
        row = {"description": "TST*PACIFIC TABLE #4421 NY"}
        assert match_rule(row, {
            "match_type": "description_substring",
            "match_value": "pacific table",
        })
        assert not match_rule(row, {
            "match_type": "description_substring",
            "match_value": "not in there",
        })

    def test_merchant_canonical_collapses_noise(self):
        # Realistic POS / store-ID noise the canonicalizer strips today —
        # processor prefix, "#1234 NY", trailing state code. City-name noise
        # ("Brooklyn", "New York") is NOT stripped today; that needs a richer
        # canonicalizer (open item in design/overrides.md).
        variants = [
            "TST*PACIFIC TABLE #4421 NY",
            "PACIFIC TABLE #4421",
            "Pacific Table NY",
            "PACIFIC TABLE",
            "Pacific Table",
        ]
        rule = {
            "match_type": "merchant_canonical",
            "match_value": "Pacific Table",
        }
        for d in variants:
            assert match_rule({"description": d}, rule), f"missed: {d!r}"

    def test_merchant_canonical_uses_canonical_match_value_too(self):
        # User pastes the noisy raw description as the rule value — should
        # still match the canonical-equivalent rows.
        rule = {
            "match_type": "merchant_canonical",
            "match_value": "TST*PACIFIC TABLE #4421 NY",
        }
        assert match_rule({"description": "Pacific Table"}, rule)

    def test_unknown_match_type_returns_false(self):
        assert not match_rule(
            {"description": "anything"},
            {"match_type": "regex", "match_value": ".*"},
        )

    def test_empty_value_never_matches(self):
        assert not match_rule(
            {"description": "anything"},
            {"match_type": "description_substring", "match_value": ""},
        )


# --- Preview -----------------------------------------------------------------


class TestPreview:
    def test_preview_returns_total_and_sample(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        _seed_raw(tx_id="tx-2", description="TST*PACIFIC TABLE #4421")
        _seed_raw(tx_id="tx-3", description="Trader Joe's")
        out = preview_rule_matches(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
        )
        assert out["total_matched"] == 2
        ids = {s["id"] for s in out["sample"]}
        assert ids == {"tx-1", "tx-2"}

    def test_preview_does_not_write(self):
        _seed_raw(tx_id="tx-1")
        preview_rule_matches(
            "u1", match_type="merchant_canonical", match_value="Pacific Table"
        )
        assert OverrideStore.get("u1", "tx-1") is None


# --- Materialization ---------------------------------------------------------


class TestMaterialize:
    def _make_rule(self, **overrides) -> int:
        kwargs = dict(
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Kids Education",
        )
        kwargs.update(overrides)
        return RuleStore.insert("u1", **kwargs)

    def test_creates_rule_override_on_match(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY", category="Dining")
        rid = self._make_rule()
        stats = materialize_rule("u1", rid)
        assert stats["materialized"] == 1
        ov = OverrideStore.get("u1", "tx-1")
        assert ov["category"] == "Kids Education"
        assert ov["source_kind"] == SOURCE_KIND_RULE
        assert ov["source_rule_id"] == rid

    def test_skips_user_manual_rows(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        OverrideStore.set_override("u1", "tx-1", category="Dining")  # manual
        rid = self._make_rule()
        stats = materialize_rule("u1", rid)
        assert stats["materialized"] == 0
        assert stats["skipped_manual"] == 1
        ov = OverrideStore.get("u1", "tx-1")
        assert ov["category"] == "Dining"
        assert ov["source_kind"] == SOURCE_KIND_USER_MANUAL

    def test_idempotent_rematerialize(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        rid = self._make_rule()
        materialize_rule("u1", rid)
        # Re-run — no new audit rows for unchanged content.
        before_events = AuditStore.list_events("u1", transaction_id="tx-1")
        materialize_rule("u1", rid)
        after_events = AuditStore.list_events("u1", transaction_id="tx-1")
        assert len(after_events) == len(before_events)

    def test_lower_priority_rule_skipped_when_higher_owns_row(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        high = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Tutoring",
            priority=200,
        )
        low = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Kids Education",
            priority=50,
        )
        materialize_rule("u1", high)
        stats = materialize_rule("u1", low)
        assert stats["materialized"] == 0
        assert stats["skipped_priority"] == 1
        ov = OverrideStore.get("u1", "tx-1")
        assert ov["category"] == "Tutoring"
        assert ov["source_rule_id"] == high

    def test_higher_priority_rule_takes_over(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        low = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Kids Education",
            priority=50,
        )
        materialize_rule("u1", low)
        high = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Tutoring",
            priority=200,
        )
        stats = materialize_rule("u1", high)
        assert stats["materialized"] == 1
        ov = OverrideStore.get("u1", "tx-1")
        assert ov["category"] == "Tutoring"
        assert ov["source_rule_id"] == high

    def test_audit_row_per_materialized_tx(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        _seed_raw(tx_id="tx-2", description="TST*PACIFIC TABLE")
        rid = self._make_rule()
        materialize_rule("u1", rid, chat_session_id="s_test")
        events = AuditStore.list_events("u1", rule_id=rid)
        # Two materializations.
        assert len([e for e in events if e["action"] == AUDIT_RULE_MATERIALIZE]) == 2
        for e in events:
            if e["action"] == AUDIT_RULE_MATERIALIZE:
                assert e["chat_session_id"] == "s_test"


# --- Unmaterialize -----------------------------------------------------------


class TestUnmaterialize:
    def test_removes_rule_overrides_only(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        _seed_raw(tx_id="tx-2", description="PACIFIC TABLE BK")
        rid = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Kids Education",
        )
        materialize_rule("u1", rid)
        # Manual override on tx-2 (created AFTER rule, replaces it).
        OverrideStore.set_override("u1", "tx-2", category="Tutoring")
        removed = unmaterialize_rule("u1", rid)
        assert removed == 1  # only tx-1 had a rule override
        assert OverrideStore.get("u1", "tx-1") is None
        # Manual override on tx-2 untouched.
        assert OverrideStore.get("u1", "tx-2")["category"] == "Tutoring"

    def test_audit_row_per_removed(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        rid = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Kids Education",
        )
        materialize_rule("u1", rid)
        unmaterialize_rule("u1", rid)
        events = AuditStore.list_events("u1", rule_id=rid)
        assert any(e["action"] == AUDIT_RULE_UNMATERIALIZE for e in events)


# --- Ingest hook -------------------------------------------------------------


class TestIngestHook:
    def test_only_new_rows_are_scanned(self):
        # Old row, then create a rule, then ingest a new row at a later time.
        early = "2026-04-01T00:00:00"
        late = "2026-05-30T00:00:00"
        _seed_raw(tx_id="tx-old", description="PACIFIC TABLE NY",
                  ingested_at=early)
        rid = RuleStore.insert(
            "u1",
            match_type="merchant_canonical",
            match_value="Pacific Table",
            target_category="Kids Education",
        )
        # tx-old wasn't materialized (rule didn't exist when it was ingested).
        assert OverrideStore.get("u1", "tx-old") is None
        # Now a new ingest brings tx-new at `late`.
        _seed_raw(tx_id="tx-new", description="PACIFIC TABLE BK",
                  ingested_at=late)
        stats = apply_rules_to_new("u1", since_ingested_at=late)
        assert stats["materialized"] == 1
        assert OverrideStore.get("u1", "tx-new")["category"] == "Kids Education"
        # tx-old still untouched — operator can call materialize_rule(rid)
        # for a backfill.
        assert OverrideStore.get("u1", "tx-old") is None

    def test_no_active_rules_short_circuits(self):
        _seed_raw(tx_id="tx-1", description="PACIFIC TABLE NY")
        stats = apply_rules_to_new("u1", since_ingested_at="1970-01-01")
        assert stats["materialized"] == 0
