"""Reconciliation layer — materializes `transactions_recon` from raw `transactions`.

`transactions` stays immutable (raw source + LLM's first-guess labels).
`transactions_recon` is a **pure, deterministic function of raw**, 1:1 by `id`,
holding the reconciled interpretation every consumer reads through
`v_transactions_recon`:

  - flow_type_recon      — corrected flow_type (paired transfer legs become 'transfer')
  - is_internal_transfer — 1 if the row is one leg of a paired internal transfer
  - transfer_group_id    — links the two legs of a pair (audit / display)
  - is_duplicate         — 1 if a cross-source duplicate was collapsed onto another row
  - signed_amount        — per-account balance sign (mirrors v_transactions_signed)

Rebuilds are per-user and full (DELETE this user's rows, recompute). It only
needs to run when raw changes: on ingest, via the CLI, or lazily on read when a
freshness check sees raw is newer than recon. Never on a timer.

See design/storage.md → "Reconciliation layer".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.storage import TransactionStore

# --- Tunables (transfer pairing) ---------------------------------------------

TRANSFER_DATE_WINDOW_DAYS: int = 3
TRANSFER_AMOUNT_TOLERANCE: Decimal = Decimal("0.01")

# Memo tokens that mark a bank-to-bank movement (used to recognize self-transfers
# like a Zelle to/from your own account without a credit-card leg).
_TRANSFER_KEYWORDS = ("zelle", "transfer", "xfer", "wire", "ach trnsfr")

# Source priority when collapsing cross-source duplicates — keep the most
# authoritative posted record, flag the rest.
_SOURCE_PRIORITY = {"statement_pdf": 0, "statement_csv": 1, "plaid": 2}

# Columns we read from raw to reconcile.
_RAW_COLS = (
    "id", "user_id", "date", "amount", "description", "category",
    "account_type", "account_id", "section_type", "flow_type",
    "source", "dedup_hash",
)


# --- Sign (mirrors v_transactions_signed.account_flow) -----------------------


def _signed_amount(account_type: str, section_type: str, amount: float) -> float:
    """Per-account balance sign: + raised the account's number, − lowered it."""
    a = account_type
    s = section_type
    if a == "credit":
        if s in ("purchase", "cash_advance", "interest_charged", "fee"):
            return amount
        if s in ("payment", "refund", "interest_credited"):
            return -amount
    elif a in ("checking", "savings"):
        if s in ("deposit", "interest_credited", "refund"):
            return amount
        if s in ("withdrawal", "check", "fee", "interest_charged"):
            return -amount
    return 0.0


def _has_transfer_keyword(description: str) -> bool:
    d = (description or "").lower()
    return any(k in d for k in _TRANSFER_KEYWORDS)


# --- Pairing -----------------------------------------------------------------


def _pair_transfers(rows: list[dict]) -> dict[str, str]:
    """Return {row_id: transfer_group_id} for rows that are one leg of a pair.

    Two complementary rules, each a greedy 1:1 match within a ±window:

    Rule A — credit-card payment: a credit `payment` leg pairs with a
        checking/savings `withdrawal` leg (the money leaving the funding account).
    Rule B — bank self-transfer: a checking/savings `deposit` pairs with a
        checking/savings `withdrawal` when BOTH memos look like transfers
        (e.g. a Zelle to/from your own account). The keyword guard keeps real
        income (payroll deposits) from being mistaken for a transfer.
    """
    groups: dict[str, str] = {}
    used: set[str] = set()

    def _amt(r: dict) -> Decimal:
        return Decimal(str(r["amount"]))

    def _within_window(a: dict, b: dict) -> bool:
        from datetime import date as _date
        da = _date.fromisoformat(a["date"])
        db = _date.fromisoformat(b["date"])
        return abs((da - db).days) <= TRANSFER_DATE_WINDOW_DAYS

    def _match(left: list[dict], right: list[dict]) -> None:
        for l in left:
            if l["id"] in used:
                continue
            for r in right:
                if r["id"] in used:
                    continue
                if abs(_amt(l) - _amt(r)) > TRANSFER_AMOUNT_TOLERANCE:
                    continue
                if not _within_window(l, r):
                    continue
                gid = f"grp_{min(l['id'], r['id'])}"
                groups[l["id"]] = gid
                groups[r["id"]] = gid
                used.add(l["id"])
                used.add(r["id"])
                break

    bank_withdrawals = [
        r for r in rows
        if r["account_type"] in ("checking", "savings") and r["section_type"] == "withdrawal"
    ]

    # Rule A: credit payments ↔ bank withdrawals.
    credit_payments = [
        r for r in rows
        if r["account_type"] == "credit" and r["section_type"] == "payment"
    ]
    _match(credit_payments, bank_withdrawals)

    # Rule B: bank deposits ↔ bank withdrawals, transfer memos on both sides.
    bank_deposits_xfer = [
        r for r in rows
        if r["account_type"] in ("checking", "savings")
        and r["section_type"] == "deposit"
        and _has_transfer_keyword(r["description"])
    ]
    bank_withdrawals_xfer = [
        r for r in bank_withdrawals if _has_transfer_keyword(r["description"])
    ]
    _match(bank_deposits_xfer, bank_withdrawals_xfer)

    return groups


def _mark_duplicates(rows: list[dict]) -> set[str]:
    """Conservative cross-source dedup: only collapse when the SAME dedup_hash
    appears under more than one `source`. Same-source repeats (two identical
    coffees) are left alone — they're probably real. Keep the highest-priority
    source's row; flag the others.
    """
    by_hash: dict[str, list[dict]] = {}
    for r in rows:
        h = r.get("dedup_hash") or ""
        if not h:
            continue
        by_hash.setdefault(h, []).append(r)

    dupes: set[str] = set()
    for group in by_hash.values():
        sources = {r.get("source") or "" for r in group}
        if len(group) < 2 or len(sources) < 2:
            continue  # not a cross-source collision
        # Keep the most authoritative; flag the rest.
        keeper = min(
            group,
            key=lambda r: (_SOURCE_PRIORITY.get(r.get("source") or "", 99), r["id"]),
        )
        for r in group:
            if r["id"] != keeper["id"]:
                dupes.add(r["id"])
    return dupes


# --- Pure reconciliation -----------------------------------------------------


def reconcile(rows: list[dict]) -> list[dict]:
    """Pure transform: raw rows (one user) → recon rows. No I/O.

    Each input row is a dict with the columns in `_RAW_COLS`. Output rows carry
    the recon overlay (no `reconciled_at` — the writer stamps that).
    """
    group_for = _pair_transfers(rows)
    duplicate_ids = _mark_duplicates(rows)

    out: list[dict] = []
    for r in rows:
        rid = r["id"]
        is_transfer = rid in group_for
        # flow_type correction: a paired leg is a transfer regardless of what the
        # source/LLM first called it (this is what fixes a self-Zelle deposit
        # that leaked into 'income').
        flow_type_recon = "transfer" if is_transfer else (r.get("flow_type") or "unknown")
        out.append({
            "id": rid,
            "user_id": r.get("user_id") or "",
            "flow_type_recon": flow_type_recon,
            "signed_amount": _signed_amount(
                r.get("account_type") or "", r.get("section_type") or "", float(r["amount"])
            ),
            "is_internal_transfer": 1 if is_transfer else 0,
            "transfer_group_id": group_for.get(rid),
            "is_duplicate": 1 if rid in duplicate_ids else 0,
        })
    return out


# --- Persistence -------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    TransactionStore.init_db()
    conn = sqlite3.connect(TransactionStore.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _read_raw(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    sql = f"SELECT {', '.join(_RAW_COLS)} FROM transactions WHERE user_id = ? ORDER BY date ASC, id ASC"
    return [dict(r) for r in conn.execute(sql, (user_id,)).fetchall()]


def rebuild_recon(user_id: str) -> int:
    """Full deterministic rebuild of one user's recon rows. Returns row count.

    Per-user by design: pairing is within-user anyway, and scoping the rebuild
    keeps one tenant's recompute from touching another's rows.
    """
    now = datetime.now().isoformat()
    with _connect() as conn:
        raw = _read_raw(conn, user_id)
        recon = reconcile(raw)
        conn.execute("DELETE FROM transactions_recon WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO transactions_recon "
            "(id, user_id, flow_type_recon, signed_amount, is_internal_transfer, "
            " transfer_group_id, is_duplicate, reconciled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    x["id"], x["user_id"], x["flow_type_recon"], x["signed_amount"],
                    x["is_internal_transfer"], x["transfer_group_id"], x["is_duplicate"], now,
                )
                for x in recon
            ],
        )
        conn.commit()
        return len(recon)


def ensure_recon_fresh(user_id: str) -> None:
    """Lazy guard: rebuild this user's recon iff raw is newer (or recon is empty
    while raw has rows). Cheap two-SELECT check; a no-op once fresh.
    """
    with _connect() as conn:
        raw_n, raw_max = conn.execute(
            "SELECT COUNT(*), MAX(ingested_at) FROM transactions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        rec_n, rec_max = conn.execute(
            "SELECT COUNT(*), MAX(reconciled_at) FROM transactions_recon WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not raw_n:
        return
    # ISO timestamps compare lexically. Rebuild after a rebuild sets rec_max > raw_max;
    # a fresh ingest advances raw_max past it → stale.
    if not rec_n or (raw_max or "") > (rec_max or ""):
        rebuild_recon(user_id)


def rebuild_all() -> dict[str, int]:
    """Rebuild every user's recon (one-time backfill / after a logic change)."""
    with _connect() as conn:
        user_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM transactions"
        ).fetchall()]
    return {uid: rebuild_recon(uid) for uid in user_ids}


__all__ = [
    "reconcile",
    "rebuild_recon",
    "ensure_recon_fresh",
    "rebuild_all",
    "TRANSFER_DATE_WINDOW_DAYS",
    "TRANSFER_AMOUNT_TOLERANCE",
]
