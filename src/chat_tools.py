"""In-process tool registry for the Phase 1C chat agent.

The agent loop calls `dispatch(user_id, name, args)` to invoke a tool. Each
tool is described by a `ToolSpec` (name, description, JSON Schema for inputs,
handler). The OpenAI function-calling format is derived via `to_openai_tools()`.

The registry is **internal-only** by design (`design/chat_agent.md`). No
external client speaks to it — no MCP server, no HTTP tool routes. The only
caller is `src/chat_agent.py`.

Every handler reads the effective view `v_transactions_effective` (and
`accounts`), never the raw `transactions` table. This guarantees the chat
numbers match the dashboard numbers, including the user's category /
flow_type overrides and rule-materialized corrections.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional

from src import dashboard_queries
from src.dashboard_queries import (
    _connect,
    _fetch_recon_rows,
    _iso,
    _month_first,
    _month_last,
    _month_key,
    _parse_iso,
    _to_f,
)
from src import overrides as overrides_mod
from src.storage import (
    AuditStore,
    OverrideStore,
    RuleStore,
    TransactionStore,
    VALID_MATCH_TYPES,
)


# --- Errors ------------------------------------------------------------------


class ToolError(Exception):
    """Raised inside a handler to return a structured error to the LLM.

    The dispatcher converts this into `{"error": str, **extras}` so the model
    can read the message and self-correct on the next iteration.
    """

    def __init__(self, message: str, **extras: Any):
        super().__init__(message)
        self.extras = extras


# --- Tool spec ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[str, dict], dict]


# --- Validation --------------------------------------------------------------


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_args(spec: ToolSpec, args: dict) -> dict:
    """Light JSON Schema check — required, type, enum, min/max.

    Raises ToolError on violation. Returns args unchanged on success (callers
    do their own coercion).
    """
    schema = spec.input_schema
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for field in required:
        if field not in args or args[field] in (None, ""):
            raise ToolError(f"missing required field '{field}'",
                            required=required)

    for field, value in list(args.items()):
        if field not in properties:
            # Unknown field — ignore quietly. The LLM may have included extras.
            continue
        ps = properties[field]
        expected = ps.get("type")
        enum = ps.get("enum") or ps.get("type") == "string" and ps.get("enum")
        if value is None:
            continue
        if expected == "string":
            if not isinstance(value, str):
                raise ToolError(f"field '{field}' must be a string")
            if ps.get("format") == "date" and not _DATE_RE.match(value):
                raise ToolError(
                    f"field '{field}' must be YYYY-MM-DD, got {value!r}")
        elif expected == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                # Booleans are ints in Python — reject them.
                try:
                    value = int(value)
                except Exception:
                    raise ToolError(f"field '{field}' must be an integer")
                args[field] = value
            mn, mx = ps.get("minimum"), ps.get("maximum")
            if mn is not None and value < mn:
                args[field] = mn
            if mx is not None and value > mx:
                args[field] = mx
        elif expected == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                try:
                    value = float(value)
                except Exception:
                    raise ToolError(f"field '{field}' must be a number")
                args[field] = value
        if "enum" in ps and value not in ps["enum"]:
            raise ToolError(
                f"field '{field}' must be one of {ps['enum']}, got {value!r}")
    return args


def _need_date(args: dict, field: str) -> date:
    s = args.get(field)
    if not s:
        raise ToolError(f"missing required date '{field}'")
    return _parse_iso(s)


def _opt_date(args: dict, field: str) -> Optional[date]:
    s = args.get(field)
    return _parse_iso(s) if s else None


def _check_window(start: date, end: date, *, max_months: int = 24) -> None:
    if end < start:
        raise ToolError("'end' must not precede 'start'")
    days = (end - start).days
    if days > max_months * 31:
        raise ToolError(f"date range too wide; use ≤ {max_months} months")


def _check_category(user_id: str, name: Optional[str]) -> Optional[str]:
    """Confirm a category exists for this user (case-insensitive). Returns the
    canonical-case name on hit; raises ToolError with suggestions on miss."""
    if not name:
        return None
    known = _known_categories(user_id)
    by_lower = {k.lower(): k for k in known}
    hit = by_lower.get(name.lower())
    if hit:
        return hit
    suggestions = _closest_strings(name, known, k=3)
    raise ToolError(
        f"unknown category {name!r}",
        suggestion=suggestions,
        available_top=list(known)[:10],
    )


def _check_account(user_id: str, account_id: Optional[str]) -> Optional[str]:
    if not account_id:
        return None
    accs = _known_accounts(user_id)
    if any(a["id"] == account_id for a in accs):
        return account_id
    raise ToolError(
        f"unknown account_id {account_id!r}",
        available=[{"id": a["id"], "name": a["name"]} for a in accs],
    )


def _closest_strings(target: str, candidates, k: int = 3) -> list[str]:
    # Simple substring + prefix score; avoids a dependency on difflib semantics.
    tl = target.lower()
    scored: list[tuple[int, str]] = []
    for c in candidates:
        cl = c.lower()
        if cl == tl:
            scored.append((0, c))
        elif cl.startswith(tl) or tl.startswith(cl):
            scored.append((1, c))
        elif tl in cl or cl in tl:
            scored.append((2, c))
    scored.sort(key=lambda p: (p[0], p[1]))
    return [c for _, c in scored[:k]]


# --- Vocab helpers -----------------------------------------------------------


def _known_categories(user_id: str) -> list[str]:
    """Distinct categories for this user, excluding transfers/dupes."""
    sql = (
        "SELECT DISTINCT category FROM v_transactions_effective "
        "WHERE user_id = ? AND is_internal_transfer = 0 AND is_duplicate = 0 AND is_user_excluded = 0 "
        "AND category IS NOT NULL AND category != '' "
        "ORDER BY category"
    )
    with _connect() as conn:
        return [r["category"] for r in conn.execute(sql, (user_id,)).fetchall()]


def _known_accounts(user_id: str) -> list[dict]:
    sql = (
        "SELECT id, bank, name, mask, type FROM accounts WHERE user_id = ? "
        "ORDER BY bank, name"
    )
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, (user_id,)).fetchall()]


# --- Handlers ----------------------------------------------------------------


def _handle_list_categories(user_id: str, args: dict) -> dict:
    start = _opt_date(args, "start")
    end = _opt_date(args, "end")
    sql = (
        "SELECT category, COUNT(*) AS n, MAX(date) AS last_seen "
        "FROM v_transactions_effective "
        "WHERE user_id = ? AND is_internal_transfer = 0 AND is_duplicate = 0 AND is_user_excluded = 0 "
        "AND category IS NOT NULL AND category != ''"
    )
    params: list = [user_id]
    if start and end:
        _check_window(start, end)
        sql += " AND date BETWEEN ? AND ?"
        params.extend([_iso(start), _iso(end)])
    sql += " GROUP BY category ORDER BY n DESC"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {
        "categories": [
            {"name": r["category"], "count": r["n"], "last_seen": r["last_seen"] or ""}
            for r in rows
        ],
    }


def _handle_list_accounts(user_id: str, args: dict) -> dict:
    return {"accounts": _known_accounts(user_id)}


def _handle_query_spending_breakdown(user_id: str, args: dict) -> dict:
    start = _need_date(args, "start")
    end = _need_date(args, "end")
    _check_window(start, end)
    category = _check_category(user_id, args.get("category"))
    account_id = _check_account(user_id, args.get("account_id"))
    group_by = args.get("group_by") or "category"

    rows = _fetch_recon_rows(user_id, start, end, account_id)
    buckets: dict[str, dict] = {}
    total = 0.0
    cat_lc = category.lower() if category else None
    for r in rows:
        if r["flow_type_recon"] != "spending":
            continue
        if r["is_internal_transfer"] or r["is_duplicate"] or r["is_user_excluded"]:
            continue
        if cat_lc and (r["category"] or "").lower() != cat_lc:
            continue
        amt = float(r["amount"])
        total += amt
        if group_by == "category":
            label = r["category"] or "Uncategorized"
        elif group_by == "merchant":
            label = dashboard_queries._canonical_merchant(r["description"]) or "Unknown"
        elif group_by == "week":
            d = _parse_iso(r["date"])
            iso_y, iso_w, _ = d.isocalendar()
            label = f"{iso_y}-W{iso_w:02d}"
        elif group_by == "month":
            label = _month_key(r["date"])
        else:
            label = r["category"] or "Uncategorized"

        b = buckets.setdefault(label, {"label": label, "amount": 0.0, "count": 0})
        b["amount"] += amt
        b["count"] += 1

    out_buckets = sorted(buckets.values(), key=lambda b: b["amount"], reverse=True)
    for b in out_buckets:
        b["pct"] = round((b["amount"] / total) * 100, 2) if total > 0 else 0.0
        b["amount"] = round(b["amount"], 2)
    return {
        "period": {"start": _iso(start), "end": _iso(end)},
        "total": round(total, 2),
        "group_by": group_by,
        "buckets": out_buckets,
    }


def _handle_query_income_breakdown(user_id: str, args: dict) -> dict:
    start = _need_date(args, "start")
    end = _need_date(args, "end")
    _check_window(start, end)
    account_id = _check_account(user_id, args.get("account_id"))
    group_by = args.get("group_by") or "subcategory"

    rows = _fetch_recon_rows(user_id, start, end, account_id)
    buckets: dict[str, dict] = {}
    total = 0.0
    for r in rows:
        if r["flow_type_recon"] != "income":
            continue
        if r["is_internal_transfer"] or r["is_duplicate"] or r["is_user_excluded"]:
            continue
        amt = float(r["amount"])
        total += amt
        if group_by == "month":
            label = _month_key(r["date"])
        else:
            label = r["category"] or "Income"
        b = buckets.setdefault(label, {"label": label, "amount": 0.0, "count": 0})
        b["amount"] += amt
        b["count"] += 1
    out_buckets = sorted(buckets.values(), key=lambda b: b["amount"], reverse=True)
    for b in out_buckets:
        b["pct"] = round((b["amount"] / total) * 100, 2) if total > 0 else 0.0
        b["amount"] = round(b["amount"], 2)
    return {
        "period": {"start": _iso(start), "end": _iso(end)},
        "total": round(total, 2),
        "group_by": group_by,
        "buckets": out_buckets,
    }


def _handle_list_transactions(user_id: str, args: dict) -> dict:
    start = _need_date(args, "start")
    end = _need_date(args, "end")
    _check_window(start, end)
    category = _check_category(user_id, args.get("category"))
    account_id = _check_account(user_id, args.get("account_id"))
    q = args.get("q")
    min_amount = args.get("min_amount")
    max_amount = args.get("max_amount")
    limit = int(args.get("limit") or 50)
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    rows = _fetch_recon_rows(user_id, start, end, account_id)
    matched: list[dict] = []
    ql = q.lower() if q else None
    cat_lc = category.lower() if category else None
    for r in rows:
        if cat_lc and (r["category"] or "").lower() != cat_lc:
            continue
        amt = float(r["amount"])
        if min_amount is not None and amt < float(min_amount):
            continue
        if max_amount is not None and amt > float(max_amount):
            continue
        if ql and ql not in (r["description"] or "").lower():
            continue
        matched.append({
            "date": r["date"],
            "description": r["description"],
            "amount": round(amt, 2),
            "category": r["category"] or "",
            "account_id": r["account_id"],
            "flow_type": r["flow_type_recon"],
            "is_internal_transfer": bool(r["is_internal_transfer"]),
        })
    matched.sort(key=lambda x: x["date"], reverse=True)
    total_matched = len(matched)
    truncated = total_matched > limit
    return {
        "rows": matched[:limit],
        "total_matched": total_matched,
        "truncated": truncated,
    }


def _handle_category_trend(user_id: str, args: dict) -> dict:
    raw_cat = args.get("category")
    category = _check_category(user_id, raw_cat)
    months = int(args.get("months") or 12)
    if months < 1:
        months = 1
    if months > 24:
        months = 24
    account_id = _check_account(user_id, args.get("account_id"))
    flow = args.get("flow") or "spending"
    return dashboard_queries.category_trend(
        user_id, category, months=months, account_id=account_id, flow=flow,
    )


def _handle_top_merchants(user_id: str, args: dict) -> dict:
    start = _need_date(args, "start")
    end = _need_date(args, "end")
    _check_window(start, end)
    category = _check_category(user_id, args.get("category"))
    account_id = _check_account(user_id, args.get("account_id"))
    limit = int(args.get("limit") or 10)
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50
    merchants = dashboard_queries.top_merchants(
        user_id, start, end, category=category, account_id=account_id, limit=limit,
    )
    return {"merchants": merchants}


def _handle_compare_periods(user_id: str, args: dict) -> dict:
    a_start = _need_date(args, "period_a_start")
    a_end = _need_date(args, "period_a_end")
    b_start = _need_date(args, "period_b_start")
    b_end = _need_date(args, "period_b_end")
    _check_window(a_start, a_end)
    _check_window(b_start, b_end)
    category = _check_category(user_id, args.get("category"))
    account_id = _check_account(user_id, args.get("account_id"))
    mover_dim = args.get("mover_dim") or "category"
    return dashboard_queries.compare_periods(
        user_id, a_start, a_end, b_start, b_end,
        category=category, account_id=account_id, mover_dim=mover_dim,
    )


def _handle_cashflow_summary(user_id: str, args: dict) -> dict:
    months = int(args.get("months") or 12)
    if months < 1:
        months = 1
    if months > 24:
        months = 24
    account_id = _check_account(user_id, args.get("account_id"))
    series = dashboard_queries.cashflow_series(
        user_id, months=months, account_id=account_id,
    )
    # Trim the heavy fixed/flexible blobs into just category names for the LLM.
    return {
        "months": series["months"],
        "income_per_month": series["income_per_month"],
        "spending_per_month": series["spending_per_month"],
        "avg_income": round(sum(series["income_per_month"]) / max(len(series["months"]), 1), 2),
        "avg_spending": round(series["avg_spending"], 2),
        "avg_net": round(series["avg_net"], 2),
        "fixed_categories": [c["name"] for c in series["fixed_categories"]],
        "flexible_categories": [c["name"] for c in series["flexible_categories"]],
    }


# --- Override / rule handlers (Phase 1C) -------------------------------------
#
# These tools mutate the user overlay. They all accept optional
# `chat_session_id` and `chat_message_id` args so the audit log records the
# provenance of every change. The chat agent passes these in automatically;
# direct testing can omit them.


_VALID_FLOW_TYPES = (
    "spending", "income", "transfer", "interest", "fee", "refund",
)


def _validate_flow_type(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    if value not in _VALID_FLOW_TYPES:
        raise ToolError(
            f"flow_type must be one of {_VALID_FLOW_TYPES}, got {value!r}"
        )
    return value


def _coerce_is_excluded(value) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        v = int(value)
    except Exception:
        raise ToolError("is_excluded must be 0 or 1")
    if v not in (0, 1):
        raise ToolError("is_excluded must be 0 or 1")
    return v


def _require_transaction(user_id: str, tx_id: str) -> None:
    """Confirm the tx exists and belongs to this user, so we never let the
    LLM hallucinate a transaction_id into the overrides table."""
    if not tx_id:
        raise ToolError("missing required field 'transaction_id'")
    TransactionStore.init_db()
    import sqlite3 as _sqlite3
    with _sqlite3.connect(TransactionStore.DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM transactions WHERE id = ? AND user_id = ?",
            (tx_id, user_id),
        ).fetchone()
    if not row:
        raise ToolError(
            f"unknown transaction_id {tx_id!r} for this user",
            hint="use list_transactions to find a real id from the current view",
        )


def _override_row_with_raw(user_id: str, tx_id: str) -> dict:
    """Return a small summary of the override + raw row for the LLM/UI."""
    import sqlite3 as _sqlite3
    TransactionStore.init_db()
    with _sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT id, date, amount, description, "
            "category AS raw_category, flow_type AS raw_flow_type "
            "FROM transactions WHERE id = ? AND user_id = ?",
            (tx_id, user_id),
        ).fetchone()
    raw = dict(row) if row else {}
    ov = OverrideStore.get(user_id, tx_id)
    return {
        "transaction_id": tx_id,
        "date":           raw.get("date"),
        "description":    raw.get("description"),
        "amount":         raw.get("amount"),
        "raw_category":   raw.get("raw_category"),
        "raw_flow_type":  raw.get("raw_flow_type"),
        "override":       ov,
    }


def _handle_preview_rule_matches(user_id: str, args: dict) -> dict:
    mt = args.get("match_type")
    if mt not in VALID_MATCH_TYPES:
        raise ToolError(
            f"match_type must be one of {VALID_MATCH_TYPES}, got {mt!r}"
        )
    mv = args.get("match_value") or ""
    if not mv.strip():
        raise ToolError("match_value must be non-empty")
    out = overrides_mod.preview_rule_matches(
        user_id, match_type=mt, match_value=mv, sample_limit=5,
    )
    # Tell the LLM what the rule *would* change so it can announce the impact
    # without a second tool call.
    out["proposed"] = {
        "target_category":    args.get("target_category"),
        "target_flow_type":   _validate_flow_type(args.get("target_flow_type")),
        "target_is_excluded": _coerce_is_excluded(args.get("target_is_excluded")),
        "match_type":         mt,
        "match_value":        mv,
    }
    return out


def _handle_set_override(user_id: str, args: dict) -> dict:
    tx_id = args.get("transaction_id")
    _require_transaction(user_id, tx_id)
    category = args.get("category")
    flow_type = _validate_flow_type(args.get("flow_type"))
    is_excluded = _coerce_is_excluded(args.get("is_excluded"))
    note = (args.get("note") or "").strip()
    if category is None and flow_type is None and is_excluded is None:
        raise ToolError(
            "set_override needs at least one of category / flow_type / is_excluded"
        )
    before = OverrideStore.get(user_id, tx_id)
    after = OverrideStore.set_override(
        user_id, tx_id,
        category=category, flow_type=flow_type, is_excluded=is_excluded,
        note=note,
        chat_session_id=args.get("_chat_session_id"),
        chat_message_id=args.get("_chat_message_id"),
    )
    return {
        "ok": True,
        "before": before,
        "after": after,
        "row": _override_row_with_raw(user_id, tx_id),
    }


def _handle_set_overrides_bulk(user_id: str, args: dict) -> dict:
    tx_ids = args.get("transaction_ids") or []
    if not isinstance(tx_ids, list) or not tx_ids:
        raise ToolError("transaction_ids must be a non-empty list")
    if len(tx_ids) > 200:
        raise ToolError("too many transaction_ids; cap is 200 per call")
    category = args.get("category")
    flow_type = _validate_flow_type(args.get("flow_type"))
    is_excluded = _coerce_is_excluded(args.get("is_excluded"))
    note = (args.get("note") or "").strip()
    if category is None and flow_type is None and is_excluded is None:
        raise ToolError(
            "set_overrides_bulk needs at least one of category / flow_type / is_excluded"
        )
    session_id = args.get("_chat_session_id")
    message_id = args.get("_chat_message_id")
    count = 0
    failed: list[str] = []
    for tx_id in tx_ids:
        try:
            _require_transaction(user_id, tx_id)
            OverrideStore.set_override(
                user_id, tx_id,
                category=category, flow_type=flow_type, is_excluded=is_excluded,
                note=note,
                chat_session_id=session_id, chat_message_id=message_id,
            )
            count += 1
        except ToolError:
            failed.append(tx_id)
    return {"ok": True, "count": count, "failed_transaction_ids": failed}


def _handle_clear_override(user_id: str, args: dict) -> dict:
    tx_id = args.get("transaction_id")
    _require_transaction(user_id, tx_id)
    removed = OverrideStore.clear_override(
        user_id, tx_id,
        chat_session_id=args.get("_chat_session_id"),
        chat_message_id=args.get("_chat_message_id"),
    )
    return {"ok": removed is not None, "before": removed}


def _handle_create_category_rule(user_id: str, args: dict) -> dict:
    mt = args.get("match_type")
    if mt not in VALID_MATCH_TYPES:
        raise ToolError(
            f"match_type must be one of {VALID_MATCH_TYPES}, got {mt!r}"
        )
    mv = args.get("match_value") or ""
    if not mv.strip():
        raise ToolError("match_value must be non-empty")
    target_category = args.get("target_category")
    target_flow_type = _validate_flow_type(args.get("target_flow_type"))
    target_is_excluded = _coerce_is_excluded(args.get("target_is_excluded"))
    if (target_category is None
            and target_flow_type is None
            and target_is_excluded is None):
        raise ToolError(
            "rule needs at least one of target_category / target_flow_type / target_is_excluded"
        )
    note = (args.get("note") or "").strip()
    priority = int(args.get("priority") or 100)
    apply_to_past = args.get("apply_to_past")
    if apply_to_past is None:
        apply_to_past = True
    session_id = args.get("_chat_session_id")
    message_id = args.get("_chat_message_id")

    rule_id = RuleStore.insert(
        user_id,
        match_type=mt,
        match_value=mv,
        target_category=target_category,
        target_flow_type=target_flow_type,
        target_is_excluded=target_is_excluded,
        priority=priority,
        note=note,
    )
    # Write the create_rule audit row.
    import sqlite3 as _sqlite3
    with _sqlite3.connect(TransactionStore.DB_PATH) as conn:
        AuditStore.append_conn(
            conn, user_id, "create_rule",
            rule_id=rule_id,
            before=None,
            after=RuleStore.get(user_id, rule_id),
            chat_session_id=session_id,
            chat_message_id=message_id,
        )
        conn.commit()

    stats = {"materialized": 0, "skipped_manual": 0, "skipped_priority": 0}
    if apply_to_past:
        stats = overrides_mod.materialize_rule(
            user_id, rule_id,
            chat_session_id=session_id, chat_message_id=message_id,
        )
    return {
        "rule_id": rule_id,
        "rule": RuleStore.get(user_id, rule_id),
        "materialized_count": stats["materialized"],
        "skipped_manual": stats["skipped_manual"],
        "skipped_priority": stats["skipped_priority"],
    }


def _handle_list_category_rules(user_id: str, args: dict) -> dict:
    active_only = args.get("active_only")
    if active_only is None:
        active_only = True
    rules = RuleStore.list_rules(user_id, active_only=bool(active_only))
    # Count of rows each rule currently affects (`source_rule_id` in overrides).
    import sqlite3 as _sqlite3
    TransactionStore.init_db()
    counts: dict[int, int] = {}
    if rules:
        with _sqlite3.connect(TransactionStore.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT source_rule_id, COUNT(*) FROM transaction_overrides "
                "WHERE user_id = ? AND source_kind = 'rule' "
                "GROUP BY source_rule_id",
                (user_id,),
            ).fetchall()
            for rid, n in rows:
                if rid is not None:
                    counts[int(rid)] = int(n)
    out = []
    for r in rules:
        out.append({**r, "affects_count": counts.get(int(r["id"]), 0)})
    return {"rules": out}


def _handle_delete_category_rule(user_id: str, args: dict) -> dict:
    rule_id = args.get("rule_id")
    if rule_id is None:
        raise ToolError("missing required field 'rule_id'")
    rule_id = int(rule_id)
    session_id = args.get("_chat_session_id")
    message_id = args.get("_chat_message_id")
    # First unmaterialize so audit ordering reads "rule_unmaterialize → delete_rule"
    # from the bottom up, which matches what happened.
    unmat = overrides_mod.unmaterialize_rule(
        user_id, rule_id,
        chat_session_id=session_id, chat_message_id=message_id,
    )
    deleted = RuleStore.delete(user_id, rule_id)
    if deleted is None:
        return {"ok": False, "error": f"rule {rule_id} not found"}
    import sqlite3 as _sqlite3
    with _sqlite3.connect(TransactionStore.DB_PATH) as conn:
        AuditStore.append_conn(
            conn, user_id, "delete_rule",
            rule_id=rule_id,
            before=deleted,
            after=None,
            chat_session_id=session_id,
            chat_message_id=message_id,
        )
        conn.commit()
    return {"ok": True, "unmaterialized_count": unmat, "rule": deleted}


def _handle_list_overrides(user_id: str, args: dict) -> dict:
    tx_id = args.get("transaction_id")
    since = args.get("since")
    limit = int(args.get("limit") or 20)
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    rows = OverrideStore.list_overrides(
        user_id, transaction_id=tx_id, since=since, limit=limit,
    )
    # Hydrate each row with the raw description so the LLM can show
    # "PACIFIC TABLE #4421 NY" instead of just an opaque id.
    if rows:
        TransactionStore.init_db()
        import sqlite3 as _sqlite3
        with _sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = _sqlite3.Row
            ids = tuple(r["transaction_id"] for r in rows)
            placeholders = ",".join(["?"] * len(ids))
            raws = {
                r["id"]: dict(r) for r in conn.execute(
                    f"SELECT id, date, description, amount, "
                    f"category AS raw_category, flow_type AS raw_flow_type "
                    f"FROM transactions WHERE user_id = ? AND id IN ({placeholders})",
                    (user_id, *ids),
                ).fetchall()
            }
        for r in rows:
            raw = raws.get(r["transaction_id"], {})
            r["date"]          = raw.get("date")
            r["description"]   = raw.get("description")
            r["amount"]        = raw.get("amount")
            r["raw_category"]  = raw.get("raw_category")
            r["raw_flow_type"] = raw.get("raw_flow_type")
    return {"overrides": rows}


def _handle_list_override_history(user_id: str, args: dict) -> dict:
    tx_id = args.get("transaction_id")
    rule_id = args.get("rule_id")
    since = args.get("since")
    limit = int(args.get("limit") or 20)
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    events = AuditStore.list_events(
        user_id,
        transaction_id=tx_id,
        rule_id=int(rule_id) if rule_id is not None else None,
        since=since,
        limit=limit,
    )
    return {"events": events}


# --- Schemas + REGISTRY ------------------------------------------------------

_SCHEMAS: dict[str, dict] = {
    "list_categories": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "format": "date",
                       "description": "optional; restrict to date range"},
            "end":   {"type": "string", "format": "date"},
        },
        "required": [],
    },
    "list_accounts": {
        "type": "object",
        "properties": {},
        "required": [],
    },
    "query_spending_breakdown": {
        "type": "object",
        "properties": {
            "start":      {"type": "string", "format": "date"},
            "end":        {"type": "string", "format": "date"},
            "category":   {"type": "string"},
            "account_id": {"type": "string"},
            "group_by":   {"type": "string",
                            "enum": ["category", "merchant", "week", "month"]},
        },
        "required": ["start", "end"],
    },
    "query_income_breakdown": {
        "type": "object",
        "properties": {
            "start":      {"type": "string", "format": "date"},
            "end":        {"type": "string", "format": "date"},
            "account_id": {"type": "string"},
            "group_by":   {"type": "string",
                            "enum": ["subcategory", "month"]},
        },
        "required": ["start", "end"],
    },
    "list_transactions": {
        "type": "object",
        "properties": {
            "start":      {"type": "string", "format": "date"},
            "end":        {"type": "string", "format": "date"},
            "category":   {"type": "string"},
            "account_id": {"type": "string"},
            "q":          {"type": "string",
                            "description": "substring match on description (case-insensitive)"},
            "min_amount": {"type": "number"},
            "max_amount": {"type": "number"},
            "limit":      {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["start", "end"],
    },
    "category_trend": {
        "type": "object",
        "properties": {
            "category":   {"type": "string"},
            "months":     {"type": "integer", "minimum": 1, "maximum": 24},
            "account_id": {"type": "string"},
            "flow":       {"type": "string", "enum": ["spending", "income"]},
        },
        "required": ["category"],
    },
    "top_merchants": {
        "type": "object",
        "properties": {
            "start":      {"type": "string", "format": "date"},
            "end":        {"type": "string", "format": "date"},
            "category":   {"type": "string"},
            "account_id": {"type": "string"},
            "limit":      {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["start", "end"],
    },
    "compare_periods": {
        "type": "object",
        "properties": {
            "period_a_start": {"type": "string", "format": "date"},
            "period_a_end":   {"type": "string", "format": "date"},
            "period_b_start": {"type": "string", "format": "date"},
            "period_b_end":   {"type": "string", "format": "date"},
            "category":       {"type": "string"},
            "account_id":     {"type": "string"},
            "mover_dim":      {"type": "string", "enum": ["category", "merchant"]},
        },
        "required": ["period_a_start", "period_a_end",
                     "period_b_start", "period_b_end"],
    },
    "cashflow_summary": {
        "type": "object",
        "properties": {
            "months":     {"type": "integer", "minimum": 1, "maximum": 24},
            "account_id": {"type": "string"},
        },
        "required": [],
    },
    "preview_rule_matches": {
        "type": "object",
        "properties": {
            "match_type":         {"type": "string",
                                    "enum": list(VALID_MATCH_TYPES)},
            "match_value":        {"type": "string"},
            "target_category":    {"type": "string"},
            "target_flow_type":   {"type": "string",
                                    "enum": list(_VALID_FLOW_TYPES)},
            "target_is_excluded": {"type": "integer", "minimum": 0, "maximum": 1},
        },
        "required": ["match_type", "match_value"],
    },
    "set_override": {
        "type": "object",
        "properties": {
            "transaction_id": {"type": "string"},
            "category":       {"type": "string"},
            "flow_type":      {"type": "string", "enum": list(_VALID_FLOW_TYPES)},
            "is_excluded":    {"type": "integer", "minimum": 0, "maximum": 1},
            "note":           {"type": "string"},
        },
        "required": ["transaction_id"],
    },
    "set_overrides_bulk": {
        "type": "object",
        "properties": {
            "transaction_ids": {"type": "array", "items": {"type": "string"}},
            "category":        {"type": "string"},
            "flow_type":       {"type": "string", "enum": list(_VALID_FLOW_TYPES)},
            "is_excluded":     {"type": "integer", "minimum": 0, "maximum": 1},
            "note":            {"type": "string"},
        },
        "required": ["transaction_ids"],
    },
    "clear_override": {
        "type": "object",
        "properties": {
            "transaction_id": {"type": "string"},
        },
        "required": ["transaction_id"],
    },
    "create_category_rule": {
        "type": "object",
        "properties": {
            "match_type":         {"type": "string",
                                    "enum": list(VALID_MATCH_TYPES)},
            "match_value":        {"type": "string"},
            "target_category":    {"type": "string"},
            "target_flow_type":   {"type": "string",
                                    "enum": list(_VALID_FLOW_TYPES)},
            "target_is_excluded": {"type": "integer", "minimum": 0, "maximum": 1},
            "note":               {"type": "string"},
            "priority":           {"type": "integer"},
            "apply_to_past":      {"type": "boolean"},
        },
        "required": ["match_type", "match_value"],
    },
    "list_category_rules": {
        "type": "object",
        "properties": {
            "active_only": {"type": "boolean"},
        },
        "required": [],
    },
    "delete_category_rule": {
        "type": "object",
        "properties": {
            "rule_id": {"type": "integer"},
        },
        "required": ["rule_id"],
    },
    "list_overrides": {
        "type": "object",
        "properties": {
            "transaction_id": {"type": "string"},
            "since":          {"type": "string", "format": "date"},
            "limit":          {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    },
    "list_override_history": {
        "type": "object",
        "properties": {
            "transaction_id": {"type": "string"},
            "rule_id":        {"type": "integer"},
            "since":          {"type": "string", "format": "date"},
            "limit":          {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": [],
    },
}

_DESCRIPTIONS: dict[str, str] = {
    "list_categories":
        "List spending/income categories that appear in the user's data, "
        "with row counts. Useful when the user names a category and you want "
        "to confirm the exact label before calling other tools.",
    "list_accounts":
        "List the user's linked or uploaded accounts (id, friendly name, "
        "bank, type, last-4 mask). Use when the user says 'my Chase "
        "account' and you need to pick the right account_id, or when you "
        "must ask the user which account they mean.",
    "query_spending_breakdown":
        "Total spending in a date range, broken down by the requested "
        "dimension (category, merchant, week, or month). Excludes internal "
        "transfers and duplicates. Use for 'break down my X' questions.",
    "query_income_breakdown":
        "Total income in a date range, broken down by subcategory or month. "
        "Excludes internal transfers.",
    "list_transactions":
        "Return individual transactions matching the filters. Use when the "
        "user wants to see actual line items, not aggregates. Capped at 200 "
        "rows per call.",
    "category_trend":
        "Monthly totals for one category over the last N months. Use for "
        "trend questions: 'is dining going up?', 'show me the past 3 months'.",
    "top_merchants":
        "Top N merchants by total spend in a date range, optionally filtered "
        "to a category or account. Merchant labels are canonicalized so the "
        "same store doesn't show up as several rows.",
    "compare_periods":
        "Compare totals between two date ranges. Returns the delta and the "
        "categories or merchants that moved most. Use for 'why was March "
        "higher?' / 'how does this month compare to last?' questions.",
    "cashflow_summary":
        "Income vs. spending per month over the last N months, with averages "
        "and the fixed/flexible category split.",
    "preview_rule_matches":
        "Dry run for a category rule: how many transactions would it affect, "
        "and what do a few sample rows look like? ALWAYS call this before "
        "create_category_rule so you can announce the scope and confirm with "
        "the user. Pass the proposed targets too — they round-trip into the "
        "preview output so you can show before → after.",
    "set_override":
        "Override one transaction's category, flow_type, and/or is_excluded. "
        "Use when the user points at a SINGLE row. For 'fix all matching rows', "
        "call create_category_rule instead. Manual overrides always win over "
        "rules.",
    "set_overrides_bulk":
        "Apply the same override (category / flow_type / is_excluded) to a "
        "list of transaction_ids. Use when the user said 'fix all past "
        "matches' but NOT 'and future ones too' — for past+future, use "
        "create_category_rule with apply_to_past=true.",
    "clear_override":
        "Remove any user override (manual or rule-materialized) on this "
        "transaction. The row reverts to its raw category / flow_type.",
    "create_category_rule":
        "Create a rule that auto-categorizes any transaction whose "
        "description matches a pattern, now and forever. Use when the user "
        "wants 'all past AND future' to be fixed. Set apply_to_past=true "
        "(default) to also fix the existing matches. Prefer match_type="
        "'merchant_canonical' when the user points at a merchant — it "
        "ignores POS prefixes / store IDs / locations.",
    "list_category_rules":
        "Show the user's active category rules: what each one matches, what "
        "it sets, and how many rows it currently affects. Use when the user "
        "asks 'what rules do I have?'.",
    "delete_category_rule":
        "Remove a rule by id. Unwinds every rule-materialized override the "
        "rule created (user_manual overrides are untouched). Use when the "
        "user says 'undo that rule'.",
    "list_overrides":
        "Show what's currently overridden — per-row before → after — for the "
        "user. Use when the user asks 'what's been changed?' or 'what's "
        "different from raw?'. Optional transaction_id narrows to one row.",
    "list_override_history":
        "Show the append-only audit history: when each override / rule was "
        "set, by which chat session, and what the before/after was. Use for "
        "'why is this row Kids Education?' (with transaction_id) or 'what "
        "changed recently?' (with since).",
}


REGISTRY: dict[str, ToolSpec] = {
    name: ToolSpec(
        name=name,
        description=_DESCRIPTIONS[name],
        input_schema=_SCHEMAS[name],
        handler=handler,
    )
    for name, handler in [
        ("list_categories",          _handle_list_categories),
        ("list_accounts",            _handle_list_accounts),
        ("query_spending_breakdown", _handle_query_spending_breakdown),
        ("query_income_breakdown",   _handle_query_income_breakdown),
        ("list_transactions",        _handle_list_transactions),
        ("category_trend",           _handle_category_trend),
        ("top_merchants",            _handle_top_merchants),
        ("compare_periods",          _handle_compare_periods),
        ("cashflow_summary",         _handle_cashflow_summary),
        # Override / rule tools (Phase 1C).
        ("preview_rule_matches",     _handle_preview_rule_matches),
        ("set_override",             _handle_set_override),
        ("set_overrides_bulk",       _handle_set_overrides_bulk),
        ("clear_override",           _handle_clear_override),
        ("create_category_rule",     _handle_create_category_rule),
        ("list_category_rules",      _handle_list_category_rules),
        ("delete_category_rule",     _handle_delete_category_rule),
        ("list_overrides",           _handle_list_overrides),
        ("list_override_history",    _handle_list_override_history),
    ]
}


# --- Adapters + dispatch -----------------------------------------------------


def to_openai_tools(registry: dict[str, ToolSpec] = REGISTRY) -> list[dict]:
    """Shape-shift REGISTRY into OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }
        for spec in registry.values()
    ]


def list_tools_for_debug(registry: dict[str, ToolSpec] = REGISTRY) -> dict:
    """JSON snapshot of the registry, served from `GET /chat/tools` for local
    development inspection. Not an external surface."""
    return {
        "tools": [
            {
                "name": s.name,
                "description": s.description,
                "inputSchema": s.input_schema,
            }
            for s in registry.values()
        ],
    }


def dispatch(user_id: str, name: str, args: dict) -> dict:
    """Run a tool by name. Returns the handler's dict or `{error: ...}`."""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"error": f"unknown tool '{name}'",
                "available": sorted(REGISTRY.keys())}
    args = dict(args or {})
    try:
        _validate_args(spec, args)
        return spec.handler(user_id, args)
    except ToolError as e:
        return {"error": str(e), **e.extras}
    except sqlite3.Error as e:
        return {"error": f"database error: {e}"}
    except Exception as e:  # pragma: no cover — defensive
        return {"error": f"unexpected error: {type(e).__name__}: {e}"}


__all__ = [
    "ToolError",
    "ToolSpec",
    "REGISTRY",
    "to_openai_tools",
    "list_tools_for_debug",
    "dispatch",
]
