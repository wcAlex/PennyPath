import json
import pytest
from src.storage import UserConfigStore, ConversationStore, SnapshotStore, UserConfig


class TestUserConfigStore:
    def test_load_returns_default_when_file_missing(self):
        cfg = UserConfigStore.load()
        assert isinstance(cfg, UserConfig)
        assert cfg.name == ""
        assert cfg.onboarding_complete is False

    def test_save_and_load_roundtrip(self, tmp_data):
        cfg = UserConfig(
            name="Alex",
            finance_profile="early_career",
            goal_type="emergency_fund",
            goal_label="Emergency fund",
            goal_monthly_target=500.0,
            intentions=["spend less on dining"],
            onboarding_complete=True,
        )
        UserConfigStore.save(cfg)
        loaded = UserConfigStore.load()
        assert loaded.name == "Alex"
        assert loaded.finance_profile == "early_career"
        assert loaded.goal_type == "emergency_fund"
        assert loaded.goal_label == "Emergency fund"
        assert loaded.goal_monthly_target == 500.0
        assert loaded.intentions == ["spend less on dining"]
        assert loaded.onboarding_complete is True

    def test_is_complete_false_by_default(self):
        assert UserConfigStore.is_complete() is False

    def test_is_complete_true_after_save_with_complete(self, tmp_data):
        cfg = UserConfig(name="Alex", onboarding_complete=True)
        UserConfigStore.save(cfg)
        assert UserConfigStore.is_complete() is True

    def test_migration_from_legacy_user_prefs(self, tmp_data):
        """If user_prefs.json exists but config.json doesn't, load() should migrate."""
        import src.storage as storage_module
        legacy_path = tmp_data / "user_prefs.json"
        # Also patch the legacy path so it looks in tmp_data
        from src.storage import UserConfigStore
        old_legacy = UserConfigStore._LEGACY_PATH
        try:
            UserConfigStore._LEGACY_PATH = legacy_path
            legacy_data = {
                "name": "Jordan",
                "saving_goal": {
                    "label": "Vacation fund",
                    "monthly_target": 200.0,
                },
                "intentions": ["eat out less"],
            }
            legacy_path.write_text(json.dumps(legacy_data))
            cfg = UserConfigStore.load()
            assert cfg.name == "Jordan"
            assert cfg.goal_label == "Vacation fund"
        finally:
            UserConfigStore._LEGACY_PATH = old_legacy


class TestConversationStore:
    def test_load_returns_empty_when_file_missing(self):
        result = ConversationStore.load()
        assert result == []

    def test_append_adds_entries(self, tmp_data):
        ConversationStore.append("user", "Hello!")
        ConversationStore.append("assistant", "Hi there!")
        history = ConversationStore.load()
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello!"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "Hi there!"

    def test_load_max_turns_limits_entries(self, tmp_data):
        # Append 6 messages (3 user + 3 assistant, alternating)
        for i in range(6):
            role = "user" if i % 2 == 0 else "assistant"
            ConversationStore.append(role, f"message {i}")
        # max_turns=2 means we get at most 2*2=4 entries
        history = ConversationStore.load(max_turns=2)
        assert len(history) == 4

    def test_clear_wipes_history(self, tmp_data):
        ConversationStore.append("user", "Hello!")
        ConversationStore.append("assistant", "Hi!")
        ConversationStore.clear()
        history = ConversationStore.load()
        assert history == []


class TestSnapshotStore:
    def test_save_and_load_recent(self, tmp_data):
        SnapshotStore.save(
            period="2024-04",
            category_totals={"dining": 73.50, "groceries": 89.00},
            total_spend=162.50,
            transaction_count=3,
        )
        recent = SnapshotStore.load_recent(n=1)
        assert len(recent) == 1
        assert recent[0]["period"] == "2024-04"
        assert recent[0]["total_spend"] == 162.50
        assert recent[0]["transaction_count"] == 3

    def test_load_period_returns_correct_entry(self, tmp_data):
        SnapshotStore.save("2024-04", {"dining": 50.0}, 50.0, 1)
        SnapshotStore.save("2024-03", {"groceries": 80.0}, 80.0, 2)
        entry = SnapshotStore.load_period("2024-03")
        assert entry is not None
        assert entry["period"] == "2024-03"
        assert entry["total_spend"] == 80.0

    def test_load_period_returns_none_for_missing(self):
        result = SnapshotStore.load_period("1999-01")
        assert result is None

    def test_multiple_saves_load_recent_sorted_desc(self, tmp_data):
        SnapshotStore.save("2024-03", {"dining": 50.0}, 50.0, 1)
        SnapshotStore.save("2024-04", {"dining": 75.0}, 75.0, 2)
        SnapshotStore.save("2024-05", {"dining": 100.0}, 100.0, 3)
        recent = SnapshotStore.load_recent(n=2)
        assert len(recent) == 2
        assert recent[0]["period"] == "2024-05"
        assert recent[1]["period"] == "2024-04"


class TestTransactionStore:
    def test_init_db_creates_tables(self, tmp_data):
        from src.storage import TransactionStore
        TransactionStore.init_db()
        import sqlite3
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "transactions" in tables
        assert "file_sources" in tables

    def test_upsert_and_query(self, tmp_data, sample_transactions):
        from src.storage import TransactionStore
        TransactionStore.upsert_transactions(sample_transactions)
        result = TransactionStore.query_all()
        assert len(result) == 5
        ids = {t.id for t in result}
        assert ids == {t.id for t in sample_transactions}

    def test_deduplication_via_primary_key(self, tmp_data, sample_transactions):
        from src.storage import TransactionStore
        TransactionStore.upsert_transactions(sample_transactions)
        TransactionStore.upsert_transactions(sample_transactions)
        result = TransactionStore.query_all()
        assert len(result) == 5

    def test_query_all_sorted_descending(self, tmp_data, sample_transactions):
        from src.storage import TransactionStore
        TransactionStore.upsert_transactions(sample_transactions)
        result = TransactionStore.query_all()
        dates = [t.date for t in result]
        assert dates == sorted(dates, reverse=True)

    def test_file_source_tracking(self, tmp_data):
        from src.storage import TransactionStore
        TransactionStore.upsert_file_source("u1", "test.csv", "data/statements/test.csv", 1234567.0, "csv", 5)
        entry = TransactionStore.get_file_source("u1", "data/statements/test.csv")
        assert entry is not None
        assert entry["filename"] == "test.csv"
        assert entry["tx_count"] == 5
        assert entry["parse_error"] is None

    def test_get_file_source_returns_none_when_missing(self, tmp_data):
        from src.storage import TransactionStore
        assert TransactionStore.get_file_source("u1", "nonexistent.csv") is None

    def test_get_parse_errors(self, tmp_data):
        from src.storage import TransactionStore
        TransactionStore.upsert_file_source("u1", "broken.pdf", "data/statements/broken.pdf", 999.0, "llm", 0, "LLM returned invalid JSON")
        TransactionStore.upsert_file_source("u1", "ok.csv", "data/statements/ok.csv", 111.0, "csv", 3, None)
        errors = TransactionStore.get_parse_errors("u1")
        assert len(errors) == 1
        assert errors[0]["filename"] == "broken.pdf"

    def test_file_sources_isolated_per_user(self, tmp_data):
        """Two tenants can ingest the same relative path without colliding, and
        each only sees their own parse errors."""
        from src.storage import TransactionStore
        same_path = "data/statements/statement.pdf"
        TransactionStore.upsert_file_source("u1", "statement.pdf", same_path, 1.0, "llm", 10, None)
        TransactionStore.upsert_file_source("u2", "statement.pdf", same_path, 2.0, "llm", 0, "u2 parse failed")

        # Same filepath, different users → two independent rows.
        e1 = TransactionStore.get_file_source("u1", same_path)
        e2 = TransactionStore.get_file_source("u2", same_path)
        assert e1["tx_count"] == 10 and e1["parse_error"] is None
        assert e2["tx_count"] == 0 and e2["parse_error"] == "u2 parse failed"

        # Parse errors are scoped per user.
        assert TransactionStore.get_parse_errors("u1") == []
        assert len(TransactionStore.get_parse_errors("u2")) == 1

    def test_replace_file_transactions_with_empty_clears_existing(self, tmp_data):
        """A legitimate empty re-parse must delete the prior rows for that file —
        otherwise stale rows survive when a statement is intentionally cleared."""
        from src.models import Transaction
        from src.storage import TransactionStore

        initial = [
            Transaction(id="r1", date="2024-04-01", amount=10.0, description="A",
                        category="", account_type="credit", source_file="x.pdf"),
            Transaction(id="r2", date="2024-04-02", amount=20.0, description="B",
                        category="", account_type="credit", source_file="x.pdf"),
        ]
        TransactionStore.replace_file_transactions("x.pdf", initial)
        assert len(TransactionStore.query_all()) == 2

        TransactionStore.replace_file_transactions("x.pdf", [])
        assert TransactionStore.query_all() == []

    def test_replace_file_transactions_scoped_to_source_file(self, tmp_data):
        """Replacing one file's rows must not touch another file's rows."""
        from src.models import Transaction
        from src.storage import TransactionStore

        TransactionStore.replace_file_transactions("a.pdf", [
            Transaction(id="a1", date="2024-04-01", amount=1.0, description="A",
                        category="", account_type="credit", source_file="a.pdf"),
        ])
        TransactionStore.replace_file_transactions("b.pdf", [
            Transaction(id="b1", date="2024-04-01", amount=2.0, description="B",
                        category="", account_type="credit", source_file="b.pdf"),
        ])
        # Clear a.pdf only
        TransactionStore.replace_file_transactions("a.pdf", [])
        remaining = TransactionStore.query_all()
        assert [t.id for t in remaining] == ["b1"]


class TestWikiStore:
    def test_load_returns_empty_when_missing(self, tmp_data):
        from src.storage import WikiStore
        assert WikiStore.load() == ""

    def test_exists_false_when_missing(self, tmp_data):
        from src.storage import WikiStore
        assert WikiStore.exists() is False

    def test_save_and_load_roundtrip(self, tmp_data):
        from src.storage import WikiStore
        content = "## Identity\nAlex. Early career.\n\n## Goal\nEmergency fund.\n"
        WikiStore.save(content)
        assert WikiStore.exists() is True
        assert WikiStore.load() == content

    def test_save_overwrites(self, tmp_data):
        from src.storage import WikiStore
        WikiStore.save("first")
        WikiStore.save("second")
        assert WikiStore.load() == "second"
