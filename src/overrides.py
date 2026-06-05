"""Rule matching + materialization for the user overlay (Phase 1C).

Storage owns the rows (RuleStore, OverrideStore, AuditStore in `src/storage.py`).
This module owns the *interpretation*:

  - `match_rule(row, rule)` — pure predicate; the closed enum of match_types
    is resolved here.
  - `preview_rule_matches(...)` — dry run; what would this rule change?
  - `materialize_rule(user_id, rule_id, ...)` — walks matches, upserts
    overrides with `source_kind='rule'`, skipping rows that already carry a
    `user_manual` override or a higher-priority rule. Each affected tx
    writes one `rule_materialize` audit row.
  - `unmaterialize_rule(user_id, rule_id, ...)` — removes a rule's
    materialized overrides; writes one `rule_unmaterialize` audit row per tx.
  - `apply_rules_to_new(user_id, since_ingested_at)` — ingest hook; runs all
    active rules against rows newer than the cutoff.

The matcher is intentionally cheap (a Python loop over the user's raw rows).
At Phase 1C scale (thousands of rows per user, single-digit rules) this is
sub-millisecond. If we ever scale we can pre-compute a `description_canonical`
column on `transactions`; not now.

See design/overrides.md → Materialization triggers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from src.dashboard_queries import _canonical_merchant
from src.storage import (
    AUDIT_RULE_MATERIALIZE,
    AUDIT_RULE_UNMATERIALIZE,
    AuditStore,
    SOURCE_KIND_RULE,
    SOURCE_KIND_USER_MANUAL,
    TransactionStore,
    VALID_MATCH_TYPES,
    _override_row_to_dict,
)


# --- Pure matcher ------------------------------------------------------------


def _normalize_match_value(match_type: str, match_value: str) -> str:
    """Canonicalize the rule's match string at evaluation time.

    For `merchant_canonical`, run the same canonicalization the matcher applies
    to each row so a rule the user typed as "TST*PACIFIC TABLE #4421 NY"
    matches a row whose canonical form is "Pacific Table". For substring /
    exact, just lower + strip — we compare against the row's raw description.
    """
    v = (match_value or "").strip()
    if not v:
        return ""
    if match_type == "merchant_canonical":
        return _canonical_merchant(v).lower()
    return v.lower()


def match_rule(row: dict, rule: dict) -> bool:
    """Return True if this raw row matches the rule.

    `row` needs `description`. `rule` needs `match_type` and `match_value`.
    """
    mt = rule.get("match_type")
    if mt not in VALID_MATCH_TYPES:
        return False
    mv = _normalize_match_value(mt, rule.get("match_value") or "")
    if not mv:
        return False
    desc_raw = (row.get("description") or "").strip()
    if mt == "description_exact":
        return desc_raw.lower() == mv
    if mt == "description_substring":
        return mv in desc_raw.lower()
    if mt == "merchant_canonical":
        return _canonical_merchant(desc_raw).lower() == mv
    return False


# --- Raw row reader ----------------------------------------------------------


def _fetch_raw_rows_for_user(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    since_ingested_at: Optional[str] = None,
) -> list[dict]:
    """Read raw transactions for this user. Used by the materializer."""
    sql = (
        "SELECT id, date, amount, description, category, account_type, "
        "account_id, user_id, section_type, flow_type, source, ingested_at "
        "FROM transactions WHERE user_id = ?"
    )
    params: list = [user_id]
    if since_ingested_at:
        sql += " AND ingested_at >= ?"
        params.append(since_ingested_at)
    sql += " ORDER BY date ASC, id ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --- Preview -----------------------------------------------------------------


def preview_rule_matches(
    user_id: str,
    *,
    match_type: str,
    match_value: str,
    sample_limit: int = 5,
) -> dict:
    """Dry run — return the rows a rule with this match would affect, without
    writing anything. Used by the chat agent for the "found 12 matches"
    confirmation before `create_category_rule`.

    Returns `{total_matched, rows: [...], sample: [...]}`. `rows` is capped
    at 200 to keep the LLM payload bounded; `total_matched` is the full count.
    """
    pseudo_rule = {"match_type": match_type, "match_value": match_value}
    TransactionStore.init_db()
    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = _fetch_raw_rows_for_user(conn, user_id)
    matched = [r for r in rows if match_rule(r, pseudo_rule)]
    sample = matched[:sample_limit]
    return {
        "total_matched": len(matched),
        "sample": [
            {
                "id":          r["id"],
                "date":        r["date"],
                "description": r["description"],
                "amount":      r["amount"],
                "category":    r["category"],
                "flow_type":   r["flow_type"],
            }
            for r in sample
        ],
    }


# --- Materializer ------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().isoformat()


def _existing_override(conn: sqlite3.Connection, user_id: str, tx_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT user_id, transaction_id, category, flow_type, is_excluded, "
        "source_kind, source_rule_id, note, created_at, updated_at "
        "FROM transaction_overrides WHERE user_id = ? AND transaction_id = ?",
        (user_id, tx_id),
    ).fetchone()
    return _override_row_to_dict(row) if row else None


def _rule_priority(conn: sqlite3.Connection, user_id: str, rule_id: int) -> int:
    row = conn.execute(
        "SELECT priority FROM category_rules WHERE user_id = ? AND id = ?",
        (user_id, rule_id),
    ).fetchone()
    return int(row["priority"]) if row else -1


def _override_payload(
    rule: dict,
    user_id: str,
    tx_id: str,
    now: str,
    *,
    created_at: Optional[str] = None,
) -> tuple:
    return (
        user_id, tx_id,
        rule.get("target_category"),
        rule.get("target_flow_type"),
        int(rule["target_is_excluded"]) if rule.get("target_is_excluded") is not None else None,
        SOURCE_KIND_RULE, int(rule["id"]),
        rule.get("note") or "",
        created_at or now, now,
    )


def materialize_rule(
    user_id: str,
    rule_id: int,
    *,
    chat_session_id: Optional[str] = None,
    chat_message_id: Optional[str] = None,
) -> dict:
    """Walk the user's raw rows, apply this rule to matches, upsert overrides
    with `source_kind='rule'`. Skips:

      - rows that already carry a `user_manual` override (manual is sacred).
      - rows that carry a different rule's override at equal-or-higher priority.

    Idempotent — re-running with no raw changes writes no audit rows. Returns
    `{materialized: N, skipped_manual: M, skipped_priority: K}`.
    """
    from src.storage import RuleStore  # late import — RuleStore is a sibling module member

    rule = RuleStore.get(user_id, rule_id)
    if rule is None:
        return {"materialized": 0, "skipped_manual": 0, "skipped_priority": 0}
    if not rule.get("active"):
        return {"materialized": 0, "skipped_manual": 0, "skipped_priority": 0}

    TransactionStore.init_db()
    now = _now_iso()
    materialized = skipped_manual = skipped_priority = 0

    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        raw_rows = _fetch_raw_rows_for_user(conn, user_id)

        for row in raw_rows:
            if not match_rule(row, rule):
                continue
            tx_id = row["id"]
            existing = _existing_override(conn, user_id, tx_id)

            if existing is not None:
                if existing["source_kind"] == SOURCE_KIND_USER_MANUAL:
                    skipped_manual += 1
                    continue
                # source_kind = 'rule'
                if existing["source_rule_id"] == rule_id:
                    # Idempotent — if the row already reflects this rule's
                    # current targets, do nothing. Otherwise, write an update.
                    if (existing["category"]    == rule.get("target_category")
                            and existing["flow_type"]   == rule.get("target_flow_type")
                            and existing["is_excluded"] == rule.get("target_is_excluded")
                            and existing["note"]        == (rule.get("note") or "")):
                        continue
                    payload = _override_payload(
                        rule, user_id, tx_id, now,
                        created_at=existing["created_at"],
                    )
                    conn.execute(
                        "INSERT INTO transaction_overrides "
                        "(user_id, transaction_id, category, flow_type, "
                        "is_excluded, source_kind, source_rule_id, note, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(user_id, transaction_id) DO UPDATE SET "
                        "  category       = excluded.category, "
                        "  flow_type      = excluded.flow_type, "
                        "  is_excluded    = excluded.is_excluded, "
                        "  source_kind    = excluded.source_kind, "
                        "  source_rule_id = excluded.source_rule_id, "
                        "  note           = excluded.note, "
                        "  updated_at     = excluded.updated_at",
                        payload,
                    )
                    after = _existing_override(conn, user_id, tx_id)
                    AuditStore.append_conn(
                        conn, user_id, AUDIT_RULE_MATERIALIZE,
                        transaction_id=tx_id, rule_id=rule_id,
                        before=existing, after=after,
                        chat_session_id=chat_session_id,
                        chat_message_id=chat_message_id,
                    )
                    materialized += 1
                    continue
                # Different rule already owns this row — compare priority.
                other_priority = _rule_priority(
                    conn, user_id, existing["source_rule_id"] or -1
                )
                if other_priority >= int(rule["priority"]):
                    skipped_priority += 1
                    continue
                # Take over — this rule outranks the existing one.
                payload = _override_payload(rule, user_id, tx_id, now)
                conn.execute(
                    "INSERT INTO transaction_overrides "
                    "(user_id, transaction_id, category, flow_type, "
                    "is_excluded, source_kind, source_rule_id, note, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, transaction_id) DO UPDATE SET "
                    "  category       = excluded.category, "
                    "  flow_type      = excluded.flow_type, "
                    "  is_excluded    = excluded.is_excluded, "
                    "  source_kind    = excluded.source_kind, "
                    "  source_rule_id = excluded.source_rule_id, "
                    "  note           = excluded.note, "
                    "  updated_at     = excluded.updated_at",
                    payload,
                )
                after = _existing_override(conn, user_id, tx_id)
                AuditStore.append_conn(
                    conn, user_id, AUDIT_RULE_MATERIALIZE,
                    transaction_id=tx_id, rule_id=rule_id,
                    before=existing, after=after,
                    chat_session_id=chat_session_id,
                    chat_message_id=chat_message_id,
                )
                materialized += 1
                continue

            # No existing override row — INSERT.
            payload = _override_payload(rule, user_id, tx_id, now)
            conn.execute(
                "INSERT INTO transaction_overrides "
                "(user_id, transaction_id, category, flow_type, "
                "is_excluded, source_kind, source_rule_id, note, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            after = _existing_override(conn, user_id, tx_id)
            AuditStore.append_conn(
                conn, user_id, AUDIT_RULE_MATERIALIZE,
                transaction_id=tx_id, rule_id=rule_id,
                before=None, after=after,
                chat_session_id=chat_session_id,
                chat_message_id=chat_message_id,
            )
            materialized += 1

        conn.commit()

    return {
        "materialized": materialized,
        "skipped_manual": skipped_manual,
        "skipped_priority": skipped_priority,
    }


def unmaterialize_rule(
    user_id: str,
    rule_id: int,
    *,
    chat_session_id: Optional[str] = None,
    chat_message_id: Optional[str] = None,
) -> int:
    """Delete every rule-materialized override for this rule. Manual rows are
    untouched. One `rule_unmaterialize` audit row per affected tx.
    Returns the count removed.
    """
    TransactionStore.init_db()
    removed = 0
    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT user_id, transaction_id, category, flow_type, is_excluded, "
            "source_kind, source_rule_id, note, created_at, updated_at "
            "FROM transaction_overrides "
            "WHERE user_id = ? AND source_rule_id = ? AND source_kind = ?",
            (user_id, rule_id, SOURCE_KIND_RULE),
        ).fetchall()
        for row in rows:
            before = _override_row_to_dict(row)
            conn.execute(
                "DELETE FROM transaction_overrides "
                "WHERE user_id = ? AND transaction_id = ?",
                (user_id, row["transaction_id"]),
            )
            AuditStore.append_conn(
                conn, user_id, AUDIT_RULE_UNMATERIALIZE,
                transaction_id=row["transaction_id"], rule_id=rule_id,
                before=before, after=None,
                chat_session_id=chat_session_id,
                chat_message_id=chat_message_id,
            )
            removed += 1
        conn.commit()
    return removed


def apply_rules_to_new(
    user_id: str,
    since_ingested_at: str,
) -> dict:
    """Ingest hook: apply every active rule to rows newer than the cutoff.

    Called from statement_ingester / plaid_client after `rebuild_recon`. The
    `since_ingested_at` cutoff limits scan cost to the just-ingested set.
    For each active rule (highest priority first), walks the new rows and
    materializes matches.
    """
    from src.storage import RuleStore  # late import

    rules = RuleStore.list_rules(user_id, active_only=True)
    if not rules:
        return {"materialized": 0, "skipped_manual": 0, "skipped_priority": 0}

    TransactionStore.init_db()
    materialized = skipped_manual = skipped_priority = 0
    now = _now_iso()

    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        new_rows = _fetch_raw_rows_for_user(
            conn, user_id, since_ingested_at=since_ingested_at,
        )
        if not new_rows:
            return {"materialized": 0, "skipped_manual": 0, "skipped_priority": 0}

        # Walk by priority (highest first) so the winning rule lands first
        # and lower-priority ones get the "skipped_priority" treatment.
        for rule in rules:
            for row in new_rows:
                if not match_rule(row, rule):
                    continue
                tx_id = row["id"]
                existing = _existing_override(conn, user_id, tx_id)
                if existing is not None:
                    if existing["source_kind"] == SOURCE_KIND_USER_MANUAL:
                        skipped_manual += 1
                        continue
                    if existing["source_rule_id"] == rule["id"]:
                        continue  # idempotent
                    other_priority = _rule_priority(
                        conn, user_id, existing["source_rule_id"] or -1
                    )
                    if other_priority >= int(rule["priority"]):
                        skipped_priority += 1
                        continue
                payload = _override_payload(rule, user_id, tx_id, now)
                conn.execute(
                    "INSERT INTO transaction_overrides "
                    "(user_id, transaction_id, category, flow_type, "
                    "is_excluded, source_kind, source_rule_id, note, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, transaction_id) DO UPDATE SET "
                    "  category       = excluded.category, "
                    "  flow_type      = excluded.flow_type, "
                    "  is_excluded    = excluded.is_excluded, "
                    "  source_kind    = excluded.source_kind, "
                    "  source_rule_id = excluded.source_rule_id, "
                    "  note           = excluded.note, "
                    "  updated_at     = excluded.updated_at",
                    payload,
                )
                after = _existing_override(conn, user_id, tx_id)
                AuditStore.append_conn(
                    conn, user_id, AUDIT_RULE_MATERIALIZE,
                    transaction_id=tx_id, rule_id=int(rule["id"]),
                    before=existing, after=after,
                )
                materialized += 1
        conn.commit()

    return {
        "materialized": materialized,
        "skipped_manual": skipped_manual,
        "skipped_priority": skipped_priority,
    }


__all__ = [
    "match_rule",
    "preview_rule_matches",
    "materialize_rule",
    "unmaterialize_rule",
    "apply_rules_to_new",
]
