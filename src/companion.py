import json
import os
import re
from typing import List, Optional

from src.chat_agent import ChatAgent, ChatReply
from src.models import Transaction
from src.llm_orchestrator import generate_checkin, answer_question, generate_monthly_analysis
from src.storage import ConversationStore, WikiStore, UserConfigStore
from src.wiki_updater import should_update_wiki, bootstrap_wiki, update_wiki

PREFS_PATH = "data/user_prefs.json"
MAX_TURNS = 50

CHECKIN_PHRASES = ["check in", "check-in", "checkin", "how am i doing", "summary", "overview"]
SET_GOAL_PHRASES = ["set goal", "my goal", "i want to save", "goal is"]
SHOW_GOALS_PHRASES = ["show goals", "my goals", "what are my goals"]
MONTHLY_ANALYSIS_PHRASES = ["monthly analysis", "analyze my month", "month summary", "monthly summary"]


def _load_prefs() -> dict:
    try:
        from src.prefs import load_prefs
        return load_prefs(PREFS_PATH)
    except ImportError:
        try:
            with open(PREFS_PATH, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def _save_prefs(prefs: dict) -> None:
    try:
        from src.prefs import save_prefs
        save_prefs(prefs, PREFS_PATH)
        return
    except ImportError:
        pass
    os.makedirs(os.path.dirname(PREFS_PATH) or ".", exist_ok=True)
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f, indent=2)


def _detect_intent(message: str) -> str:
    lowered = message.lower()
    if any(p in lowered for p in SET_GOAL_PHRASES):
        return "set_goal"
    if any(p in lowered for p in SHOW_GOALS_PHRASES):
        return "show_goals"
    if any(p in lowered for p in MONTHLY_ANALYSIS_PHRASES):
        return "monthly_analysis"
    if any(p in lowered for p in CHECKIN_PHRASES):
        return "checkin"
    return "question"


def _parse_goal(message: str) -> tuple:
    """Return (label, amount). amount is None if not found."""
    amount = None
    amount_match = re.search(r"\d+(?:\.\d+)?", message.replace(",", ""))
    if amount_match:
        amount = float(amount_match.group())

    lowered = message.lower()
    label = message
    goal_idx = lowered.rfind("goal")
    if goal_idx != -1:
        label = message[goal_idx + len("goal"):]
    elif "i want to save" in lowered:
        idx = lowered.find("i want to save")
        label = message[idx + len("i want to save"):]

    if amount_match:
        label = re.sub(r"\$?\d[\d,]*(?:\.\d+)?", "", label)

    # Strip leading connector words left over from natural phrasing.
    label = re.sub(
        r"^(?:\s|[:\-$])*(?:is\s+|to\s+|of\s+|save\s+|for\s+|a\s+|an\s+|my\s+|the\s+)*",
        "",
        label,
        flags=re.IGNORECASE,
    )
    label = label.strip(" \t\n:-$").strip()
    return label, amount


class Companion:
    MAX_TURNS = MAX_TURNS

    def __init__(self, memory_path: str = "data/memory.json"):
        self.memory_path = memory_path  # kept for CLI backward compat

        # Rotate session if gap > 30 min; get prev turns for wiki update
        self.session_id, is_new_session, prev_turns = ConversationStore.rotate_session_if_needed()

        if is_new_session and prev_turns and should_update_wiki(prev_turns):
            try:
                current_wiki = WikiStore.load()
                updated = update_wiki(prev_turns, current_wiki)
                WikiStore.save(updated)
            except Exception as e:
                print(f"Warning: session wiki update failed: {e}")

        # Bootstrap wiki on first run
        if not WikiStore.exists():
            try:
                config = UserConfigStore.load()
                WikiStore.save(bootstrap_wiki(config))
            except Exception as e:
                print(f"Warning: could not bootstrap wiki: {e}")

        self.wiki = WikiStore.load()
        self.session_turns = ConversationStore.get_session_turns(self.session_id)
        self.history = ConversationStore.load(max_turns=self.MAX_TURNS // 2)

    def chat(
        self,
        user_message: str,
        transactions: List[Transaction],
        image_b64: str = None,
        mime_type: str = "image/jpeg",
        chart_context: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> ChatReply:
        """Single user turn. Always returns a ChatReply.

        - `question` intent → routes to ChatAgent.run() (tool-using loop, may
          attach `blocks`).
        - other intents → existing handlers, wrapped in a text-only ChatReply.
        """
        self.history.append({"role": "user", "content": user_message})
        ConversationStore.append("user", user_message, session_id=self.session_id)
        self.session_turns.append({"role": "user", "content": user_message})

        intent = _detect_intent(user_message)
        prefs = _load_prefs()

        reply: ChatReply
        if intent == "checkin":
            reply = ChatReply(text=generate_checkin(transactions, prefs, wiki=self.wiki))
        elif intent == "set_goal":
            reply = ChatReply(text=self._handle_set_goal(user_message, prefs))
        elif intent == "show_goals":
            reply = ChatReply(text=self._handle_show_goals(prefs))
        elif intent == "monthly_analysis":
            reply = ChatReply(text=generate_monthly_analysis(transactions, prefs, wiki=self.wiki))
        else:
            # Phase 1C drill-down. The ChatAgent does its own tool-using LLM
            # loop against the reconciled view, so we hand it `user_id` and
            # the dashboard `chart_context` directly. `transactions` is not
            # used here — the tools query the DB.
            reply = ChatAgent().run(
                user_id=user_id or "default",
                user_message=user_message,
                history=self.history,
                chart_context=chart_context,
                wiki_text=self.wiki or "",
            )

        # Persist only the text — blocks are render-time concerns, not history.
        self.history.append({"role": "assistant", "content": reply.text})
        self.history = self.history[-(MAX_TURNS * 2):]
        ConversationStore.append("assistant", reply.text, session_id=self.session_id)
        self.session_turns.append({"role": "assistant", "content": reply.text})
        return reply

    def _handle_set_goal(self, user_message: str, prefs: dict) -> str:
        label, amount = _parse_goal(user_message)
        if not label:
            label = "your savings goal"

        if amount is None:
            return (
                f"Love that you want to set a goal around \"{label}\". "
                "How much would you like to aim for each month? Just let me know the amount."
            )

        prefs["saving_goal"] = {"label": label, "monthly_target": amount}
        _save_prefs(prefs)
        return (
            f"Got it — I've saved your goal: \"{label}\" with a monthly target of ${amount:g}. "
            "We'll take it one step at a time, no pressure."
        )

    def _handle_show_goals(self, prefs: dict) -> str:
        goal = prefs.get("saving_goal")
        intentions = prefs.get("intentions", [])

        lines = []
        if goal:
            label = goal.get("label", "Savings goal")
            target = goal.get("monthly_target")
            if target is not None:
                lines.append(f"Saving goal: {label} — ${target:g}/month")
            else:
                lines.append(f"Saving goal: {label}")
        if intentions:
            lines.append("Intentions:")
            for item in intentions:
                lines.append(f"  - {item}")

        if not lines:
            return (
                "You haven't set any goals yet — and that's totally fine. "
                "Whenever you're ready, just tell me something like \"set goal emergency fund 500\"."
            )

        return "Here's what you're working toward:\n" + "\n".join(lines)

    def clear_memory(self) -> None:
        self._finalize_session()
        self.history = []
        self.session_turns = []
        ConversationStore.clear()

    def _finalize_session(self) -> None:
        """Update wiki at explicit session end (New Chat). Requires >= 2 assistant turns."""
        if not self.session_turns or not should_update_wiki(self.session_turns):
            return
        try:
            current_wiki = WikiStore.load()
            updated = update_wiki(self.session_turns, current_wiki)
            WikiStore.save(updated)
            self.wiki = updated
        except Exception as e:
            print(f"Warning: wiki finalization failed: {e}")
