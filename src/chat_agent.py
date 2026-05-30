"""Phase 1C drill-down agent — bounded OpenAI-function-calling loop.

`ChatAgent.run(...)` is the only entry point. It builds a message list
(system prompt + history + chart_context + user message), then alternates
between the LLM (which may emit `tool_calls`) and the in-process tool
dispatcher in `src/chat_tools.py` until the LLM produces a final reply or
the loop hits its bounds (5 iterations / 20 seconds).

The final reply is parsed: if the LLM returned a JSON object with `text` and
`blocks`, those are split into a `ChatReply`. Otherwise the raw content is
the `text` and `blocks` is empty.

Tool-call / tool-result message pairs are NOT persisted to ConversationStore
— only the user/assistant text turns are. See design/chat_agent.md §5.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src import chat_tools
from src.llm_orchestrator import _client, _model, _safe_json_loads

_PROMPT_PATH = Path(__file__).parent / "prompts" / "chat_drill_down.txt"

MAX_ITERS = 5
WALL_BUDGET_SECONDS = 20.0
TOOL_TIMEOUT_SECONDS = 5.0  # per-tool soft budget; not enforced via signals

_FALLBACK_BUSY = (
    "I got a bit tangled trying to look that up — could you try that again "
    "in a moment?"
)
_FALLBACK_GAVE_UP = (
    "I tried a few angles and couldn't quite land it — want to try asking "
    "another way?"
)
_FALLBACK_ERROR = (
    "Something hiccupped on my end. Could you try that one more time?"
)


@dataclass
class ChatReply:
    text: str
    blocks: list[dict] = field(default_factory=list)


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _format_chart_context(chart_context: Optional[dict]) -> str:
    """Render the chart_context blob as a short, plain-language system note."""
    if not chart_context:
        return ("The user is **not** currently looking at a dashboard chart. "
                "Treat any vague question (no period, no category) as "
                "low-confidence — ask one focused clarifier before calling "
                "tools.")

    parts: list[str] = ["The user is currently viewing this on the dashboard:"]
    ck = chart_context.get("chart_key")
    if ck:
        parts.append(f"- chart: **{ck}**")
    period = chart_context.get("period")
    if period:
        label = period.get("label") or ""
        start = period.get("start") or ""
        end = period.get("end") or ""
        parts.append(f"- period: **{label}** ({start} → {end})")
    sel_cat = chart_context.get("selected_category")
    if sel_cat:
        parts.append(f"- selected category: **{sel_cat}**")
    sel_acc = chart_context.get("selected_account_id")
    if sel_acc:
        parts.append(f"- selected account_id: `{sel_acc}`")
    sn = chart_context.get("summary_numbers") or {}
    if sn:
        parts.append("- numbers already visible to the user (background only — "
                     "do not state new numbers from these; call tools instead):")
        parts.append(f"  ```json\n  {json.dumps(sn)}\n  ```")
    parts.append("Treat this as the default scope unless the user broadens "
                 "or narrows it.")
    return "\n".join(parts)


def _build_messages(
    history: list[dict],
    chart_context: Optional[dict],
    wiki_text: str,
    user_message: str,
) -> list[dict]:
    system = _load_prompt()
    if wiki_text and wiki_text.strip():
        system = system + "\n\n# What I know about this user\n\n" + wiki_text.strip()

    messages: list[dict] = [{"role": "system", "content": system}]
    messages.append({
        "role": "system",
        "content": _format_chart_context(chart_context),
    })

    # Recent text history (strip metadata keys that the OpenAI API rejects).
    for turn in history[-12:]:
        role = turn.get("role")
        content = turn.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})
    return messages


# Matches a markdown table: header row, separator row, ≥1 data row.
_MD_TABLE_RE = re.compile(
    r"(?:^|\n)"
    r"(\|[^\n]+\|)\s*\n"               # header row
    r"\|\s*[:\-\| ]+\s*\|\s*\n"        # separator row
    r"((?:\|[^\n]+\|\s*\n?)+)",        # data rows
    re.MULTILINE,
)


def _split_md_row(row: str) -> list[str]:
    """Split a markdown table row into trimmed cells."""
    inner = row.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _extract_md_table_block(text: str) -> tuple[str, Optional[dict]]:
    """If the prose contains a markdown table, lift it into a `table` block.

    DeepSeek (our default LLM) tends to format tabular data as markdown even
    when the prompt asks for the JSON envelope. Rather than force a second
    LLM call, we parse the first markdown table out and attach it as a block;
    the surrounding prose becomes the reply text.
    """
    m = _MD_TABLE_RE.search(text)
    if not m:
        return text, None
    header_row = m.group(1)
    data_rows_blob = m.group(2)
    columns = _split_md_row(header_row)
    rows: list[list[str]] = []
    for line in data_rows_blob.strip().split("\n"):
        if not line.strip().startswith("|"):
            continue
        cells = _split_md_row(line)
        if not any(cells):
            continue
        # Normalize column count (truncate extras, pad shorts).
        if len(cells) < len(columns):
            cells = cells + [""] * (len(columns) - len(cells))
        elif len(cells) > len(columns):
            cells = cells[:len(columns)]
        rows.append(cells)
    if not rows or not columns:
        return text, None
    # Strip the table from the prose, leaving the surrounding text intact.
    new_text = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    block = {"type": "table", "columns": columns, "rows": rows}
    return new_text, block


def _parse_reply(content: str) -> ChatReply:
    """Turn LLM output into a ChatReply.

    Order of attempts:
    1. JSON envelope `{"text": str, "blocks": [...]}` — preferred; happens
       when the LLM followed the prompt instruction.
    2. Prose with a markdown table — post-process to lift the table into a
       `table` block (DeepSeek prefers markdown tables; this absorbs that).
    3. Plain text — pass through unchanged.
    """
    text = (content or "").strip()
    if not text:
        return ChatReply(text="")
    if text.startswith("{"):
        parsed = _safe_json_loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            blocks = parsed.get("blocks") or []
            if not isinstance(blocks, list):
                blocks = []
            blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") in ("table", "chart_spec")]
            return ChatReply(text=parsed["text"], blocks=blocks)
    # Markdown-table fallback.
    stripped_text, md_block = _extract_md_table_block(text)
    if md_block:
        return ChatReply(text=stripped_text, blocks=[md_block])
    return ChatReply(text=text)


def _tool_calls_from_choice(choice) -> list[dict]:
    """Adapter — turn the SDK's tool_calls into plain dicts so we can iterate
    them once and also persist a compatible assistant-message structure."""
    out = []
    for tc in (choice.tool_calls or []):
        # Both attribute-style and dict-style work with the OpenAI SDK.
        fn = tc.function
        out.append({
            "id": tc.id,
            "type": "function",
            "function": {
                "name": fn.name,
                "arguments": fn.arguments or "{}",
            },
        })
    return out


class ChatAgent:
    """Stateless — `run()` does one user-turn worth of work.

    Tracing: pass `trace=...` (a dict the agent populates) to capture every
    iteration's request/response and tool dispatch — see /chat/debug. The env
    var `PENNYPATH_CHAT_TRACE=1` additionally prints a compact summary of
    every run() to stderr (handy while clicking around in the drawer).
    """

    def run(
        self,
        user_id: str,
        user_message: str,
        history: Optional[list[dict]] = None,
        chart_context: Optional[dict] = None,
        wiki_text: str = "",
        trace: Optional[dict] = None,
    ) -> ChatReply:
        history = history or []
        messages = _build_messages(history, chart_context, wiki_text, user_message)
        tools = chat_tools.to_openai_tools()

        # If the caller didn't pass a trace dict but env tracing is on, use a
        # local one so the stderr printer has something to render at the end.
        own_trace = False
        if trace is None and _env_trace_enabled():
            trace = {}
            own_trace = True
        if trace is not None:
            trace.setdefault("user_id", user_id)
            trace.setdefault("user_message", user_message)
            trace.setdefault("chart_context", chart_context)
            trace.setdefault("tools_available", [t["function"]["name"] for t in tools])
            trace.setdefault("iterations", [])

        client = _client()
        model = _model()
        start_t = time.monotonic()
        reply = ChatReply(text=_FALLBACK_GAVE_UP)
        stopped_reason = "max_iters"

        for iter_n in range(1, MAX_ITERS + 1):
            if time.monotonic() - start_t > WALL_BUDGET_SECONDS:
                reply = ChatReply(text=_FALLBACK_BUSY)
                stopped_reason = "wall_budget"
                break

            iter_record: dict = {}
            if trace is not None:
                iter_record = {
                    "n": iter_n,
                    "messages_sent": _scrub_messages_for_trace(messages),
                    "tool_calls": [],
                    "response": {},
                }
                trace["iterations"].append(iter_record)

            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
            except Exception as e:
                self._log(f"LLM call failed: {e!r}")
                if trace is not None:
                    iter_record["error"] = repr(e)
                reply = ChatReply(text=_FALLBACK_ERROR)
                stopped_reason = "llm_error"
                break

            choice = resp.choices[0].message
            tcs = _tool_calls_from_choice(choice)

            if trace is not None:
                iter_record["response"] = {
                    "content": choice.content or "",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "arguments_raw": tc["function"]["arguments"],
                        }
                        for tc in tcs
                    ],
                }

            if tcs:
                # Echo the assistant's tool-call turn back into the message log
                # so the next iteration can include the matching tool results.
                messages.append({
                    "role": "assistant",
                    "content": choice.content or "",
                    "tool_calls": tcs,
                })
                for tc in tcs:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tool_start = time.monotonic()
                    result = chat_tools.dispatch(user_id, name, args)
                    elapsed = time.monotonic() - tool_start
                    if elapsed > TOOL_TIMEOUT_SECONDS:
                        self._log(
                            f"tool '{name}' took {elapsed:.1f}s (over soft budget)"
                        )
                    if trace is not None:
                        iter_record["tool_calls"].append({
                            "id": tc["id"],
                            "name": name,
                            "arguments": args,
                            "result": result,
                            "elapsed_s": round(elapsed, 3),
                        })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, default=str),
                    })
                continue

            reply = _parse_reply(choice.content or "")
            stopped_reason = "final_reply"
            break

        if trace is not None:
            trace["stopped"] = stopped_reason
            trace["wall_elapsed_s"] = round(time.monotonic() - start_t, 3)
            trace["reply"] = {"text": reply.text, "blocks": reply.blocks}
            if own_trace:
                _print_trace_to_stderr(trace)
        return reply

    @staticmethod
    def _log(msg: str) -> None:
        try:
            import sys
            print(f"[chat_agent] {msg}", file=sys.stderr)
        except Exception:
            pass


# --- Tracing helpers ---------------------------------------------------------


def _env_trace_enabled() -> bool:
    return os.getenv("PENNYPATH_CHAT_TRACE") == "1"


def _scrub_messages_for_trace(messages: list[dict]) -> list[dict]:
    """Copy messages as-is for the JSON trace.

    The OpenAI SDK accepts long system prompts; we don't truncate them here
    (the /chat/debug consumer can render or fold as it likes). For stderr
    rendering, see `_print_trace_to_stderr` which applies its own elision.
    """
    out: list[dict] = []
    for m in messages:
        # Some entries (assistant turns with tool_calls) include nested
        # objects; serialize via dict copy.
        out.append({k: v for k, v in m.items()})
    return out


def _truncate(s: str, limit: int = 500) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    return s[:limit] + f" …[{len(s) - limit} more chars]"


def _print_trace_to_stderr(trace: dict) -> None:
    """Compact, human-scannable summary of one ChatAgent.run() call."""
    import sys

    def out(line: str) -> None:
        print(f"[chat_agent] {line}", file=sys.stderr)

    out("=" * 60)
    out(f"turn: user_id={trace.get('user_id')} "
        f"msg={_truncate(str(trace.get('user_message')), 120)!r}")
    ctx = trace.get("chart_context") or {}
    if ctx:
        period = (ctx.get("period") or {}).get("label", "?")
        out(f"chart_context: chart={ctx.get('chart_key')} "
            f"period={period} "
            f"cat={ctx.get('selected_category')!r} "
            f"acct={ctx.get('selected_account_id')!r}")
    else:
        out("chart_context: (none)")

    for it in trace.get("iterations", []):
        n = it.get("n")
        sent = it.get("messages_sent") or []
        role_counts = {}
        for m in sent:
            role_counts[m["role"]] = role_counts.get(m["role"], 0) + 1
        out(f"--- iter {n} — sent {len(sent)} messages "
            f"({', '.join(f'{r}:{c}' for r, c in role_counts.items())}) ---")

        if it.get("error"):
            out(f"  ERROR: {it['error']}")
            continue

        resp = it.get("response") or {}
        tcs = resp.get("tool_calls") or []
        if tcs:
            for tc in tcs:
                out(f"  → tool_call {tc['name']}({_truncate(tc.get('arguments_raw',''), 200)})")
            for record in (it.get("tool_calls") or []):
                rj = json.dumps(record.get("result", {}), default=str)
                out(f"  ← {record['name']} -> {_truncate(rj, 400)} "
                    f"({record.get('elapsed_s')}s)")
        else:
            content = resp.get("content") or ""
            out(f"  ← final text: {_truncate(content, 600)}")

    out(f"stopped: {trace.get('stopped')} "
        f"wall={trace.get('wall_elapsed_s')}s")
    reply = trace.get("reply") or {}
    blocks = reply.get("blocks") or []
    if blocks:
        out(f"reply: {len(blocks)} block(s) — "
            f"{', '.join(b.get('type', '?') for b in blocks)}")
    out("=" * 60)


__all__ = ["ChatAgent", "ChatReply", "MAX_ITERS", "WALL_BUDGET_SECONDS"]
