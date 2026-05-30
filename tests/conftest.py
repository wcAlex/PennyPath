import pytest
from pathlib import Path
from src.models import Transaction
from src.storage import DATA_DIR


@pytest.fixture
def sample_transactions():
    return [
        Transaction(id="t1", date="2024-04-01", amount=28.50, description="Chipotle", category="dining", account_type="credit"),
        Transaction(id="t2", date="2024-04-02", amount=89.00, description="Whole Foods", category="groceries", account_type="credit"),
        Transaction(id="t3", date="2024-04-03", amount=12.00, description="Spotify", category="subscriptions", account_type="credit"),
        Transaction(id="t4", date="2024-04-05", amount=145.00, description="Electric bill", category="utilities", account_type="checking"),
        Transaction(id="t5", date="2024-04-08", amount=45.00, description="Uber Eats", category="dining", account_type="credit"),
    ]


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    """Redirect all storage writes to a temp directory."""
    import src.storage as storage_module
    monkeypatch.setattr(storage_module, "DATA_DIR", tmp_path)
    from src.storage import UserConfigStore, ConversationStore, SnapshotStore, TransactionStore, WikiStore
    monkeypatch.setattr(UserConfigStore, "PATH", tmp_path / "config.json")
    monkeypatch.setattr(ConversationStore, "PATH", tmp_path / "memory.json")
    monkeypatch.setattr(SnapshotStore, "PATH", tmp_path / "snapshots.json")
    monkeypatch.setattr(TransactionStore, "DB_PATH", tmp_path / "transactions.db")
    monkeypatch.setattr(WikiStore, "PATH", tmp_path / "user_wiki.md")
    yield tmp_path
