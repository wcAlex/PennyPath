from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, List, Optional, Tuple
import hashlib
import json
import sqlite3
from datetime import datetime

DATA_DIR = Path("data")


@dataclass
class BudgetHint:
    """A single line of LLM-generated, text-based budget guidance for one category."""
    category: str = ""
    hint_text: str = ""


@dataclass
class UserConfig:
    name: str = ""
    finance_profile: str = ""  # "early_career"|"growing_family"|"paying_debt"|"building_wealth"|"custom"
    custom_profile: str = ""
    goal_type: str = ""        # "emergency_fund"|"reduce_spending"|"save_purchase"|"get_out_of_debt"|"custom"
    goal_label: str = ""
    goal_monthly_target: Optional[float] = None
    intentions: List[str] = field(default_factory=list)
    onboarding_complete: bool = False
    # Phase 1B additions — goal + LLM-derived text budget. Goal is the anchor
    # for dashboard insights; derived_budget is plain-text hints, not numeric
    # budgets the system enforces. See design/ui_dashboard.md §6.
    goal_key: str = ""         # "stay_ahead_bills"|"pay_off_credit"|"build_credit"|"custom"|""
    goal_text: str = ""        # free-text elaboration of the goal
    derived_budget: List[BudgetHint] = field(default_factory=list)
    derived_budget_generated_at: Optional[str] = None


class UserConfigStore:
    PATH = DATA_DIR / "config.json"
    _LEGACY_PATH = DATA_DIR / "user_prefs.json"

    @classmethod
    def _coerce_budget(cls, raw) -> List[BudgetHint]:
        """Backward-compatible: tolerate missing or malformed budget hints."""
        if not raw:
            return []
        out: List[BudgetHint] = []
        for entry in raw:
            if isinstance(entry, BudgetHint):
                out.append(entry)
            elif isinstance(entry, dict):
                out.append(BudgetHint(
                    category=str(entry.get("category", "")),
                    hint_text=str(entry.get("hint_text", "")),
                ))
        return out

    @classmethod
    def load(cls) -> UserConfig:
        if cls.PATH.exists():
            with open(cls.PATH) as f:
                data = json.load(f)
            # Only keep keys that exist on UserConfig (forward-compat with new fields,
            # backward-compat with old configs that lack them).
            kwargs = {k: v for k, v in data.items() if k in UserConfig.__dataclass_fields__}
            if "derived_budget" in kwargs:
                kwargs["derived_budget"] = cls._coerce_budget(kwargs["derived_budget"])
            return UserConfig(**kwargs)

        if cls._LEGACY_PATH.exists():
            with open(cls._LEGACY_PATH) as f:
                old = json.load(f)

            name = old.get("name", "")
            saving_goal = old.get("saving_goal", None)

            # Also check old "goals" list format (first entry as saving_goal)
            if saving_goal is None and old.get("goals"):
                first = old["goals"][0]
                saving_goal = {
                    "label": first.get("label", ""),
                    "monthly_target": first.get("monthly_target") or first.get("amount"),
                }

            goal_label = ""
            goal_monthly_target = None
            goal_type = ""
            if saving_goal:
                goal_label = saving_goal.get("label", "")
                goal_monthly_target = saving_goal.get("monthly_target")
                goal_type = "custom"

            return UserConfig(
                name=name,
                goal_type=goal_type,
                goal_label=goal_label,
                goal_monthly_target=goal_monthly_target,
                intentions=old.get("intentions", []),
                onboarding_complete=bool(name),
            )

        return UserConfig()

    @classmethod
    def save(cls, config: UserConfig) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        with open(cls.PATH, "w") as f:
            json.dump(asdict(config), f, indent=2)

    @classmethod
    def is_complete(cls) -> bool:
        return cls.load().onboarding_complete


class ConversationStore:
    PATH = DATA_DIR / "memory.json"
    MAX_TURNS = 50
    SESSION_GAP_MINUTES = 30

    @classmethod
    def load(cls, max_turns: int = 20) -> List[dict]:
        if not cls.PATH.exists():
            return []
        with open(cls.PATH) as f:
            data = json.load(f)
        history = data.get("history", [])
        return history[-(max_turns * 2):]

    @classmethod
    def append(cls, role: str, content: str, session_id: Optional[str] = None) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        if cls.PATH.exists():
            with open(cls.PATH) as f:
                data = json.load(f)
            history = data.get("history", [])
        else:
            data = {}
            history = []

        entry: dict = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        if session_id:
            entry["session_id"] = session_id

        history.append(entry)
        history = history[-(cls.MAX_TURNS * 2):]

        data["history"] = history
        data["last_activity_at"] = datetime.now().isoformat()
        if session_id:
            data["current_session_id"] = session_id

        with open(cls.PATH, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def clear(cls) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        with open(cls.PATH, "w") as f:
            json.dump({"history": []}, f, indent=2)

    @classmethod
    def _new_session_id(cls) -> str:
        return f"s_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    @classmethod
    def get_current_session_id(cls) -> Optional[str]:
        if not cls.PATH.exists():
            return None
        with open(cls.PATH) as f:
            data = json.load(f)
        return data.get("current_session_id")

    @classmethod
    def rotate_session_if_needed(cls) -> Tuple[str, bool, list]:
        """
        Returns (session_id, is_new_session, prev_session_turns).
        Creates a new session if no history or last activity > SESSION_GAP_MINUTES ago.
        prev_session_turns is the turns from the old session (for wiki update).
        """
        if not cls.PATH.exists():
            new_id = cls._new_session_id()
            return new_id, True, []

        with open(cls.PATH) as f:
            data = json.load(f)

        history = data.get("history", [])
        current_id = data.get("current_session_id", "")
        last_at = data.get("last_activity_at", "")

        if not history or not current_id:
            new_id = cls._new_session_id()
            return new_id, True, []

        if last_at:
            try:
                elapsed_min = (
                    datetime.now() - datetime.fromisoformat(last_at)
                ).total_seconds() / 60
                if elapsed_min > cls.SESSION_GAP_MINUTES:
                    prev_turns = [
                        {"role": t["role"], "content": t["content"]}
                        for t in history
                        if t.get("session_id") == current_id
                    ]
                    new_id = cls._new_session_id()
                    return new_id, True, prev_turns
            except (ValueError, TypeError):
                pass

        return current_id, False, []

    @classmethod
    def get_session_turns(cls, session_id: str) -> list:
        """Return role+content dicts for turns in the given session."""
        if not cls.PATH.exists():
            return []
        with open(cls.PATH) as f:
            data = json.load(f)
        return [
            {"role": t["role"], "content": t["content"]}
            for t in data.get("history", [])
            if t.get("session_id") == session_id
        ]


class SnapshotStore:
    PATH = DATA_DIR / "snapshots.json"

    @classmethod
    def _load_all(cls) -> dict:
        if not cls.PATH.exists():
            return {}
        with open(cls.PATH) as f:
            return json.load(f)

    @classmethod
    def save(cls, period: str, category_totals: dict, total_spend: float, transaction_count: int) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        snapshots = cls._load_all()
        snapshots[period] = {
            "period": period,
            "category_totals": category_totals,
            "total_spend": total_spend,
            "transaction_count": transaction_count,
            "saved_at": datetime.now().isoformat(),
        }
        with open(cls.PATH, "w") as f:
            json.dump(snapshots, f, indent=2)

    @classmethod
    def load_recent(cls, n: int = 3) -> List[dict]:
        snapshots = cls._load_all()
        sorted_entries = sorted(snapshots.values(), key=lambda e: e["period"], reverse=True)
        return sorted_entries[:n]

    @classmethod
    def load_period(cls, period: str) -> Optional[dict]:
        snapshots = cls._load_all()
        return snapshots.get(period)


class TransactionStore:
    DB_PATH = DATA_DIR / "transactions.db"

    _INSERT_SQL = (
        "INSERT OR REPLACE INTO transactions "
        "(id, date, amount, description, category, account_type, "
        "source_file, user_id, account_id, source, dedup_hash, flow_type, "
        "notes, section_type, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    @classmethod
    def init_db(cls) -> None:
        """Create tables if they don't exist."""
        cls.DB_PATH.parent.mkdir(exist_ok=True)
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id         TEXT PRIMARY KEY,
                    user_id    TEXT NOT NULL DEFAULT '',
                    bank       TEXT NOT NULL DEFAULT '',
                    name       TEXT NOT NULL DEFAULT '',
                    mask       TEXT NOT NULL DEFAULT '',
                    type       TEXT NOT NULL DEFAULT 'unknown',
                    source     TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id           TEXT PRIMARY KEY,
                    date         TEXT NOT NULL,
                    amount       REAL NOT NULL,
                    description  TEXT NOT NULL,
                    category     TEXT NOT NULL DEFAULT '',
                    account_type TEXT NOT NULL,
                    source_file  TEXT NOT NULL DEFAULT '',
                    user_id      TEXT NOT NULL DEFAULT '',
                    account_id   TEXT NOT NULL DEFAULT '',
                    source       TEXT NOT NULL DEFAULT '',
                    dedup_hash   TEXT NOT NULL DEFAULT '',
                    flow_type    TEXT NOT NULL DEFAULT 'unknown',
                    notes        TEXT NOT NULL DEFAULT '',
                    section_type TEXT NOT NULL DEFAULT '',
                    ingested_at  TEXT NOT NULL
                )
            """)

            # Guard against a pre-account-schema database.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
            if "account_id" not in cols:
                raise RuntimeError(
                    f"{cls.DB_PATH} uses an outdated schema. "
                    f"Delete it and re-ingest:  rm {cls.DB_PATH}"
                )

            # Non-destructive migration for DBs that predate flow_type — existing
            # rows get 'unknown' and will be reclassified on the next re-ingestion.
            if "flow_type" not in cols:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN flow_type TEXT NOT NULL DEFAULT 'unknown'"
                )

            # Non-destructive migration for DBs that predate the notes column.
            if "notes" not in cols:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN notes TEXT NOT NULL DEFAULT ''"
                )

            # Non-destructive migration for DBs that predate section_type.
            if "section_type" not in cols:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN section_type TEXT NOT NULL DEFAULT ''"
                )

            # View that adds a derived account_flow column: positive when this row
            # raised the account's balance number (deposits / interest_credited on
            # checking; purchases / interest_charged / fees on credit), negative
            # when it lowered it. This is per-account-balance perspective — useful
            # for reconciling against the statement's prev/new balance values.
            # See design/storage.md for the full sign discussion.
            conn.execute("DROP VIEW IF EXISTS v_transactions_signed")
            conn.execute("""
                CREATE VIEW v_transactions_signed AS
                SELECT t.*,
                    CASE
                        -- Credit cards: balance owed goes UP for purchases/cash_advances/fees/interest,
                        -- DOWN for payments/refunds.
                        WHEN t.account_type = 'credit'
                          AND t.section_type IN ('purchase','cash_advance','interest_charged','fee') THEN  t.amount
                        WHEN t.account_type = 'credit'
                          AND t.section_type IN ('payment','refund','interest_credited') THEN -t.amount
                        -- Checking/savings: balance goes UP for deposits/interest_credited,
                        -- DOWN for withdrawals/checks/fees/interest_charged.
                        WHEN t.account_type IN ('checking','savings')
                          AND t.section_type IN ('deposit','interest_credited','refund') THEN  t.amount
                        WHEN t.account_type IN ('checking','savings')
                          AND t.section_type IN ('withdrawal','check','fee','interest_charged') THEN -t.amount
                        ELSE 0
                    END AS account_flow
                FROM transactions t
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_dedup ON transactions(dedup_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_source_file ON transactions(source_file)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_flow_type ON transactions(flow_type)")

            # Migrate file_sources: if it predates the surrogate `id` PK, rebuild
            # the table in place. Filename is no longer unique enough to be a PK
            # (different banks can share a basename); filepath is the natural key.
            existing_fs_cols = {r[1] for r in conn.execute("PRAGMA table_info(file_sources)")}
            if existing_fs_cols and "id" not in existing_fs_cols:
                have_recon = "recon_warning" in existing_fs_cols
                recon_select = "recon_warning" if have_recon else "NULL"
                conn.execute("""
                    CREATE TABLE file_sources_new (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename      TEXT NOT NULL,
                        filepath      TEXT NOT NULL UNIQUE,
                        file_mtime    REAL NOT NULL,
                        parse_method  TEXT NOT NULL,
                        tx_count      INTEGER NOT NULL,
                        parse_error   TEXT,
                        recon_warning TEXT,
                        parsed_at     TEXT NOT NULL
                    )
                """)
                conn.execute(f"""
                    INSERT INTO file_sources_new
                      (filename, filepath, file_mtime, parse_method, tx_count,
                       parse_error, recon_warning, parsed_at)
                    SELECT filename, filepath, file_mtime, parse_method, tx_count,
                           parse_error, {recon_select}, parsed_at
                    FROM file_sources
                """)
                conn.execute("DROP TABLE file_sources")
                conn.execute("ALTER TABLE file_sources_new RENAME TO file_sources")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_sources (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       TEXT NOT NULL DEFAULT '',
                    filename      TEXT NOT NULL,
                    filepath      TEXT NOT NULL,
                    file_mtime    REAL NOT NULL,
                    parse_method  TEXT NOT NULL,
                    tx_count      INTEGER NOT NULL,
                    parse_error   TEXT,
                    recon_warning TEXT,
                    parsed_at     TEXT NOT NULL,
                    UNIQUE(user_id, filepath)
                )
            """)
            # Defensive: ensure recon_warning exists even if a transitional DB
            # has the new id PK but predates this column.
            try:
                conn.execute("ALTER TABLE file_sources ADD COLUMN recon_warning TEXT")
            except sqlite3.OperationalError:
                pass

            # Multi-tenant migration: older file_sources rows have no user_id and
            # a global UNIQUE(filepath). Rebuild to add user_id + scope uniqueness
            # per tenant so two users can ingest a file at the same relative path.
            # Backfill user_id from the transactions each file produced.
            fs_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(file_sources)")}
            if fs_cols2 and "user_id" not in fs_cols2:
                conn.execute("ALTER TABLE file_sources RENAME TO file_sources_old")
                conn.execute("""
                    CREATE TABLE file_sources (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id       TEXT NOT NULL DEFAULT '',
                        filename      TEXT NOT NULL,
                        filepath      TEXT NOT NULL,
                        file_mtime    REAL NOT NULL,
                        parse_method  TEXT NOT NULL,
                        tx_count      INTEGER NOT NULL,
                        parse_error   TEXT,
                        recon_warning TEXT,
                        parsed_at     TEXT NOT NULL,
                        UNIQUE(user_id, filepath)
                    )
                """)
                conn.execute("""
                    INSERT INTO file_sources
                      (id, user_id, filename, filepath, file_mtime, parse_method,
                       tx_count, parse_error, recon_warning, parsed_at)
                    SELECT o.id,
                           COALESCE((SELECT t.user_id FROM transactions t
                                     WHERE t.source_file = o.filename AND t.user_id != ''
                                     LIMIT 1), ''),
                           o.filename, o.filepath, o.file_mtime, o.parse_method,
                           o.tx_count, o.parse_error, o.recon_warning, o.parsed_at
                    FROM file_sources_old o
                """)
                conn.execute("DROP TABLE file_sources_old")

            # Reconciliation layer (see design/storage.md). transactions_recon is
            # a materialized, per-user, 1:1 derivation of `transactions` —
            # corrected flow_type, paired-transfer flags, dedup flag, signed
            # amount. It is a pure function of raw and fully rebuildable, so the
            # reconciler owns its contents; storage only owns the shape.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transactions_recon (
                    id                   TEXT PRIMARY KEY,
                    user_id              TEXT NOT NULL DEFAULT '',
                    flow_type_recon      TEXT NOT NULL DEFAULT 'unknown',
                    signed_amount        REAL NOT NULL DEFAULT 0,
                    is_internal_transfer INTEGER NOT NULL DEFAULT 0,
                    transfer_group_id    TEXT,
                    is_duplicate         INTEGER NOT NULL DEFAULT 0,
                    reconciled_at        TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recon_user ON transactions_recon(user_id)")

            # User overlay (Phase 1C — see design/overrides.md). Three tables:
            # - transaction_overrides: per-tx user corrections (manual or
            #   rule-materialized). One row per (user_id, transaction_id).
            # - category_rules: patterns the user wants applied to all matching
            #   rows, past and future.
            # - override_audit: append-only history of every override / rule
            #   mutation, with the chat session that produced it.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transaction_overrides (
                    user_id        TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    category       TEXT,
                    flow_type      TEXT,
                    is_excluded    INTEGER,
                    source_kind    TEXT NOT NULL DEFAULT 'user_manual',
                    source_rule_id INTEGER,
                    note           TEXT NOT NULL DEFAULT '',
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL,
                    PRIMARY KEY (user_id, transaction_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_overrides_rule "
                "ON transaction_overrides(source_rule_id)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS category_rules (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id            TEXT NOT NULL,
                    match_type         TEXT NOT NULL,
                    match_value        TEXT NOT NULL,
                    target_category    TEXT,
                    target_flow_type   TEXT,
                    target_is_excluded INTEGER,
                    priority           INTEGER NOT NULL DEFAULT 100,
                    active             INTEGER NOT NULL DEFAULT 1,
                    note               TEXT NOT NULL DEFAULT '',
                    created_at         TEXT NOT NULL,
                    updated_at         TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rules_user "
                "ON category_rules(user_id, active)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS override_audit (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id          TEXT NOT NULL,
                    at               TEXT NOT NULL,
                    action           TEXT NOT NULL,
                    transaction_id   TEXT,
                    rule_id          INTEGER,
                    before_json      TEXT NOT NULL DEFAULT 'null',
                    after_json       TEXT NOT NULL DEFAULT 'null',
                    chat_session_id  TEXT,
                    chat_message_id  TEXT,
                    note             TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_user_at "
                "ON override_audit(user_id, at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_tx "
                "ON override_audit(user_id, transaction_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_rule "
                "ON override_audit(user_id, rule_id)"
            )

            # The view every dashboard / chat / analysis read goes through: raw
            # columns + the recon overlay + the user overlay. LEFT JOINs +
            # COALESCE preserve precedence: explicit override > rule override >
            # system recon > raw. See design/overrides.md.
            conn.execute("DROP VIEW IF EXISTS v_transactions_recon")
            conn.execute("DROP VIEW IF EXISTS v_transactions_effective")
            conn.execute("""
                CREATE VIEW v_transactions_effective AS
                SELECT t.id,
                    t.date,
                    t.amount,
                    t.description,
                    COALESCE(o.category, t.category)                      AS category,
                    t.account_type,
                    t.account_id,
                    t.user_id,
                    t.section_type,
                    t.source,
                    t.notes,
                    COALESCE(o.flow_type, r.flow_type_recon, t.flow_type) AS flow_type_recon,
                    COALESCE(r.signed_amount, 0)                          AS signed_amount,
                    COALESCE(r.is_internal_transfer, 0)                   AS is_internal_transfer,
                    r.transfer_group_id                                   AS transfer_group_id,
                    COALESCE(r.is_duplicate, 0)                           AS is_duplicate,
                    COALESCE(o.is_excluded, 0)                            AS is_user_excluded,
                    o.source_kind                                         AS override_source,
                    o.source_rule_id                                      AS override_rule_id,
                    o.note                                                AS override_note
                FROM transactions t
                LEFT JOIN transactions_recon    r ON r.id = t.id
                LEFT JOIN transaction_overrides o
                       ON o.user_id = t.user_id AND o.transaction_id = t.id
            """)

            conn.commit()

    @staticmethod
    def dedup_hash(account_id: str, date: str, amount: float, description: str) -> str:
        """Content hash used by the query layer to collapse duplicate records.

        Storage never drops on this; it only stores it.
        """
        norm_desc = " ".join(description.lower().split())
        return hashlib.md5(
            f"{account_id}|{date}|{amount}|{norm_desc}".encode()
        ).hexdigest()[:16]

    @classmethod
    def _row_tuple(cls, t, now: str) -> tuple:
        return (
            t.id, t.date, t.amount, t.description, t.category, t.account_type,
            t.source_file, t.user_id, t.account_id, t.source,
            cls.dedup_hash(t.account_id, t.date, t.amount, t.description),
            getattr(t, "flow_type", "unknown") or "unknown",
            getattr(t, "notes", "") or "",
            getattr(t, "section_type", "") or "",
            now,
        )

    @classmethod
    def upsert_transactions(cls, transactions) -> None:
        """Insert/replace transactions by primary key (used by the Plaid path)."""
        if not transactions:
            return
        now = datetime.now().isoformat()
        cls.init_db()
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.executemany(cls._INSERT_SQL, [cls._row_tuple(t, now) for t in transactions])
            conn.commit()

    @classmethod
    def replace_file_transactions(cls, source_file: str, transactions) -> None:
        """Delete every row for a statement file, then insert the freshly-parsed set.

        Idempotent re-ingestion that keeps all parsed rows — including legitimate
        same-day / same-amount duplicates within one statement. The caller is
        responsible for only invoking this on a *successful* parse; an empty
        list here means "the statement legitimately has no transactions" and the
        existing rows are cleared. Parse failures must not reach this method —
        gate at the call site so stale data isn't accidentally wiped.
        """
        now = datetime.now().isoformat()
        cls.init_db()
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.execute("DELETE FROM transactions WHERE source_file = ?", (source_file,))
            if transactions:
                conn.executemany(cls._INSERT_SQL, [cls._row_tuple(t, now) for t in transactions])
            conn.commit()

    @classmethod
    def upsert_account(cls, account) -> None:
        """Insert or update an account, preserving the original created_at."""
        cls.init_db()
        with sqlite3.connect(cls.DB_PATH) as conn:
            existing = conn.execute(
                "SELECT created_at FROM accounts WHERE id = ?", (account.id,)
            ).fetchone()
            created = existing[0] if existing else (account.created_at or datetime.now().isoformat())
            conn.execute(
                "INSERT OR REPLACE INTO accounts "
                "(id, user_id, bank, name, mask, type, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account.id, account.user_id, account.bank, account.name,
                 account.mask, account.type, account.source, created),
            )
            conn.commit()

    @classmethod
    def get_account(cls, account_id: str):
        from src.models import Account
        if not cls.DB_PATH.exists():
            return None
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if not row:
            return None
        return Account(
            id=row["id"], user_id=row["user_id"], bank=row["bank"], name=row["name"],
            mask=row["mask"], type=row["type"], source=row["source"],
            created_at=row["created_at"],
        )

    @classmethod
    def query_accounts(cls) -> list:
        """Return all accounts with a per-account transaction count, as dicts."""
        if not cls.DB_PATH.exists():
            return []
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT a.*, COUNT(t.id) AS tx_count
                FROM accounts a
                LEFT JOIN transactions t ON t.account_id = a.id
                GROUP BY a.id
                ORDER BY a.type, a.mask
            """).fetchall()
        return [dict(r) for r in rows]

    @classmethod
    def upsert_file_source(cls, user_id: str, filename: str, filepath: str, mtime: float,
                           method: str, count: int, error: Optional[str] = None,
                           recon_warning: Optional[str] = None) -> None:
        """Insert or update the file_sources row keyed on (user_id, filepath).

        Using UPSERT (rather than INSERT OR REPLACE) preserves the row's `id`
        across re-ingestions so future foreign keys remain stable. Scoped per
        user so two tenants can ingest a file at the same relative path.
        """
        cls.init_db()
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO file_sources "
                "(user_id, filename, filepath, file_mtime, parse_method, tx_count, "
                "parse_error, recon_warning, parsed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, filepath) DO UPDATE SET "
                "  filename      = excluded.filename, "
                "  file_mtime    = excluded.file_mtime, "
                "  parse_method  = excluded.parse_method, "
                "  tx_count      = excluded.tx_count, "
                "  parse_error   = excluded.parse_error, "
                "  recon_warning = excluded.recon_warning, "
                "  parsed_at     = excluded.parsed_at",
                (user_id, filename, filepath, mtime, method, count,
                 error, recon_warning, datetime.now().isoformat()),
            )
            conn.commit()

    @classmethod
    def get_file_source(cls, user_id: str, filepath: str) -> Optional[dict]:
        """Look up by (user_id, filepath) — scoped per tenant."""
        if not cls.DB_PATH.exists():
            return None
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM file_sources WHERE user_id = ? AND filepath = ?",
                (user_id, filepath),
            ).fetchone()
        return dict(row) if row else None

    @classmethod
    def query_all(cls) -> list:
        """Return all transactions sorted by date descending as List[Transaction]."""
        from src.models import Transaction
        if not cls.DB_PATH.exists():
            return []
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM transactions ORDER BY date DESC"
            ).fetchall()
        return [
            Transaction(
                id=r["id"], date=r["date"], amount=r["amount"],
                description=r["description"], category=r["category"],
                account_type=r["account_type"], source_file=r["source_file"],
                user_id=r["user_id"], account_id=r["account_id"], source=r["source"],
                flow_type=r["flow_type"] if "flow_type" in r.keys() else "unknown",
                notes=r["notes"] if "notes" in r.keys() else "",
                section_type=r["section_type"] if "section_type" in r.keys() else "",
            )
            for r in rows
        ]

    @classmethod
    def get_parse_errors(cls, user_id: str) -> list:
        """Return this user's file_sources entries with a parse_error or recon_warning."""
        if not cls.DB_PATH.exists():
            return []
        with sqlite3.connect(cls.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT filename, parse_error, recon_warning, parsed_at FROM file_sources "
                "WHERE user_id = ? AND (parse_error IS NOT NULL OR recon_warning IS NOT NULL)",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


class ChartAnnotationStore:
    """Cache for LLM-generated dashboard annotations, keyed on (chart_key, period_key).

    The companion regenerates a chart's insight only when the underlying payload
    hash changes (new ingestion) or the user explicitly requests a refresh —
    see design/ui_dashboard.md §5.
    """

    DB_PATH = TransactionStore.DB_PATH

    @classmethod
    def _connect(cls) -> sqlite3.Connection:
        # Reuse TransactionStore's DB so a single file backs all SQL state.
        # Re-resolve at call time so monkeypatched DB_PATH (tests) is honored.
        path = TransactionStore.DB_PATH
        path.parent.mkdir(exist_ok=True)
        return sqlite3.connect(path)

    @classmethod
    def init_db(cls) -> None:
        # TransactionStore.init_db creates the parent directory and other tables.
        TransactionStore.init_db()
        with cls._connect() as conn:
            # chart_annotations is a regenerable cache, so a schema bump can just
            # drop and recreate — annotations rebuild lazily on next view. The
            # cache is per-user: the key includes user_id so two tenants never
            # share or overwrite each other's insights.
            ca_cols = {r[1] for r in conn.execute("PRAGMA table_info(chart_annotations)")}
            if ca_cols and "user_id" not in ca_cols:
                conn.execute("DROP TABLE chart_annotations")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chart_annotations (
                    user_id         TEXT NOT NULL DEFAULT '',
                    chart_key       TEXT NOT NULL,
                    period_key      TEXT NOT NULL,
                    payload_hash    TEXT NOT NULL,
                    annotation_text TEXT NOT NULL,
                    suggestions     TEXT NOT NULL DEFAULT '[]',
                    generated_at    TEXT NOT NULL,
                    PRIMARY KEY (user_id, chart_key, period_key)
                )
            """)
            # pinned_charts is empty in 1B (Phase 1C writes it), so an additive
            # column is enough; backfill old rows to '' if any slipped in.
            pc_cols = {r[1] for r in conn.execute("PRAGMA table_info(pinned_charts)")}
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pinned_charts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT NOT NULL DEFAULT '',
                    name       TEXT NOT NULL DEFAULT '',
                    spec_json  TEXT NOT NULL DEFAULT '{}',
                    pinned_at  TEXT NOT NULL
                )
            """)
            if pc_cols and "user_id" not in pc_cols:
                conn.execute("ALTER TABLE pinned_charts ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pinned_user ON pinned_charts(user_id)")
            conn.commit()

    @classmethod
    def get(cls, user_id: str, chart_key: str, period_key: str) -> Optional[dict]:
        cls.init_db()
        with cls._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT user_id, chart_key, period_key, payload_hash, annotation_text, "
                "suggestions, generated_at FROM chart_annotations "
                "WHERE user_id = ? AND chart_key = ? AND period_key = ?",
                (user_id, chart_key, period_key),
            ).fetchone()
        if not row:
            return None
        try:
            suggestions = json.loads(row["suggestions"] or "[]")
            if not isinstance(suggestions, list):
                suggestions = []
        except json.JSONDecodeError:
            suggestions = []
        return {
            "user_id": row["user_id"],
            "chart_key": row["chart_key"],
            "period_key": row["period_key"],
            "payload_hash": row["payload_hash"],
            "annotation_text": row["annotation_text"],
            "suggestions": suggestions,
            "generated_at": row["generated_at"],
        }

    @classmethod
    def upsert(
        cls,
        user_id: str,
        chart_key: str,
        period_key: str,
        payload_hash: str,
        annotation_text: str,
        suggestions: List[str],
    ) -> None:
        cls.init_db()
        suggestions_json = json.dumps(list(suggestions or []))
        now = datetime.now().isoformat()
        with cls._connect() as conn:
            conn.execute(
                "INSERT INTO chart_annotations "
                "(user_id, chart_key, period_key, payload_hash, annotation_text, suggestions, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, chart_key, period_key) DO UPDATE SET "
                "  payload_hash    = excluded.payload_hash, "
                "  annotation_text = excluded.annotation_text, "
                "  suggestions     = excluded.suggestions, "
                "  generated_at    = excluded.generated_at",
                (user_id, chart_key, period_key, payload_hash, annotation_text, suggestions_json, now),
            )
            conn.commit()

    @classmethod
    def delete(cls, user_id: str, chart_key: str, period_key: str) -> None:
        cls.init_db()
        with cls._connect() as conn:
            conn.execute(
                "DELETE FROM chart_annotations WHERE user_id = ? AND chart_key = ? AND period_key = ?",
                (user_id, chart_key, period_key),
            )
            conn.commit()


class PinnedChartStore:
    """Reserved for Phase 1C — chart pinning from chat. Read-only stub in 1B."""

    @classmethod
    def init_db(cls) -> None:
        # Table is created alongside chart_annotations.
        ChartAnnotationStore.init_db()

    @classmethod
    def list_for_user(cls, user_id: str) -> list:
        cls.init_db()
        path = TransactionStore.DB_PATH
        if not path.exists():
            return []
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, user_id, name, spec_json, pinned_at FROM pinned_charts "
                "WHERE user_id = ? ORDER BY pinned_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


class WikiStore:
    PATH = DATA_DIR / "user_wiki.md"

    @classmethod
    def load(cls) -> str:
        if not cls.PATH.exists():
            return ""
        return cls.PATH.read_text(encoding="utf-8")

    @classmethod
    def save(cls, content: str) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        cls.PATH.write_text(content, encoding="utf-8")

    @classmethod
    def exists(cls) -> bool:
        return cls.PATH.exists()


# --- User overlay: per-tx overrides + audit (Phase 1C) -----------------------
# See design/overrides.md for the full contract. All mutations write a row
# into `override_audit` inside the same DB transaction so the log never
# drifts from the data.


# Closed enum — what kind of override row this is.
SOURCE_KIND_USER_MANUAL = "user_manual"
SOURCE_KIND_RULE = "rule"

# Closed enum — audit action values. See design/overrides.md → Audit action.
AUDIT_SET_OVERRIDE       = "set_override"
AUDIT_CLEAR_OVERRIDE     = "clear_override"
AUDIT_CREATE_RULE        = "create_rule"
AUDIT_EDIT_RULE          = "edit_rule"
AUDIT_DELETE_RULE        = "delete_rule"
AUDIT_PAUSE_RULE         = "pause_rule"
AUDIT_RESUME_RULE        = "resume_rule"
AUDIT_RULE_MATERIALIZE   = "rule_materialize"
AUDIT_RULE_UNMATERIALIZE = "rule_unmaterialize"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _override_row_to_dict(row) -> dict:
    return {
        "user_id":        row["user_id"],
        "transaction_id": row["transaction_id"],
        "category":       row["category"],
        "flow_type":      row["flow_type"],
        "is_excluded":    row["is_excluded"],
        "source_kind":    row["source_kind"],
        "source_rule_id": row["source_rule_id"],
        "note":           row["note"],
        "created_at":     row["created_at"],
        "updated_at":     row["updated_at"],
    }


class AuditStore:
    """Append-only history. Writers pass an open sqlite3 connection so the
    audit insert lands in the same transaction as the override mutation."""

    @staticmethod
    def append_conn(
        conn: sqlite3.Connection,
        user_id: str,
        action: str,
        *,
        transaction_id: Optional[str] = None,
        rule_id: Optional[int] = None,
        before: Any = None,
        after: Any = None,
        chat_session_id: Optional[str] = None,
        chat_message_id: Optional[str] = None,
        note: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO override_audit "
            "(user_id, at, action, transaction_id, rule_id, "
            "before_json, after_json, chat_session_id, chat_message_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, _now_iso(), action, transaction_id, rule_id,
                json.dumps(before, default=str), json.dumps(after, default=str),
                chat_session_id, chat_message_id, note,
            ),
        )

    @classmethod
    def list_events(
        cls,
        user_id: str,
        *,
        transaction_id: Optional[str] = None,
        rule_id: Optional[int] = None,
        since: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        TransactionStore.init_db()
        sql = (
            "SELECT id, user_id, at, action, transaction_id, rule_id, "
            "before_json, after_json, chat_session_id, chat_message_id, note "
            "FROM override_audit WHERE user_id = ?"
        )
        params: list = [user_id]
        if transaction_id is not None:
            sql += " AND transaction_id = ?"
            params.append(transaction_id)
        if rule_id is not None:
            sql += " AND rule_id = ?"
            params.append(rule_id)
        if since:
            sql += " AND at >= ?"
            params.append(since)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        out: List[dict] = []
        for r in rows:
            try:
                before = json.loads(r["before_json"])
            except Exception:
                before = None
            try:
                after = json.loads(r["after_json"])
            except Exception:
                after = None
            out.append({
                "id":               r["id"],
                "user_id":          r["user_id"],
                "at":               r["at"],
                "action":           r["action"],
                "transaction_id":   r["transaction_id"],
                "rule_id":          r["rule_id"],
                "before":           before,
                "after":            after,
                "chat_session_id":  r["chat_session_id"],
                "chat_message_id":  r["chat_message_id"],
                "note":             r["note"],
            })
        return out


class OverrideStore:
    """Per-transaction overrides — manual and rule-materialized share one
    table. `source_kind` distinguishes; manual always wins on upsert."""

    @classmethod
    def _read(cls, conn: sqlite3.Connection, user_id: str, transaction_id: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT user_id, transaction_id, category, flow_type, is_excluded, "
            "source_kind, source_rule_id, note, created_at, updated_at "
            "FROM transaction_overrides WHERE user_id = ? AND transaction_id = ?",
            (user_id, transaction_id),
        ).fetchone()
        return _override_row_to_dict(row) if row else None

    @classmethod
    def get(cls, user_id: str, transaction_id: str) -> Optional[dict]:
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return cls._read(conn, user_id, transaction_id)

    @classmethod
    def set_override(
        cls,
        user_id: str,
        transaction_id: str,
        *,
        category: Optional[str] = None,
        flow_type: Optional[str] = None,
        is_excluded: Optional[int] = None,
        note: str = "",
        chat_session_id: Optional[str] = None,
        chat_message_id: Optional[str] = None,
    ) -> dict:
        """Upsert a manual override. Replaces any existing row (including a
        rule-materialized one) at this PK. At least one of category /
        flow_type / is_excluded must be non-None.
        """
        if category is None and flow_type is None and is_excluded is None:
            raise ValueError(
                "set_override needs at least one of category / flow_type / is_excluded"
            )
        TransactionStore.init_db()
        now = _now_iso()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            before = cls._read(conn, user_id, transaction_id)
            created_at = before["created_at"] if before else now
            conn.execute(
                "INSERT INTO transaction_overrides "
                "(user_id, transaction_id, category, flow_type, is_excluded, "
                "source_kind, source_rule_id, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?) "
                "ON CONFLICT(user_id, transaction_id) DO UPDATE SET "
                "  category       = excluded.category, "
                "  flow_type      = excluded.flow_type, "
                "  is_excluded    = excluded.is_excluded, "
                "  source_kind    = excluded.source_kind, "
                "  source_rule_id = NULL, "
                "  note           = excluded.note, "
                "  updated_at     = excluded.updated_at",
                (
                    user_id, transaction_id, category, flow_type,
                    int(is_excluded) if is_excluded is not None else None,
                    SOURCE_KIND_USER_MANUAL, note, created_at, now,
                ),
            )
            after = cls._read(conn, user_id, transaction_id)
            AuditStore.append_conn(
                conn, user_id, AUDIT_SET_OVERRIDE,
                transaction_id=transaction_id,
                before=before, after=after,
                chat_session_id=chat_session_id,
                chat_message_id=chat_message_id,
            )
            conn.commit()
            return after or {}

    @classmethod
    def clear_override(
        cls,
        user_id: str,
        transaction_id: str,
        *,
        chat_session_id: Optional[str] = None,
        chat_message_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Remove any override at this PK (manual or rule). Returns the row
        that was deleted, or None if there was none.
        """
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            before = cls._read(conn, user_id, transaction_id)
            if before is None:
                return None
            conn.execute(
                "DELETE FROM transaction_overrides "
                "WHERE user_id = ? AND transaction_id = ?",
                (user_id, transaction_id),
            )
            AuditStore.append_conn(
                conn, user_id, AUDIT_CLEAR_OVERRIDE,
                transaction_id=transaction_id,
                before=before, after=None,
                chat_session_id=chat_session_id,
                chat_message_id=chat_message_id,
            )
            conn.commit()
            return before

    @classmethod
    def list_overrides(
        cls,
        user_id: str,
        *,
        transaction_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """Return overrides for this user (most recently updated first)."""
        TransactionStore.init_db()
        sql = (
            "SELECT user_id, transaction_id, category, flow_type, is_excluded, "
            "source_kind, source_rule_id, note, created_at, updated_at "
            "FROM transaction_overrides WHERE user_id = ?"
        )
        params: list = [user_id]
        if transaction_id is not None:
            sql += " AND transaction_id = ?"
            params.append(transaction_id)
        if since:
            sql += " AND updated_at >= ?"
            params.append(since)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [_override_row_to_dict(r) for r in rows]


# --- Rule storage ------------------------------------------------------------


VALID_MATCH_TYPES = ("description_exact", "description_substring", "merchant_canonical")


def _rule_row_to_dict(row) -> dict:
    return {
        "id":                 row["id"],
        "user_id":            row["user_id"],
        "match_type":         row["match_type"],
        "match_value":        row["match_value"],
        "target_category":    row["target_category"],
        "target_flow_type":   row["target_flow_type"],
        "target_is_excluded": row["target_is_excluded"],
        "priority":           row["priority"],
        "active":             row["active"],
        "note":               row["note"],
        "created_at":         row["created_at"],
        "updated_at":         row["updated_at"],
    }


class RuleStore:
    """CRUD on category_rules. Materialization (turning a rule into per-tx
    override rows) lives in src/overrides.py — it needs the matcher and the
    raw rows. RuleStore only owns the rule rows."""

    @classmethod
    def _validate_targets(cls, target_category, target_flow_type, target_is_excluded) -> None:
        if (target_category is None
                and target_flow_type is None
                and target_is_excluded is None):
            raise ValueError(
                "rule must set at least one of target_category / "
                "target_flow_type / target_is_excluded"
            )

    @classmethod
    def _validate_match(cls, match_type, match_value) -> None:
        if match_type not in VALID_MATCH_TYPES:
            raise ValueError(
                f"match_type must be one of {VALID_MATCH_TYPES}, got {match_type!r}"
            )
        if not (match_value and match_value.strip()):
            raise ValueError("match_value must be non-empty")

    @classmethod
    def get(cls, user_id: str, rule_id: int) -> Optional[dict]:
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, user_id, match_type, match_value, target_category, "
                "target_flow_type, target_is_excluded, priority, active, note, "
                "created_at, updated_at "
                "FROM category_rules WHERE user_id = ? AND id = ?",
                (user_id, rule_id),
            ).fetchone()
        return _rule_row_to_dict(row) if row else None

    @classmethod
    def list_rules(
        cls,
        user_id: str,
        *,
        active_only: bool = False,
    ) -> List[dict]:
        TransactionStore.init_db()
        sql = (
            "SELECT id, user_id, match_type, match_value, target_category, "
            "target_flow_type, target_is_excluded, priority, active, note, "
            "created_at, updated_at "
            "FROM category_rules WHERE user_id = ?"
        )
        params: list = [user_id]
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY priority DESC, id ASC"
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [_rule_row_to_dict(r) for r in rows]

    @classmethod
    def insert(
        cls,
        user_id: str,
        *,
        match_type: str,
        match_value: str,
        target_category: Optional[str] = None,
        target_flow_type: Optional[str] = None,
        target_is_excluded: Optional[int] = None,
        priority: int = 100,
        note: str = "",
    ) -> int:
        """Insert a new rule row. Returns the new rule_id.

        Audit + materialization are the caller's responsibility — they live
        in src/overrides.py because they touch transactions/overrides too.
        """
        cls._validate_match(match_type, match_value)
        cls._validate_targets(target_category, target_flow_type, target_is_excluded)
        TransactionStore.init_db()
        now = _now_iso()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO category_rules "
                "(user_id, match_type, match_value, target_category, "
                "target_flow_type, target_is_excluded, priority, active, note, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    user_id, match_type, match_value.strip(),
                    target_category, target_flow_type,
                    int(target_is_excluded) if target_is_excluded is not None else None,
                    int(priority), note, now, now,
                ),
            )
            conn.commit()
            return cur.lastrowid

    @classmethod
    def delete(cls, user_id: str, rule_id: int) -> Optional[dict]:
        """Delete a rule row. Returns the row that was deleted, or None."""
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT id, user_id, match_type, match_value, target_category, "
                "target_flow_type, target_is_excluded, priority, active, note, "
                "created_at, updated_at "
                "FROM category_rules WHERE user_id = ? AND id = ?",
                (user_id, rule_id),
            ).fetchone()
            if existing is None:
                return None
            conn.execute(
                "DELETE FROM category_rules WHERE user_id = ? AND id = ?",
                (user_id, rule_id),
            )
            conn.commit()
            return _rule_row_to_dict(existing)

    @classmethod
    def set_active(cls, user_id: str, rule_id: int, active: bool) -> Optional[dict]:
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            conn.execute(
                "UPDATE category_rules SET active = ?, updated_at = ? "
                "WHERE user_id = ? AND id = ?",
                (1 if active else 0, _now_iso(), user_id, rule_id),
            )
            conn.commit()
        return cls.get(user_id, rule_id)
