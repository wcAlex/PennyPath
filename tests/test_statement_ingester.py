import pytest
from pathlib import Path
from src.statement_ingester import (
    _normalize_date,
    _parse_amount,
    _parse_magnitude,
    _parse_csv,
    _last4,
    _normalize_section_type,
    _reconcile,
    ingest_statements,
    SECTION_DIRECTION,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_CSV = str(FIXTURES_DIR / "sample.csv")


class TestNormalizeDate:
    def test_iso_format(self):
        assert _normalize_date("2024-04-01") == "2024-04-01"

    def test_mm_dd_yyyy(self):
        assert _normalize_date("04/05/2024") == "2024-04-05"

    def test_month_dd_yyyy(self):
        assert _normalize_date("Apr 08 2024") == "2024-04-08"

    def test_month_with_comma(self):
        assert _normalize_date("Apr 08, 2024") == "2024-04-08"

    def test_invalid_returns_none(self):
        assert _normalize_date("not-a-date") is None

    def test_trims_whitespace(self):
        assert _normalize_date("  2024-04-01  ") == "2024-04-01"


class TestParseAmount:
    def test_plain_float(self):
        assert _parse_amount("28.50") == pytest.approx(28.50)

    def test_with_dollar_sign(self):
        assert _parse_amount("$28.50") == pytest.approx(28.50)

    def test_with_commas(self):
        assert _parse_amount("1,234.56") == pytest.approx(1234.56)

    def test_negative(self):
        assert _parse_amount("-45.00") == pytest.approx(-45.00)

    def test_invalid_returns_none(self):
        assert _parse_amount("not-a-number") is None

    def test_trims_whitespace(self):
        assert _parse_amount("  89.00  ") == pytest.approx(89.00)


class TestLast4:
    def test_masked_pattern_xxxx(self):
        assert _last4("XXXX XXXX XXXX 7370") == "7370"

    def test_account_ending_in_phrase(self):
        assert _last4("Account ending in 0418") == "0418"

    def test_full_numeric_account(self):
        assert _last4("123456789012") == "9012"

    def test_hyphen_separated(self):
        assert _last4("XX-7370") == "7370"

    def test_only_three_digits_returns_empty(self):
        # We require a 4+ digit contiguous group; "123" alone shouldn't match.
        assert _last4("acct 123") == ""

    def test_no_digits_returns_empty(self):
        assert _last4("Account ending in N/A") == ""
        assert _last4("") == ""

    def test_picks_last_group_when_multiple(self):
        # If multiple 4+ digit groups appear, we want the trailing one
        # (the account number tends to come last in formatted patterns).
        assert _last4("ref 1111 / acct ending in 9999") == "9999"

    def test_long_trailing_group_takes_last_4(self):
        assert _last4("account 12345678") == "5678"


class TestParseMagnitude:
    """LLM passes raw_amount as a verbatim string; code strips sign + commas + $."""
    def test_plain(self):
        assert _parse_magnitude("100.00") == pytest.approx(100.00)
    def test_with_commas(self):
        assert _parse_magnitude("6,391.79") == pytest.approx(6391.79)
    def test_negative_sign_stripped(self):
        assert _parse_magnitude("-788.29") == pytest.approx(788.29)
    def test_dollar_sign_stripped(self):
        assert _parse_magnitude("$1,234.56") == pytest.approx(1234.56)
    def test_accounting_parens(self):
        # (45.00) is accounting notation for -45.00 → magnitude is 45.00
        assert _parse_magnitude("(45.00)") == pytest.approx(45.00)
    def test_combined_artifacts(self):
        assert _parse_magnitude("-$1,234.56") == pytest.approx(1234.56)
    def test_invalid_returns_none(self):
        assert _parse_magnitude("not a number") is None
        assert _parse_magnitude("") is None
        assert _parse_magnitude(None) is None
    def test_numeric_input_works(self):
        assert _parse_magnitude(-50.0) == pytest.approx(50.0)
        assert _parse_magnitude(50.0) == pytest.approx(50.0)


class TestNormalizeSectionType:
    def test_valid_values_round_trip(self):
        for s in ("deposit", "withdrawal", "check", "fee", "interest_charged",
                  "interest_credited", "payment", "purchase", "refund"):
            assert _normalize_section_type(s) == s
    def test_uppercase_normalized(self):
        assert _normalize_section_type("DEPOSIT") == "deposit"
    def test_dashes_to_underscores(self):
        assert _normalize_section_type("interest-charged") == "interest_charged"
    def test_spaces_to_underscores(self):
        assert _normalize_section_type("interest credited") == "interest_credited"
    def test_unrecognized_returns_empty(self):
        assert _normalize_section_type("garbage") == ""
        assert _normalize_section_type("") == ""
        assert _normalize_section_type(None) == ""


class TestSectionDirection:
    """SECTION_DIRECTION should classify all enum values into in/out exactly."""
    def test_covers_all_section_types(self):
        from src.statement_ingester import _ALLOWED_SECTION_TYPES
        assert set(SECTION_DIRECTION) == _ALLOWED_SECTION_TYPES
    def test_in_set(self):
        assert SECTION_DIRECTION["deposit"]            == "in"
        assert SECTION_DIRECTION["interest_credited"]  == "in"
        assert SECTION_DIRECTION["payment"]            == "in"
        assert SECTION_DIRECTION["refund"]             == "in"
    def test_out_set(self):
        assert SECTION_DIRECTION["withdrawal"]         == "out"
        assert SECTION_DIRECTION["check"]              == "out"
        assert SECTION_DIRECTION["fee"]                == "out"
        assert SECTION_DIRECTION["interest_charged"]   == "out"
        assert SECTION_DIRECTION["purchase"]           == "out"


class TestReconcile:
    """Per-comparison tier boundaries: ≤$0.01 clean / ≤$5 or ≤5% warn / above fail.
    A single failed comparison fails the whole file. Under Option 5, magnitudes
    are always positive and reconcile buckets by section_type."""

    def _make_tx(self, section_type: str, amount: float, account_type: str = "credit"):
        from src.models import Transaction
        # flow_type is set from section_type via a reasonable default for the test;
        # reconcile only consults section_type for bucket sums anyway.
        flow_default = {
            "purchase": "spending", "payment": "transfer", "refund": "refund",
            "fee": "fee", "interest_charged": "interest", "interest_credited": "interest",
            "deposit": "income", "withdrawal": "spending", "check": "spending",
        }[section_type]
        return Transaction(
            id="t", date="2024-04-01", amount=amount, description="X",
            category="", account_type=account_type, flow_type=flow_default,
            section_type=section_type,
        )

    # --- tier boundaries (credit-card flavor) ---
    def test_all_clean_returns_none_none(self):
        txs = [self._make_tx("purchase", 100.00)]
        meta = {"total_purchases": 100.00, "account_type": "credit"}
        assert _reconcile(txs, meta) == (None, None)

    def test_within_cent_is_clean(self):
        txs = [self._make_tx("purchase", 100.005)]
        meta = {"total_purchases": 100.00, "account_type": "credit"}
        assert _reconcile(txs, meta) == (None, None)

    def test_small_dollar_gap_is_warn(self):
        txs = [self._make_tx("purchase", 103.00)]  # gap $3 ≤ $5
        meta = {"total_purchases": 100.00, "account_type": "credit"}
        warning, error = _reconcile(txs, meta)
        assert error is None
        assert warning is not None and "purchases" in warning

    def test_percentage_band_passes_when_dollar_band_fails(self):
        # Gap $40 on $1000 = 4% → under 5%, still warn
        txs = [self._make_tx("purchase", 1040.00)]
        meta = {"total_purchases": 1000.00, "account_type": "credit"}
        warning, error = _reconcile(txs, meta)
        assert error is None and warning is not None

    def test_above_both_bands_is_error(self):
        # Gap $200 on $1000 = 20% → over both
        txs = [self._make_tx("purchase", 1200.00)]
        meta = {"total_purchases": 1000.00, "account_type": "credit"}
        warning, error = _reconcile(txs, meta)
        assert warning is None
        assert error is not None and "purchases" in error

    def test_single_failed_comparison_fails_whole_file(self):
        txs = [
            self._make_tx("purchase", 100.00),
            self._make_tx("interest_charged", 500.00),
        ]
        meta = {
            "total_purchases": 100.00,
            "total_interest": 10.00,  # parsed $500 vs $10 → fail
            "account_type": "credit",
        }
        _, error = _reconcile(txs, meta)
        assert error is not None and "interest" in error

    def test_untagged_section_is_warning_not_error(self):
        from src.models import Transaction
        txs = [Transaction(
            id="t", date="2024-04-01", amount=50.0, description="X",
            category="", account_type="credit", flow_type="spending",
            section_type="",  # missing tag
        )]
        meta = {"account_type": "credit"}
        warning, error = _reconcile(txs, meta)
        assert error is None
        assert warning is not None and "section_type" in warning

    def test_null_statement_totals_are_skipped(self):
        txs = [self._make_tx("purchase", 999.00)]
        meta = {"total_purchases": None, "account_type": "credit"}
        assert _reconcile(txs, meta) == (None, None)

    # --- per-account balance flow ---
    def test_checking_net_change_uses_in_minus_out(self):
        # eStmt_2025-09-11.pdf shape:
        # 3 deposits totaling 13,314.93; 8 withdrawals totaling 10,906.63
        # prev=$1,878.85, new=$4,287.15 → expected_net = +2,408.30 (balance up)
        txs = (
            [self._make_tx("deposit", 13314.93, account_type="checking")] +
            [self._make_tx("withdrawal", 10906.63, account_type="checking")]
        )
        meta = {
            "account_type": "checking",
            "previous_balance": 1878.85,
            "new_balance": 4287.15,
        }
        assert _reconcile(txs, meta) == (None, None)

    def test_credit_net_change_uses_out_minus_in(self):
        # CC: $500 purchase + $30 interest_charged + $5 fee minus $200 payment
        # balance owed change = (500+30+5) - 200 = +335 (debt grew)
        txs = [
            self._make_tx("purchase", 500.00),
            self._make_tx("interest_charged", 30.00),
            self._make_tx("fee", 5.00),
            self._make_tx("payment", 200.00),
        ]
        meta = {
            "account_type": "credit",
            "previous_balance": 1000.00,
            "new_balance": 1335.00,
        }
        assert _reconcile(txs, meta) == (None, None)

    def test_interest_routes_by_account_type(self):
        # On a checking statement, total_interest refers to interest CREDITED
        txs = [self._make_tx("interest_credited", 0.50, account_type="checking")]
        meta = {"total_interest": 0.50, "account_type": "checking"}
        assert _reconcile(txs, meta) == (None, None)

    def test_error_message_absorbs_warnings(self):
        txs = [
            self._make_tx("purchase", 200.00),         # gap $100 on $100 → fail
            self._make_tx("interest_charged", 13.00),  # gap $3 on $10 → warn
        ]
        meta = {
            "total_purchases": 100.00,
            "total_interest": 10.00,
            "account_type": "credit",
        }
        warning, error = _reconcile(txs, meta)
        assert warning is None
        assert error is not None
        assert "purchases" in error and "interest" in error


class TestParseCsv:
    def test_returns_5_transactions(self):
        txs = _parse_csv(SAMPLE_CSV, "local_user")
        assert len(txs) == 5

    def test_dates_are_normalized(self):
        txs = _parse_csv(SAMPLE_CSV, "local_user")
        dates = {t.date for t in txs}
        assert "2024-04-01" in dates
        assert "2024-04-05" in dates  # from MM/DD/YYYY
        assert "2024-04-08" in dates  # from Apr 08 2024

    def test_amounts_correct(self):
        txs = _parse_csv(SAMPLE_CSV, "local_user")
        amounts = sorted(t.amount for t in txs)
        assert amounts == pytest.approx([12.00, 28.50, 45.00, 89.00, 145.00])

    def test_descriptions_present(self):
        txs = _parse_csv(SAMPLE_CSV, "local_user")
        descs = {t.description for t in txs}
        assert "Chipotle" in descs
        assert "Whole Foods" in descs
        assert "Uber Eats" in descs

    def test_account_types_valid(self):
        txs = _parse_csv(SAMPLE_CSV, "local_user")
        for t in txs:
            assert t.account_type in ("checking", "credit")


class TestIngestStatements:
    def test_two_files_same_content_are_independent(self, tmp_path):
        """Two different filenames with identical CSV content produce transactions from both files.
        Deduplication is file-scoped (by mtime), not cross-file content-based."""
        import shutil
        stmts_dir = tmp_path / "statements"
        stmts_dir.mkdir()
        shutil.copy(SAMPLE_CSV, stmts_dir / "sample_a.csv")
        shutil.copy(SAMPLE_CSV, stmts_dir / "sample_b.csv")
        txs = ingest_statements(str(stmts_dir))
        assert len(txs) == 10

    def test_empty_directory_returns_empty(self, tmp_path):
        stmts_dir = tmp_path / "statements"
        stmts_dir.mkdir()
        txs = ingest_statements(str(stmts_dir))
        assert txs == []

    def test_nonexistent_directory_returns_empty(self, tmp_path):
        txs = ingest_statements(str(tmp_path / "nonexistent"))
        assert txs == []

    def test_results_sorted_descending_by_date(self, tmp_path):
        stmts_dir = tmp_path / "statements"
        stmts_dir.mkdir()
        import shutil
        shutil.copy(SAMPLE_CSV, stmts_dir / "sample.csv")
        txs = ingest_statements(str(stmts_dir))
        dates = [t.date for t in txs]
        assert dates == sorted(dates, reverse=True)

    def test_previously_failed_file_is_retried(self, tmp_path):
        """A file whose prior parse errored must be re-tried on the next ingest,
        even if its mtime hasn't changed. Otherwise a transient LLM failure
        becomes permanent until the user manually clears file_sources."""
        import shutil
        from src.storage import TransactionStore

        stmts_dir = tmp_path / "statements"
        stmts_dir.mkdir()
        dst = stmts_dir / "sample.csv"
        shutil.copy(SAMPLE_CSV, dst)

        # Seed a failed parse for this file at the file's actual mtime.
        import os
        from src.statement_ingester import _user_id
        uid = _user_id()
        mtime = os.path.getmtime(dst)
        TransactionStore.upsert_file_source(
            uid, "sample.csv", str(dst), mtime, "llm", 0, "transient LLM failure"
        )

        txs = ingest_statements(str(stmts_dir))
        # The retry should succeed and parse all 5 rows from the CSV.
        assert len(txs) == 5
        fs = TransactionStore.get_file_source(uid, str(dst))
        assert fs["parse_error"] is None
        assert fs["tx_count"] == 5

    def test_clean_file_is_not_reparsed(self, tmp_path):
        """The mtime guard must still short-circuit when the prior parse was clean."""
        import shutil
        from src.storage import TransactionStore

        stmts_dir = tmp_path / "statements"
        stmts_dir.mkdir()
        dst = stmts_dir / "sample.csv"
        shutil.copy(SAMPLE_CSV, dst)

        from src.statement_ingester import _user_id
        uid = _user_id()
        ingest_statements(str(stmts_dir))
        first_parsed_at = TransactionStore.get_file_source(uid, str(dst))["parsed_at"]

        # Second run with unchanged mtime should leave parsed_at untouched.
        ingest_statements(str(stmts_dir))
        second_parsed_at = TransactionStore.get_file_source(uid, str(dst))["parsed_at"]
        assert first_parsed_at == second_parsed_at
