import pytest
from src.llm_orchestrator import _category_totals, generate_dashboard_summary, _summarize_for_checkin
from src.models import Transaction


class TestCategoryTotals:
    def test_groups_by_category_and_sums(self, sample_transactions):
        totals = _category_totals(sample_transactions)
        # dining: 28.50 + 45.00 = 73.50
        assert totals["dining"] == pytest.approx(73.50)
        assert totals["groceries"] == pytest.approx(89.00)
        assert totals["subscriptions"] == pytest.approx(12.00)
        assert totals["utilities"] == pytest.approx(145.00)

    def test_empty_transactions(self):
        totals = _category_totals([])
        assert totals == {}

    def test_single_transaction(self):
        txs = [Transaction(id="x", date="2024-04-01", amount=50.0, description="Test", category="food", account_type="credit")]
        totals = _category_totals(txs)
        assert totals == {"food": 50.0}


class TestGenerateDashboardSummary:
    def test_returns_required_keys(self, sample_transactions):
        result = generate_dashboard_summary(sample_transactions, {})
        assert "category_totals" in result
        assert "total_spend" in result
        assert "transaction_count" in result
        assert "top_categories" in result
        assert "large_charges" in result

    def test_total_spend_is_correct(self, sample_transactions):
        result = generate_dashboard_summary(sample_transactions, {})
        # 28.50 + 89.00 + 12.00 + 145.00 + 45.00 = 319.50
        assert result["total_spend"] == pytest.approx(319.50)

    def test_transaction_count(self, sample_transactions):
        result = generate_dashboard_summary(sample_transactions, {})
        assert result["transaction_count"] == 5

    def test_large_charges_only_over_100(self, sample_transactions):
        result = generate_dashboard_summary(sample_transactions, {})
        large = result["large_charges"]
        assert all(t.amount > 100 for t in large)
        # Only Electric bill (145.00) qualifies
        assert len(large) == 1
        assert large[0].description == "Electric bill"

    def test_empty_transactions(self):
        result = generate_dashboard_summary([], {})
        assert result["total_spend"] == 0.0
        assert result["transaction_count"] == 0
        assert result["large_charges"] == []


class TestSummarizeForCheckin:
    def test_empty_list_returns_no_transactions_string(self):
        result = _summarize_for_checkin([], {})
        assert "no transactions" in result.lower()

    def test_non_empty_returns_total_spend(self, sample_transactions):
        result = _summarize_for_checkin(sample_transactions, {})
        assert "319.50" in result

    def test_non_empty_mentions_categories(self, sample_transactions):
        result = _summarize_for_checkin(sample_transactions, {})
        assert "dining" in result
        assert "groceries" in result

    def test_large_transactions_mentioned(self, sample_transactions):
        result = _summarize_for_checkin(sample_transactions, {})
        assert "Electric bill" in result or "145" in result
