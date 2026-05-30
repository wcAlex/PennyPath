import argparse
import os
import sys

# Ensure the project root is on sys.path so `from src.xxx import` works
# regardless of how this script is invoked.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from src.prefs import load_prefs, save_prefs


def _load_transactions(local: bool):
    """Load transactions either from local statements or from Plaid.

    Returns a list of Transaction, or None if the source could not be loaded
    (a helpful message is printed in that case).
    """
    if local:
        try:
            from src.statement_ingester import ingest_statements
        except ImportError as e:
            print(f"Could not load statement ingester: {e}")
            print("Install dependencies with: pip install pdfminer.six")
            return None
        transactions = ingest_statements()
        if not transactions:
            print("No transactions found in data/statements/.")
            print("Drop PDF or CSV bank statements there and try again.")
        return transactions

    try:
        from src.plaid_client import PlaidClient, PlaidError
    except ImportError as e:
        print(f"Could not load Plaid client: {e}")
        print("Install dependencies with: pip install plaid-python python-dotenv")
        print("Or run with --local to use bank statements in data/statements/.")
        return None

    try:
        client = PlaidClient()
        return client.get_transactions()
    except PlaidError as e:
        print(f"Plaid is not available: {e}")
        print("Check your .env configuration, or run with --local instead.")
        return None


def cmd_chat(args):
    try:
        from src.companion import Companion
    except ImportError as e:
        print(f"Could not load companion: {e}")
        return

    transactions = _load_transactions(args.local)
    if transactions is None:
        return

    companion = Companion()
    print("Chatting with PennyPath. Type 'quit' or 'exit' to leave.")
    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit"):
            break
        try:
            response = companion.chat(user_input, transactions)
        except Exception as e:
            print(f"Something went wrong: {e}")
            continue
        print(response)
    print("Take care!")


def cmd_checkin(args):
    try:
        from src.llm_orchestrator import generate_checkin
    except ImportError as e:
        print(f"Could not load LLM orchestrator: {e}")
        return

    transactions = _load_transactions(args.local)
    if transactions is None:
        return

    prefs = load_prefs()
    try:
        message = generate_checkin(transactions, prefs)
    except Exception as e:
        print(f"Could not generate check-in: {e}")
        return
    print(message)


def cmd_set_goal(args):
    label = input("Goal label: ").strip()
    amount_raw = input("Monthly amount ($): ").strip()
    try:
        amount = float(amount_raw)
    except ValueError:
        print(f"'{amount_raw}' is not a valid amount.")
        return

    prefs = load_prefs()
    prefs.setdefault("goals", []).append({"label": label, "amount": amount})
    save_prefs(prefs)
    print(f"Saved goal: {label} (${amount}/month)")


def cmd_show_goals(args):
    prefs = load_prefs()
    goals = prefs.get("goals", [])
    if not goals:
        print("No goals set yet. Run 'pennypath set-goal' to add one.")
        return
    for goal in goals:
        print(f"- {goal['label']}: ${goal['amount']}/month")


def cmd_ingest(args):
    try:
        from src.statement_ingester import ingest_statements
    except ImportError as e:
        print(f"Could not load statement ingester: {e}")
        print("Install dependencies with: pip install pdfminer.six")
        return

    print("Ingesting statements from data/statements/ ...")
    transactions = ingest_statements()

    from src.storage import TransactionStore
    from src.statement_ingester import _user_id
    flagged = TransactionStore.get_parse_errors(_user_id())
    print(f"Done. {len(transactions)} transaction(s) in the store.")

    hard_errors = [f for f in flagged if f.get("parse_error")]
    recon_warnings = [f for f in flagged if f.get("recon_warning") and not f.get("parse_error")]

    if hard_errors:
        print(f"\n{len(hard_errors)} file(s) had parse errors:")
        for f in hard_errors:
            print(f"  - {f['filename']}: {f['parse_error']}")
    if recon_warnings:
        print(f"\n{len(recon_warnings)} file(s) had reconciliation warnings:")
        for f in recon_warnings:
            print(f"  - {f['filename']}: {f['recon_warning']}")


def cmd_accounts(args):
    from src.storage import TransactionStore
    accounts = TransactionStore.query_accounts()
    if not accounts:
        print("No accounts yet. Run 'pennypath ingest' first.")
        return

    header = f"{'BANK':<22} {'NAME':<30} {'TYPE':<10} {'MASK':<6} {'SOURCE':<12} {'TXNS':>6}"
    print(header)
    print("-" * len(header))
    for a in accounts:
        bank = (a["bank"] or "-")[:22]
        name = (a["name"] or "-")[:30]
        mask = a["mask"] or "-"
        print(
            f"{bank:<22} {name:<30} {a['type']:<10} {mask:<6} "
            f"{a['source']:<12} {a['tx_count']:>6}"
        )


def cmd_rebuild_recon(args):
    """Rematerialize the reconciliation layer (transactions_recon)."""
    from src.reconciler import rebuild_recon, rebuild_all
    if args.user:
        n = rebuild_recon(args.user)
        print(f"Reconciled {n} row(s) for user '{args.user}'.")
    else:
        results = rebuild_all()
        if not results:
            print("No transactions to reconcile.")
        for uid, n in results.items():
            print(f"Reconciled {n} row(s) for user '{uid}'.")


def main():
    parser = argparse.ArgumentParser(
        prog="pennypath", description="PennyPath finance companion"
    )
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="interactive conversation loop")
    chat_parser.add_argument(
        "--local",
        action="store_true",
        help="load transactions from data/statements/ instead of Plaid",
    )
    chat_parser.set_defaults(func=cmd_chat)

    checkin_parser = subparsers.add_parser(
        "checkin", help="one-shot daily check-in message"
    )
    checkin_parser.add_argument(
        "--local",
        action="store_true",
        help="load transactions from data/statements/ instead of Plaid",
    )
    checkin_parser.set_defaults(func=cmd_checkin)

    ingest_parser = subparsers.add_parser(
        "ingest", help="parse statements in data/statements/ into the store"
    )
    ingest_parser.set_defaults(func=cmd_ingest)

    accounts_parser = subparsers.add_parser(
        "accounts", help="list linked accounts and their transaction counts"
    )
    accounts_parser.set_defaults(func=cmd_accounts)

    set_goal_parser = subparsers.add_parser("set-goal", help="add a savings goal")
    set_goal_parser.set_defaults(func=cmd_set_goal)

    show_goals_parser = subparsers.add_parser("show-goals", help="list saved goals")
    show_goals_parser.set_defaults(func=cmd_show_goals)

    recon_parser = subparsers.add_parser(
        "rebuild-recon",
        help="rematerialize transactions_recon (transfer pairing, dedup, signs)",
    )
    recon_parser.add_argument(
        "--user", default=None,
        help="user_id to rebuild (default: all users)",
    )
    recon_parser.set_defaults(func=cmd_rebuild_recon)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
