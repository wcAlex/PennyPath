"""Tests for src/chat_agent.py — the bounded LLM-tool loop.

The LLM client is mocked end-to-end (no network calls). We script a sequence
of "LLM responses" and assert the agent dispatches the tools we expect, feeds
results back, parses the final reply (JSON envelope OR markdown-table
fallback), and respects MAX_ITERS / wall budget.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import chat_agent
from src.chat_agent import (
    ChatAgent, ChatReply, MAX_ITERS,
    _build_messages, _format_chart_context, _parse_reply,
    _extract_md_table_block,
)


# --- LLM mocking helpers -----------------------------------------------------


def _fake_tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _fake_completion(content: str = "", tool_calls: list | None = None) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class _ScriptedClient:
    """Returns a sequence of pre-baked completions on each .create() call.

    Records every call so tests can assert on the message list the agent
    sends (e.g., that chart_context lands in the system message, that tool
    results come back as `role=tool`).
    """

    def __init__(self, completions):
        self._queue = list(completions)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            raise RuntimeError("LLM client ran out of scripted responses")
        return self._queue.pop(0)


# --- _format_chart_context ---------------------------------------------------


class TestFormatChartContext:
    def test_null_context_marks_low_confidence(self):
        out = _format_chart_context(None)
        assert "not** currently looking" in out
        assert "low-confidence" in out

    def test_context_is_serialized_into_system_text(self):
        ctx = {
            "chart_key": "spending",
            "period": {"start": "2026-04-01", "end": "2026-04-30",
                        "label": "April 2026"},
            "selected_category": "Dining",
            "summary_numbers": {"total_spend": 1936.01},
        }
        out = _format_chart_context(ctx)
        assert "spending" in out
        assert "April 2026" in out
        assert "Dining" in out
        # The summary numbers are flagged as context-only.
        assert "background only" in out
        assert "1936.01" in out


# --- _build_messages ---------------------------------------------------------


class TestBuildMessages:
    def test_includes_system_then_chart_then_user(self):
        msgs = _build_messages(
            history=[],
            chart_context=None,
            wiki_text="",
            user_message="hello",
        )
        # First two messages are system: the agent prompt + the chart-context.
        assert msgs[0]["role"] == "system"
        assert "PennyPath" in msgs[0]["content"]
        assert msgs[1]["role"] == "system"
        assert "not** currently looking" in msgs[1]["content"]
        # Last is the user message.
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "hello"

    def test_wiki_appended_to_system(self):
        msgs = _build_messages(
            history=[], chart_context=None,
            wiki_text="User loves Don Angie.",
            user_message="hi",
        )
        assert "Don Angie" in msgs[0]["content"]

    def test_recent_history_included(self):
        history = [
            {"role": "user", "content": "prior 1"},
            {"role": "assistant", "content": "prior 2"},
        ]
        msgs = _build_messages(history=history, chart_context=None,
                                wiki_text="", user_message="now")
        roles = [m["role"] for m in msgs]
        # system, system(ctx), user(prior), assistant(prior), user(now)
        assert roles == ["system", "system", "user", "assistant", "user"]


# --- _parse_reply ------------------------------------------------------------


class TestParseReply:
    def test_plain_text_passthrough(self):
        r = _parse_reply("Looking at Dining for May — about $1,711.")
        assert r.text.startswith("Looking at Dining")
        assert r.blocks == []

    def test_json_envelope_extracted(self):
        envelope = (
            '{"text": "Top spots:", "blocks": [{'
            '"type": "table", "title": "T", "columns": ["A","B"], '
            '"rows": [["x","y"]]}]}'
        )
        r = _parse_reply(envelope)
        assert r.text == "Top spots:"
        assert len(r.blocks) == 1
        assert r.blocks[0]["type"] == "table"

    def test_json_envelope_filters_invalid_blocks(self):
        envelope = (
            '{"text": "x", "blocks": ['
            '{"type": "table", "rows": []},'
            '{"type": "bogus", "rows": []}'
            ']}'
        )
        r = _parse_reply(envelope)
        types = [b["type"] for b in r.blocks]
        assert types == ["table"]

    def test_markdown_table_extracted_to_block(self):
        prose = (
            "Looking at Dining for April:\n\n"
            "| Merchant | Spent | Visits |\n"
            "|---|---|---|\n"
            "| Don Angie | $412.00 | 2 |\n"
            "| Levain    | $187.50 | 5 |\n\n"
            "That's the picture."
        )
        r = _parse_reply(prose)
        assert "Looking at Dining" in r.text
        assert "That's the picture." in r.text
        assert "|" not in r.text  # the table itself was stripped
        assert len(r.blocks) == 1
        block = r.blocks[0]
        assert block["type"] == "table"
        assert block["columns"] == ["Merchant", "Spent", "Visits"]
        assert block["rows"] == [
            ["Don Angie", "$412.00", "2"],
            ["Levain", "$187.50", "5"],
        ]

    def test_empty_content(self):
        assert _parse_reply("").text == ""
        assert _parse_reply("   ").text == ""

    def test_envelope_embedded_in_prose_is_extracted(self):
        # Matches the exact failure mode the user reported: DeepSeek wrote a
        # warm intro, then dropped the JSON envelope inline, then continued
        # with analysis prose. The drawer was rendering the JSON as text.
        content = (
            "Of course! Here's a bar chart showing how your top dining "
            "merchants stack up in April:\n"
            '{"text":"","blocks":[{"type":"chart_spec",'
            '"title":"Top Dining Merchants — April 2026","chart_type":"bar",'
            '"labels":["Seattle Pacific Table","Fortune Feast"],'
            '"series":[{"name":"Spent","data":[605,188.33]}]}]}\n'
            "**Seattle Pacific Table** towers over everything at $605.\n"
            "Want me to pull up how March looked so you can compare?"
        )
        r = _parse_reply(content)
        # The chart block was lifted out.
        assert len(r.blocks) == 1
        assert r.blocks[0]["type"] == "chart_spec"
        assert r.blocks[0]["title"] == "Top Dining Merchants — April 2026"
        assert r.blocks[0]["labels"] == ["Seattle Pacific Table", "Fortune Feast"]
        # The surrounding prose became the reply text. No raw JSON leakage.
        assert "Here's a bar chart" in r.text
        assert "towers over everything" in r.text
        assert "Want me to pull up" in r.text
        assert "chart_spec" not in r.text
        assert '"blocks"' not in r.text

    def test_envelope_embedded_with_envelope_text_when_no_surrounding(self):
        # Envelope only, but with a populated text field — equivalent to
        # case (1) of _parse_reply; verify both paths produce the same result.
        content = (
            '{"text":"Here it is:","blocks":'
            '[{"type":"table","title":"X","columns":["a"],"rows":[["b"]]}]}'
        )
        r = _parse_reply(content)
        assert r.text == "Here it is:"
        assert r.blocks[0]["type"] == "table"

    def test_balanced_close_handles_nested_braces(self):
        # Envelope contains a nested object — extractor must respect depth.
        content = (
            "Intro.\n"
            '{"text":"X","blocks":[{"type":"chart_spec","title":"Y",'
            '"chart_type":"bar","labels":["a"],'
            '"series":[{"name":"S","data":[1]}]}]}\n'
            "Outro."
        )
        r = _parse_reply(content)
        assert len(r.blocks) == 1
        assert r.text.startswith("Intro.")
        assert r.text.endswith("Outro.")

    def test_malformed_envelope_with_extra_brace_is_repaired(self):
        # The exact failure mode from the screenshot: LLM wrote `]}` where
        # `]]` was expected after the last row, leaving one extra `}`. The
        # lenient parser deletes the offending char and retries.
        content = (
            "And here's the table with how many times you visited each spot:\n"
            '{"text":"","blocks":[{"type":"table","title":"Dining Breakdown — April 2026",'
            '"columns":["Merchant","Spent","Visits"],"rows":'
            '[["Seattle Pacific Table Ten Bellevue","$605.00",8],'
            '["Fortune Feast Richmond","$188.33",1],'
            '["Other (16 spots)","$385.58",3]}]}]}\n'
            "A couple things that stand out: Seattle Pacific Table at $605 is 31%."
        )
        r = _parse_reply(content)
        # The table block was recovered.
        assert len(r.blocks) == 1
        assert r.blocks[0]["type"] == "table"
        assert r.blocks[0]["title"] == "Dining Breakdown — April 2026"
        assert r.blocks[0]["columns"] == ["Merchant", "Spent", "Visits"]
        assert len(r.blocks[0]["rows"]) == 3
        assert r.blocks[0]["rows"][0] == ["Seattle Pacific Table Ten Bellevue", "$605.00", 8]
        # Prose on either side is preserved; raw JSON is gone.
        assert "And here's the table" in r.text
        assert "stand out: Seattle Pacific Table" in r.text
        assert '"blocks"' not in r.text
        assert '"rows"' not in r.text

    def test_multiple_envelopes_in_one_reply(self):
        # Reply contains a chart envelope AND a table envelope. Both should
        # be extracted into separate blocks; prose stays intact.
        content = (
            "Here's the chart:\n"
            '{"text":"","blocks":[{"type":"chart_spec","title":"April Dining",'
            '"chart_type":"bar","labels":["A","B"],'
            '"series":[{"name":"Spent","data":[1,2]}]}]}\n'
            "And here's the table:\n"
            '{"text":"","blocks":[{"type":"table","title":"Visits",'
            '"columns":["X","Y"],"rows":[["a",1],["b",2]]}]}\n'
            "Anything else?"
        )
        r = _parse_reply(content)
        assert len(r.blocks) == 2
        assert [b["type"] for b in r.blocks] == ["chart_spec", "table"]
        assert r.blocks[0]["title"] == "April Dining"
        assert r.blocks[1]["title"] == "Visits"
        # Surrounding prose is preserved.
        assert "Here's the chart" in r.text
        assert "And here's the table" in r.text
        assert "Anything else?" in r.text
        # No raw JSON leaks.
        assert "chart_spec" not in r.text
        assert "blocks" not in r.text

    def test_lenient_loader_repairs_misplaced_brace_after_rows(self):
        from src.chat_agent import _safe_json_loads_lenient
        # Pattern from the screenshot: one `}` placed where `]` was expected
        # (closing the rows array). Strict json.loads errors at the bad `}`;
        # the lenient repair deletes that char and parses successfully.
        rows_broken = '{"rows":[[1,2],[3,4]}]}'  # extra `}` between row 2 and rows-array close
        parsed = _safe_json_loads_lenient(rows_broken)
        assert parsed == {"rows": [[1, 2], [3, 4]]}

    def test_lenient_loader_repairs_trailing_comma(self):
        from src.chat_agent import _safe_json_loads_lenient
        parsed = _safe_json_loads_lenient('{"a":[1,2,]}')
        assert parsed == {"a": [1, 2]}


# --- _extract_md_table_block (unit, finer-grained) ---------------------------


class TestExtractMdTable:
    def test_no_table_returns_none(self):
        text, block = _extract_md_table_block("Just prose. No table.")
        assert block is None
        assert text == "Just prose. No table."

    def test_table_with_uneven_columns_padded(self):
        prose = "Header\n| A | B | C |\n|---|---|---|\n| 1 | 2 |\n"
        text, block = _extract_md_table_block(prose)
        assert block is not None
        assert block["rows"] == [["1", "2", ""]]


# --- The bounded agent loop --------------------------------------------------


class TestAgentLoop:
    def _patch_client_and_dispatch(self, monkeypatch, completions,
                                    dispatch_return=None):
        client = _ScriptedClient(completions)
        monkeypatch.setattr(chat_agent, "_client", lambda: client)
        monkeypatch.setattr(chat_agent, "_model", lambda: "deepseek-chat")
        if dispatch_return is None:
            dispatch_return = {"ok": True}
        recorded_calls: list[tuple[str, str, dict]] = []

        def fake_dispatch(user_id, name, args):
            recorded_calls.append((user_id, name, args))
            return dispatch_return

        monkeypatch.setattr(chat_agent.chat_tools, "dispatch", fake_dispatch)
        return client, recorded_calls

    def test_no_tool_call_returns_text(self, monkeypatch):
        completions = [_fake_completion(content="Hi! How can I help?")]
        client, calls = self._patch_client_and_dispatch(monkeypatch, completions)

        reply = ChatAgent().run("u1", "hi", history=[], chart_context=None)
        assert reply.text == "Hi! How can I help?"
        assert reply.blocks == []
        assert len(calls) == 0  # no tools called

    def test_one_tool_then_final_reply(self, monkeypatch):
        completions = [
            _fake_completion(tool_calls=[
                _fake_tool_call("c1", "list_categories", "{}"),
            ]),
            _fake_completion(content="Got the categories."),
        ]
        _, calls = self._patch_client_and_dispatch(
            monkeypatch, completions,
            dispatch_return={"categories": [{"name": "Dining"}]},
        )
        reply = ChatAgent().run("u1", "what categories?",
                                  history=[], chart_context=None)
        assert reply.text == "Got the categories."
        assert calls == [("u1", "list_categories", {})]

    def test_tool_result_fed_back_to_llm(self, monkeypatch):
        completions = [
            _fake_completion(tool_calls=[
                _fake_tool_call("c1", "list_categories", "{}"),
            ]),
            _fake_completion(content="Done."),
        ]
        client, _ = self._patch_client_and_dispatch(
            monkeypatch, completions,
            dispatch_return={"categories": []},
        )
        ChatAgent().run("u1", "x", history=[], chart_context=None)
        # The second completion should have seen an assistant-with-tool_calls
        # message and a tool-role message carrying the dispatch result.
        second_call = client.calls[1]
        roles = [m["role"] for m in second_call["messages"]]
        assert "tool" in roles
        tool_msg = next(m for m in second_call["messages"] if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "c1"
        assert "categories" in tool_msg["content"]

    def test_max_iters_cap_returns_fallback(self, monkeypatch):
        # LLM keeps tool-calling forever. Loop should bail at MAX_ITERS.
        completions = [
            _fake_completion(tool_calls=[
                _fake_tool_call(f"c{i}", "list_categories", "{}"),
            ])
            for i in range(MAX_ITERS + 2)
        ]
        _, calls = self._patch_client_and_dispatch(monkeypatch, completions)
        reply = ChatAgent().run("u1", "loop", history=[], chart_context=None)
        assert "couldn't quite land it" in reply.text or "tangled" in reply.text
        assert len(calls) == MAX_ITERS

    def test_chart_context_lands_in_system_message(self, monkeypatch):
        completions = [_fake_completion(content="ok")]
        client, _ = self._patch_client_and_dispatch(monkeypatch, completions)
        ctx = {
            "chart_key": "spending",
            "period": {"start": "2026-04-01", "end": "2026-04-30",
                        "label": "April 2026"},
            "selected_category": "Dining",
        }
        ChatAgent().run("u1", "hi", history=[], chart_context=ctx)
        first_call = client.calls[0]
        sys_msgs = [m for m in first_call["messages"] if m["role"] == "system"]
        joined = "\n".join(m["content"] for m in sys_msgs)
        assert "April 2026" in joined
        assert "Dining" in joined
        assert "spending" in joined

    def test_low_confidence_path_no_tool_call(self, monkeypatch):
        # No chart_context; the LLM (per the prompt) should ask one question
        # rather than tool-calling. We simulate that — the agent must not
        # crash and must just return the question as text.
        completions = [_fake_completion(
            content="Happy to dig in — this month, or year to date?"
        )]
        _, calls = self._patch_client_and_dispatch(monkeypatch, completions)
        reply = ChatAgent().run("u1", "how am I doing on travel?",
                                  history=[], chart_context=None)
        assert "this month" in reply.text
        assert calls == []

    def test_tools_passed_to_llm(self, monkeypatch):
        completions = [_fake_completion(content="ok")]
        client, _ = self._patch_client_and_dispatch(monkeypatch, completions)
        ChatAgent().run("u1", "hi", history=[], chart_context=None)
        first_call = client.calls[0]
        tools = first_call["tools"]
        names = [t["function"]["name"] for t in tools]
        assert "query_spending_breakdown" in names
        assert "category_trend" in names

    def test_trace_dict_populated(self, monkeypatch):
        # Verify a caller-supplied trace dict gets the full record per
        # iteration — what /chat/debug returns to the client.
        completions = [
            _fake_completion(tool_calls=[
                _fake_tool_call("c1", "list_categories", '{"start":"2026-01-01","end":"2026-04-30"}'),
            ]),
            _fake_completion(content="Got 4 categories."),
        ]
        _, _ = self._patch_client_and_dispatch(
            monkeypatch, completions,
            dispatch_return={"categories": [{"name": "Dining"}]},
        )
        trace: dict = {}
        ChatAgent().run("u1", "what categories?",
                          history=[], chart_context=None, trace=trace)
        assert trace["user_id"] == "u1"
        assert trace["user_message"] == "what categories?"
        assert trace["stopped"] == "final_reply"
        assert len(trace["iterations"]) == 2
        first = trace["iterations"][0]
        assert len(first["tool_calls"]) == 1
        assert first["tool_calls"][0]["name"] == "list_categories"
        assert first["tool_calls"][0]["arguments"] == {"start": "2026-01-01", "end": "2026-04-30"}
        assert "result" in first["tool_calls"][0]
        # Second iteration should be the final reply.
        second = trace["iterations"][1]
        assert second["response"]["content"] == "Got 4 categories."
        assert trace["reply"]["text"] == "Got 4 categories."

    def test_llm_failure_returns_warm_fallback(self, monkeypatch):
        class _BrokenClient:
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(
                    create=self._raise))

            def _raise(self, **_):
                raise RuntimeError("upstream 503")

        monkeypatch.setattr(chat_agent, "_client", _BrokenClient)
        monkeypatch.setattr(chat_agent, "_model", lambda: "deepseek-chat")
        reply = ChatAgent().run("u1", "hi", history=[], chart_context=None)
        # Warm fallback, not a stack trace.
        assert "hiccupped" in reply.text or "tangled" in reply.text
