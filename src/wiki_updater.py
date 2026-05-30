from pathlib import Path
from typing import List

_WIKI_UPDATE_PROMPT_PATH = Path(__file__).parent / "prompts" / "wiki_update.txt"


def _load_wiki_update_prompt() -> str:
    return _WIKI_UPDATE_PROMPT_PATH.read_text(encoding="utf-8")


def should_update_wiki(session_turns: List[dict]) -> bool:
    """Return True if the session had at least 2 assistant responses."""
    assistant_count = sum(1 for t in session_turns if t.get("role") == "assistant")
    return assistant_count >= 2


def bootstrap_wiki(config) -> str:
    """Create the initial wiki markdown from user config when no wiki exists yet."""
    name = config.name or "the user"
    profile_map = {
        "early_career": "early career",
        "growing_family": "growing family",
        "paying_debt": "paying down debt",
        "building_wealth": "building wealth",
    }
    profile = profile_map.get(config.finance_profile, config.finance_profile or "not specified")

    goal_section = "No goal set yet."
    if config.goal_label:
        target_str = f" Monthly target: ${config.goal_monthly_target:g}." if config.goal_monthly_target else ""
        goal_section = f"{config.goal_label}.{target_str}\nSignal: not enough data yet — check back after a few weeks."

    return f"""## Identity
{name}. Finance profile: {profile}. New to PennyPath.

## Goal
{goal_section}

## Active Concerns

## Observed Patterns

## Preferences

## Resolved
"""


def update_wiki(session_turns: List[dict], current_wiki: str) -> str:
    """
    Call the LLM to update the user wiki based on this session's turns.
    Returns the updated wiki content, or current_wiki if the update fails validation.
    """
    from src.llm_orchestrator import _client, _model

    if not session_turns:
        return current_wiki

    dialogue = "\n".join(
        f"{t['role'].capitalize()}: {t['content']}"
        for t in session_turns
        if isinstance(t.get("content"), str)
    )

    prompt = _load_wiki_update_prompt()
    user_message = f"Current profile:\n{current_wiki}\n\nToday's conversation:\n{dialogue}"

    try:
        client = _client()
        response = client.chat.completions.create(
            model=_model(),
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_message},
            ],
        )
        updated = response.choices[0].message.content.strip()

        required_headers = [
            "## Identity", "## Goal", "## Active Concerns",
            "## Observed Patterns", "## Preferences", "## Resolved",
        ]
        if all(h in updated for h in required_headers):
            return updated
        print("Warning: wiki update response missing required headers; keeping existing wiki")
        return current_wiki
    except Exception as e:
        print(f"Warning: wiki update failed: {e}")
        return current_wiki
