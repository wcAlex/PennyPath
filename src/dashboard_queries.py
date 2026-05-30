"""Dashboard aggregation reads for Phase 1B.

This module owns the read-time logic for the four standard charts:
Spending, Income, Transactions, Cash Flow. It reads the **reconciled** view
`v_transactions_recon` — transfer pairing, flow_type correction, dedup, and
sign all live in `src/reconciler.py` now and are materialized once per user.
This module just aggregates; it never re-derives those decisions.

All queries are read-only against `data/transactions.db`.

See design/ui_dashboard.md §3 and design/storage.md → Reconciliation layer.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from src.storage import TransactionStore

# --- Tunable thresholds ------------------------------------------------------

# A category is "Fixed" if appears in >= FIXED_MIN_MONTHS of the last
# FIXED_LOOKBACK_MONTHS AND its coefficient of variation
# (stddev/mean) over those months is <= FIXED_COV_THRESHOLD.
# Start at 0.25 per design; tweak after a few weeks of real data.
FIXED_COV_THRESHOLD: float = 0.25
FIXED_MIN_MONTHS: int = 4
FIXED_LOOKBACK_MONTHS: int = 6


# --- Helpers -----------------------------------------------------------------


def _db_path() -> Path:
    """Resolve the DB path at call time so monkeypatched tests work."""
    return TransactionStore.DB_PATH


_initialized_path: Optional[str] = None


def _connect() -> sqlite3.Connection:
    # Ensure the schema + v_transactions_signed view exist before reading, so a
    # fresh install (or a monkeypatched test DB) returns empty results instead of
    # raising "no such table". init_db is idempotent; we run it once per DB path.
    global _initialized_path
    path = str(_db_path())
    if _initialized_path != path:
        TransactionStore.init_db()
        _initialized_path = path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _parse_iso(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _period_label(start: date, end: date) -> str:
    """Human-readable label, e.g. '2026-05' if it covers exactly one month."""
    if start.year == end.year and start.month == end.month and start.day == 1:
        # If end is the last day of the same month, label as YYYY-MM.
        next_month_first = (date(start.year + (start.month // 12),
                                  (start.month % 12) + 1, 1))
        if end == next_month_first - timedelta(days=1):
            return f"{start.year:04d}-{start.month:02d}"
    return f"{_iso(start)}..{_iso(end)}"


def _month_first(d: date) -> date:
    return date(d.year, d.month, 1)


def _month_last(d: date) -> date:
    nxt = date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
    return nxt - timedelta(days=1)


def _months_back(today: date, n: int) -> list[str]:
    """Return YYYY-MM keys for the last n months ending at today (oldest first)."""
    out: list[str] = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def _month_key(d: str) -> str:
    """'2026-05-21' -> '2026-05'."""
    return d[:7]


def _to_f(v) -> float:
    """Decimal/None-safe cast for JSON output."""
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


# --- Reading the reconciled view ---------------------------------------------


def _fetch_recon_rows(
    user_id: str,
    start: date,
    end: date,
    account_id: Optional[str] = None,
) -> list[dict]:
    """Read v_transactions_recon for the window — recon overlay already applied.

    Ensures the user's recon is fresh first, so a brand-new ingest is reflected
    even if the rebuild trigger was missed.
    """
    from src.reconciler import ensure_recon_fresh
    ensure_recon_fresh(user_id)

    sql = (
        "SELECT id, date, amount, description, category, account_type, "
        "account_id, user_id, section_type, flow_type, flow_type_recon, "
        "signed_amount, is_internal_transfer, is_duplicate "
        "FROM v_transactions_recon "
        "WHERE user_id = ? AND date BETWEEN ? AND ?"
    )
    params: list = [user_id, _iso(start), _iso(end)]
    if account_id:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " ORDER BY date ASC, id ASC"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_transactions_signed(
    user_id: str,
    start: date,
    end: date,
    account_id: Optional[str] = None,
) -> list[dict]:
    """Reconciled rows for the window.

    Keeps the key names the rest of this module already uses, now sourced from
    the recon overlay: `flow_type` is the *reconciled* type, `is_paired_transfer`
    mirrors the recon transfer flag, `account_flow` is the signed amount. Adds
    `is_duplicate`.
    """
    rows = _fetch_recon_rows(user_id, start, end, account_id)
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "date": r["date"],
            "amount": r["amount"],
            "description": r["description"],
            "category": r["category"],
            "account_type": r["account_type"],
            "account_id": r["account_id"],
            "user_id": r["user_id"],
            "section_type": r["section_type"],
            "flow_type": r["flow_type_recon"],
            "account_flow": r["signed_amount"],
            "is_paired_transfer": bool(r["is_internal_transfer"]),
            "is_duplicate": bool(r["is_duplicate"]),
        })
    return out


def _excluded_from_spending(row: dict) -> bool:
    """Excluded from spending if it's an internal transfer or a cross-source duplicate."""
    return row["is_paired_transfer"] or row["flow_type"] == "transfer" or row["is_duplicate"]


def _excluded_from_income(row: dict) -> bool:
    """Excluded from income if it's an internal transfer or a cross-source duplicate."""
    return row["is_paired_transfer"] or row["flow_type"] == "transfer" or row["is_duplicate"]


# --- Spending ----------------------------------------------------------------


def _spending_total_for_window(
    user_id: str,
    start: date,
    end: date,
    account_id: Optional[str] = None,
) -> tuple[Decimal, dict[str, Decimal]]:
    """Returns (total, per_category) for the window, exclusions applied."""
    rows = list_transactions_signed(user_id, start, end, account_id)
    total = Decimal("0")
    by_cat: dict[str, Decimal] = {}
    for r in rows:
        if r["flow_type"] != "spending":
            continue
        if _excluded_from_spending(r):
            continue
        amt = Decimal(str(r["amount"]))
        total += amt
        cat = r["category"] or "Uncategorized"
        by_cat[cat] = by_cat.get(cat, Decimal("0")) + amt
    return total, by_cat


def _previous_window(start: date, end: date) -> tuple[date, date]:
    """Same-length window immediately before [start, end]."""
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return prev_start, prev_end


def _avg_6mo_by_category(
    user_id: str,
    end: date,
    account_id: Optional[str] = None,
) -> dict[str, float]:
    """Rolling 6-month per-category mean of spending, ending at `end`'s month."""
    months = _months_back(end, 6)
    if not months:
        return {}
    first_month = months[0]
    last_month = months[-1]
    start = _parse_iso(first_month + "-01")
    end_of_last = _month_last(_parse_iso(last_month + "-01"))
    rows = list_transactions_signed(user_id, start, end_of_last, account_id)
    # Per-category total per month.
    per_month: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        if r["flow_type"] != "spending":
            continue
        if _excluded_from_spending(r):
            continue
        mk = _month_key(r["date"])
        cat = r["category"] or "Uncategorized"
        per_month.setdefault(cat, {})
        per_month[cat][mk] = per_month[cat].get(mk, Decimal("0")) + Decimal(str(r["amount"]))
    # Mean over the 6-month window (months with zero spend still divide by 6).
    out: dict[str, float] = {}
    for cat, by_month in per_month.items():
        total = sum((by_month.get(m, Decimal("0")) for m in months), Decimal("0"))
        out[cat] = float(total / Decimal(6))
    return out


def spending_breakdown(
    user_id: str,
    start: date,
    end: date,
    account_id: Optional[str] = None,
) -> dict:
    """Spending donut + category list.

    Returns:
        {
            "period": "YYYY-MM" or "start..end",
            "total_spend": float,
            "categories": [{"name", "amount", "avg_6mo"}, ...],
            "previous_period_total": float,
        }

    Excludes paired internal transfers and any flow_type='transfer' rows.
    avg_6mo is the rolling 6-month per-category mean.
    """
    total, by_cat = _spending_total_for_window(user_id, start, end, account_id)
    avg_6mo = _avg_6mo_by_category(user_id, end, account_id)
    prev_start, prev_end = _previous_window(start, end)
    prev_total, _ = _spending_total_for_window(user_id, prev_start, prev_end, account_id)

    categories = [
        {
            "name": cat,
            "amount": _to_f(amt),
            "avg_6mo": _to_f(avg_6mo.get(cat, 0.0)),
        }
        for cat, amt in sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return {
        "period": _period_label(start, end),
        "total_spend": _to_f(total),
        "categories": categories,
        "previous_period_total": _to_f(prev_total),
    }


# --- Income ------------------------------------------------------------------


def _income_total_for_window(
    user_id: str,
    start: date,
    end: date,
    account_id: Optional[str] = None,
) -> tuple[Decimal, dict[str, Decimal]]:
    rows = list_transactions_signed(user_id, start, end, account_id)
    total = Decimal("0")
    by_cat: dict[str, Decimal] = {}
    for r in rows:
        if r["flow_type"] != "income":
            continue
        if _excluded_from_income(r):
            continue
        amt = Decimal(str(r["amount"]))
        total += amt
        cat = r["category"] or "Income"
        by_cat[cat] = by_cat.get(cat, Decimal("0")) + amt
    return total, by_cat


def income_breakdown(
    user_id: str,
    start: date,
    end: date,
    account_id: Optional[str] = None,
) -> dict:
    """Income donut + 12-month bar chart + avg.

    Returns:
        {
            "period": str,
            "total_income": float,
            "subcategories": [{"name", "amount", "pct"}, ...],
            "monthly_history": [{"month": "YYYY-MM", "total": float}, ...],
            "avg_monthly": float,
        }
    """
    total, by_cat = _income_total_for_window(user_id, start, end, account_id)
    subcategories = []
    if total > 0:
        for cat, amt in sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True):
            subcategories.append({
                "name": cat,
                "amount": _to_f(amt),
                "pct": float((amt / total) * Decimal(100)),
            })

    # 12-month income history ending at end's month.
    months = _months_back(end, 12)
    if months:
        hist_start = _parse_iso(months[0] + "-01")
        hist_end = _month_last(_parse_iso(months[-1] + "-01"))
        rows = list_transactions_signed(user_id, hist_start, hist_end, account_id)
        per_month: dict[str, Decimal] = {m: Decimal("0") for m in months}
        for r in rows:
            if r["flow_type"] != "income":
                continue
            if _excluded_from_income(r):
                continue
            mk = _month_key(r["date"])
            if mk in per_month:
                per_month[mk] += Decimal(str(r["amount"]))
        monthly_history = [{"month": m, "total": _to_f(per_month[m])} for m in months]
        avg_monthly = _to_f(sum(per_month.values(), Decimal("0")) / Decimal(12))
    else:
        monthly_history = []
        avg_monthly = 0.0

    return {
        "period": _period_label(start, end),
        "total_income": _to_f(total),
        "subcategories": subcategories,
        "monthly_history": monthly_history,
        "avg_monthly": avg_monthly,
    }


# --- Transactions (filterable list) ------------------------------------------


def transactions_filtered(
    user_id: str,
    start: date,
    end: date,
    *,
    category: Optional[list[str]] = None,
    account_id: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Filterable, paginated transaction list. Paired transfers are *shown*
    (with the flag set) so the UI can mute-style them; they're never hidden
    from this view per the design.
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 50

    rows = list_transactions_signed(user_id, start, end, account_id)

    def _keep(r: dict) -> bool:
        if category and r["category"] not in category:
            return False
        if min_amount is not None and r["amount"] < min_amount:
            return False
        if max_amount is not None and r["amount"] > max_amount:
            return False
        if q:
            ql = q.lower()
            if ql not in r["description"].lower() and ql not in (r["category"] or "").lower():
                return False
        return True

    filtered = [r for r in rows if _keep(r)]
    # Most recent first.
    filtered.sort(key=lambda r: (r["date"], r["id"]), reverse=True)

    total = len(filtered)
    lo = (page - 1) * page_size
    hi = lo + page_size
    page_rows = filtered[lo:hi]

    out_rows = []
    for r in page_rows:
        out_rows.append({
            "id": r["id"],
            "date": r["date"],
            "merchant": r["description"],
            "account_id": r["account_id"],
            "account_type": r["account_type"],
            "category": r["category"],
            "amount": _to_f(r["amount"]),
            "amount_signed": _to_f(r["account_flow"]),
            "section_type": r["section_type"],
            "flow_type": r["flow_type"],
            "is_paired_transfer": r["is_paired_transfer"],
            "is_duplicate": r["is_duplicate"],
        })

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "rows": out_rows,
    }


# --- Cash Flow + Fixed vs Flexible -------------------------------------------


def _spending_per_month_with_categories(
    user_id: str,
    months: list[str],
    account_id: Optional[str] = None,
) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]]]:
    """For each month key in `months`, return total spending and per-category totals.

    Exclusions applied. Returns (per_month_total, per_category_per_month).
    """
    if not months:
        return {}, {}
    start = _parse_iso(months[0] + "-01")
    end = _month_last(_parse_iso(months[-1] + "-01"))
    rows = list_transactions_signed(user_id, start, end, account_id)
    per_month: dict[str, Decimal] = {m: Decimal("0") for m in months}
    per_cat: dict[str, dict[str, Decimal]] = {}
    for r in rows:
        if r["flow_type"] != "spending":
            continue
        if _excluded_from_spending(r):
            continue
        mk = _month_key(r["date"])
        if mk not in per_month:
            continue
        amt = Decimal(str(r["amount"]))
        per_month[mk] += amt
        cat = r["category"] or "Uncategorized"
        per_cat.setdefault(cat, {m: Decimal("0") for m in months})
        per_cat[cat][mk] += amt
    return per_month, per_cat


def _income_per_month(
    user_id: str,
    months: list[str],
    account_id: Optional[str] = None,
) -> dict[str, Decimal]:
    if not months:
        return {}
    start = _parse_iso(months[0] + "-01")
    end = _month_last(_parse_iso(months[-1] + "-01"))
    rows = list_transactions_signed(user_id, start, end, account_id)
    per_month: dict[str, Decimal] = {m: Decimal("0") for m in months}
    for r in rows:
        if r["flow_type"] != "income":
            continue
        if _excluded_from_income(r):
            continue
        mk = _month_key(r["date"])
        if mk in per_month:
            per_month[mk] += Decimal(str(r["amount"]))
    return per_month


def _classify_fixed_vs_flexible(
    per_cat_per_month: dict[str, dict[str, Decimal]],
    months: list[str],
) -> tuple[list[dict], list[dict]]:
    """Apply the Fixed/Flexible heuristic.

    Fixed if:
      - appears in >= FIXED_MIN_MONTHS of the last FIXED_LOOKBACK_MONTHS, AND
      - coefficient of variation (stddev/mean) of monthly totals <= FIXED_COV_THRESHOLD.
    """
    fixed: list[dict] = []
    flexible: list[dict] = []
    lookback = months[-FIXED_LOOKBACK_MONTHS:] if len(months) >= FIXED_LOOKBACK_MONTHS else months

    for cat, by_month in per_cat_per_month.items():
        # 12-month-style view used for output (avg, monthly map).
        values_all = [float(by_month.get(m, Decimal("0"))) for m in months]
        monthly_map = {m: _to_f(by_month.get(m, Decimal("0"))) for m in months}
        avg_all = sum(values_all) / len(values_all) if values_all else 0.0

        # Classification uses the lookback window only.
        values_lb = [float(by_month.get(m, Decimal("0"))) for m in lookback]
        nonzero = [v for v in values_lb if v > 0.0]
        is_fixed = False
        if len(nonzero) >= FIXED_MIN_MONTHS and len(values_lb) >= 2:
            mean_v = sum(values_lb) / len(values_lb)
            if mean_v > 0:
                stdev_v = statistics.pstdev(values_lb)
                cov = stdev_v / mean_v
                if cov <= FIXED_COV_THRESHOLD:
                    is_fixed = True

        entry = {
            "name": cat,
            "monthly": monthly_map,
            "avg": avg_all,
        }
        (fixed if is_fixed else flexible).append(entry)

    fixed.sort(key=lambda c: c["avg"], reverse=True)
    flexible.sort(key=lambda c: c["avg"], reverse=True)
    return fixed, flexible


def fixed_vs_flexible(
    user_id: str,
    months: int = 6,
) -> tuple[list[dict], list[dict]]:
    """Classify categories into Fixed vs Flexible using the last `months` months
    of spending data ending at today's month.
    """
    today = date.today()
    month_keys = _months_back(today, months)
    _, per_cat = _spending_per_month_with_categories(user_id, month_keys)
    return _classify_fixed_vs_flexible(per_cat, month_keys)


def cashflow_series(
    user_id: str,
    months: int = 12,
    account_id: Optional[str] = None,
) -> dict:
    """Cash Flow chart payload: 12-month income vs spending + fixed/flex tables.

    Returns:
        {
            "months": [...],
            "income_per_month": [...],
            "spending_per_month": [...],
            "avg_spending": float,
            "avg_net": float,
            "fixed_categories": [{"name","monthly":{m:amount},"avg"}, ...],
            "flexible_categories": [...],
        }
    """
    today = date.today()
    month_keys = _months_back(today, months)
    spend_per_month, per_cat = _spending_per_month_with_categories(user_id, month_keys, account_id)
    inc_per_month = _income_per_month(user_id, month_keys, account_id)

    spending_list = [_to_f(spend_per_month.get(m, Decimal("0"))) for m in month_keys]
    income_list = [_to_f(inc_per_month.get(m, Decimal("0"))) for m in month_keys]

    n = max(len(month_keys), 1)
    avg_spending = sum(spending_list) / n
    avg_income = sum(income_list) / n
    avg_net = avg_income - avg_spending

    fixed_cats, flexible_cats = _classify_fixed_vs_flexible(per_cat, month_keys)

    return {
        "months": month_keys,
        "income_per_month": income_list,
        "spending_per_month": spending_list,
        "avg_spending": avg_spending,
        "avg_net": avg_net,
        "fixed_categories": fixed_cats,
        "flexible_categories": flexible_cats,
    }


# --- Period parsing ----------------------------------------------------------


def parse_period(
    period: Optional[str],
    start: Optional[str],
    end: Optional[str],
    *,
    today: Optional[date] = None,
) -> tuple[date, date]:
    """Resolve a (start, end) window from the dashboard's query conventions.

    Supported:
      - explicit start/end (overrides `period`)
      - period='YYYY-MM' single month
      - period='ytd' year to date
      - period='last-12mo' trailing 12 months
      - None / '' → current month

    Raises ValueError on invalid input.
    """
    today = today or date.today()

    if start or end:
        if not (start and end):
            raise ValueError("Both 'start' and 'end' must be provided together.")
        s = _parse_iso(start)
        e = _parse_iso(end)
        if e < s:
            raise ValueError("'end' must not precede 'start'.")
        return s, e

    if not period:
        return _month_first(today), _month_last(today)

    period_l = period.strip().lower()

    if period_l == "ytd":
        return date(today.year, 1, 1), today

    if period_l in ("last-12mo", "last12mo", "12mo"):
        first = today.replace(day=1)
        # 11 months back
        y, m = first.year, first.month
        for _ in range(11):
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        return date(y, m, 1), today

    # YYYY-MM
    try:
        parts = period_l.split("-")
        if len(parts) == 2:
            y = int(parts[0])
            m = int(parts[1])
            if not (1 <= m <= 12):
                raise ValueError
            start_d = date(y, m, 1)
            return start_d, _month_last(start_d)
    except (ValueError, IndexError):
        pass

    raise ValueError(f"Unrecognized period: {period!r}")


# --- Chat-agent helpers (Phase 1C) -------------------------------------------
#
# The three helpers below back chat tools (`category_trend`, `top_merchants`,
# `compare_periods`). They read the same reconciled view and reuse the same
# exclusion rules as the dashboard, so chat numbers match the dashboard.


def category_trend(
    user_id: str,
    category: str,
    months: int = 12,
    account_id: Optional[str] = None,
    flow: str = "spending",
    *,
    today: Optional[date] = None,
) -> dict:
    """Monthly totals for one category over the last `months` months ending today.

    `category` matches case-insensitively. `flow` is "spending" or "income".
    Excludes internal transfers and duplicates.
    """
    flow = flow.lower()
    if flow not in ("spending", "income"):
        raise ValueError("flow must be 'spending' or 'income'")
    if months < 1:
        months = 1
    cat_lc = (category or "").lower()
    today = today or date.today()
    keys = _months_back(today, months)
    if not keys:
        return {"months": [], "amounts": [], "avg": 0.0, "peak": None, "trough": None}

    start = _parse_iso(keys[0] + "-01")
    end = _month_last(_parse_iso(keys[-1] + "-01"))
    rows = list_transactions_signed(user_id, start, end, account_id)
    per_month: dict[str, Decimal] = {m: Decimal("0") for m in keys}
    for r in rows:
        if r["flow_type"] != flow:
            continue
        if r["is_paired_transfer"] or r["is_duplicate"]:
            continue
        if (r["category"] or "").lower() != cat_lc:
            continue
        mk = _month_key(r["date"])
        if mk in per_month:
            per_month[mk] += Decimal(str(r["amount"]))

    amounts = [_to_f(per_month[m]) for m in keys]
    nonzero_pairs = [(m, a) for m, a in zip(keys, amounts) if a > 0]
    peak = max(nonzero_pairs, key=lambda p: p[1]) if nonzero_pairs else None
    trough = min(nonzero_pairs, key=lambda p: p[1]) if nonzero_pairs else None
    avg = sum(amounts) / max(len(amounts), 1)
    return {
        "months": keys,
        "amounts": amounts,
        "avg": avg,
        "peak": {"month": peak[0], "amount": peak[1]} if peak else None,
        "trough": {"month": trough[0], "amount": trough[1]} if trough else None,
    }


# Patterns we strip before grouping merchants. The descriptor field on bank
# statements is noisy: store IDs, locations, and processor prefixes vary by
# bank but the underlying merchant string is usually stable up to one of these
# tokens.
_MERCHANT_PREFIXES = re.compile(
    r"^(?:tst\*|sq\s*\*|sp\s*\*|paypal\s*\*|venmo\s*\*|amzn\s*mktp\s*\*?)\s*",
    re.IGNORECASE,
)
_MERCHANT_TRAIL = re.compile(
    r"(?:"
    r"\s+#\s*\d+.*$"                # "#12345 ..."
    r"|\s+\d{3,}\s+[A-Z]{2}\s*$"    # "12345 NY"
    r"|\s+[A-Z]{2}\s*$"             # trailing state code
    r"|\s+\d{5}(?:-\d{4})?\s*$"     # trailing zip
    r")",
    re.IGNORECASE,
)


def _canonical_merchant(desc: str) -> str:
    """Best-effort merchant canonicalization. Lowercased, stripped of common
    processor prefixes and trailing location/store tokens, then title-cased.
    Conservative — if a description doesn't match any pattern, it falls through
    unchanged (only case-normalized).
    """
    s = (desc or "").strip()
    if not s:
        return ""
    s = _MERCHANT_PREFIXES.sub("", s)
    # Strip up to two trailing tokens (zip after state, e.g.).
    for _ in range(2):
        new = _MERCHANT_TRAIL.sub("", s)
        if new == s:
            break
        s = new.strip()
    # Collapse whitespace.
    s = " ".join(s.split())
    # Title-case for readability, but keep already-capitalized acronyms intact.
    return s.title() if s.islower() or s.isupper() else s


def top_merchants(
    user_id: str,
    start: date,
    end: date,
    category: Optional[str] = None,
    account_id: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Top N merchants by total spend in the window. Excludes transfers/dupes."""
    if limit < 1:
        limit = 1
    cat_lc = category.lower() if category else None
    rows = list_transactions_signed(user_id, start, end, account_id)
    by_merchant: dict[str, dict] = {}
    for r in rows:
        if r["flow_type"] != "spending":
            continue
        if r["is_paired_transfer"] or r["is_duplicate"]:
            continue
        if cat_lc and (r["category"] or "").lower() != cat_lc:
            continue
        name = _canonical_merchant(r["description"]) or "Unknown"
        bucket = by_merchant.setdefault(name, {"name": name, "total": Decimal("0"),
                                                "visits": 0, "last_seen": ""})
        bucket["total"] += Decimal(str(r["amount"]))
        bucket["visits"] += 1
        if r["date"] > bucket["last_seen"]:
            bucket["last_seen"] = r["date"]
    out = sorted(by_merchant.values(), key=lambda b: b["total"], reverse=True)[:limit]
    return [{"name": b["name"], "total": _to_f(b["total"]),
             "visits": b["visits"], "last_seen": b["last_seen"]} for b in out]


def compare_periods(
    user_id: str,
    period_a_start: date,
    period_a_end: date,
    period_b_start: date,
    period_b_end: date,
    category: Optional[str] = None,
    account_id: Optional[str] = None,
    mover_dim: str = "category",
) -> dict:
    """Compare spending in two periods. Returns totals + top movers."""
    mover_dim = mover_dim.lower()
    if mover_dim not in ("category", "merchant"):
        raise ValueError("mover_dim must be 'category' or 'merchant'")
    cat_lc = category.lower() if category else None

    def _bucket(start: date, end: date) -> tuple[Decimal, dict[str, Decimal]]:
        rows = list_transactions_signed(user_id, start, end, account_id)
        total = Decimal("0")
        by_key: dict[str, Decimal] = {}
        for r in rows:
            if r["flow_type"] != "spending":
                continue
            if r["is_paired_transfer"] or r["is_duplicate"]:
                continue
            if cat_lc and (r["category"] or "").lower() != cat_lc:
                continue
            amt = Decimal(str(r["amount"]))
            total += amt
            key = (r["category"] or "Uncategorized") if mover_dim == "category" \
                else (_canonical_merchant(r["description"]) or "Unknown")
            by_key[key] = by_key.get(key, Decimal("0")) + amt
        return total, by_key

    total_a, by_a = _bucket(period_a_start, period_a_end)
    total_b, by_b = _bucket(period_b_start, period_b_end)

    keys = set(by_a) | set(by_b)
    movers = []
    for k in keys:
        a = float(by_a.get(k, Decimal("0")))
        b = float(by_b.get(k, Decimal("0")))
        movers.append({"label": k, "a": a, "b": b, "delta": b - a})
    movers.sort(key=lambda m: abs(m["delta"]), reverse=True)
    top_movers = movers[:8]

    delta = float(total_b - total_a)
    pct = (delta / float(total_a) * 100.0) if total_a > 0 else None

    return {
        "period_a": {"start": _iso(period_a_start), "end": _iso(period_a_end), "total": _to_f(total_a)},
        "period_b": {"start": _iso(period_b_start), "end": _iso(period_b_end), "total": _to_f(total_b)},
        "delta": delta,
        "delta_pct": pct,
        "top_movers": top_movers,
    }


__all__ = [
    "FIXED_COV_THRESHOLD",
    "FIXED_MIN_MONTHS",
    "FIXED_LOOKBACK_MONTHS",
    "spending_breakdown",
    "income_breakdown",
    "transactions_filtered",
    "cashflow_series",
    "fixed_vs_flexible",
    "list_transactions_signed",
    "parse_period",
    "category_trend",
    "top_merchants",
    "compare_periods",
]
