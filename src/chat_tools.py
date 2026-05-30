"""In-process tool registry for the Phase 1C chat agent.

The agent loop calls `dispatch(user_id, name, args)` to invoke a tool. Each
tool is described by a `ToolSpec` (name, description, JSON Schema for inputs,
handler). The OpenAI function-calling format is derived via `to_openai_tools()`.

The registry is **internal-only** by design (`design/chat_agent.md`). No
external client speaks to it — no MCP server, no HTTP tool routes. The only
caller is `src/chat_agent.py`.

Every handler reads the reconciled view `v_transactions_recon` (and
`accounts`), never the raw `transactions` table. This guarantees the chat
numbers match the dashboard numbers.
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
        "SELECT DISTINCT category FROM v_transactions_recon "
        "WHERE user_id = ? AND is_internal_transfer = 0 AND is_duplicate = 0 "
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
        "FROM v_transactions_recon "
        "WHERE user_id = ? AND is_internal_transfer = 0 AND is_duplicate = 0 "
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
        if r["is_internal_transfer"] or r["is_duplicate"]:
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
        if r["is_internal_transfer"] or r["is_duplicate"]:
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
