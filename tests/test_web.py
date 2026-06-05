import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from src.web_chat import app

client = TestClient(app)


class TestRootRoute:
    def test_get_root_returns_200_unauthenticated(self):
        """GET / returns 200; serves onboarding since config is not complete."""
        resp = client.get("/")
        assert resp.status_code == 200

    def test_get_onboard_returns_200(self):
        resp = client.get("/onboard")
        assert resp.status_code == 200


class TestOnboarding:
    def test_post_onboard_returns_ok(self):
        payload = {
            "name": "Alex",
            "finance_profile": "early_career",
            "goal_type": "emergency_fund",
            "goal_label": "Emergency fund",
            "intentions": ["spend less on dining"],
        }
        resp = client.post("/onboard", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_get_config_after_onboard_has_name(self):
        payload = {
            "name": "Alex",
            "finance_profile": "early_career",
            "goal_type": "emergency_fund",
            "goal_label": "Emergency fund",
            "intentions": ["spend less on dining"],
        }
        client.post("/onboard", json=payload)
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Alex"

    def test_root_serves_chat_after_onboarding(self):
        """After onboarding completes, GET / should return chat page with 'PennyPath'."""
        payload = {
            "name": "Alex",
            "finance_profile": "early_career",
            "goal_type": "emergency_fund",
            "goal_label": "Emergency fund",
            "intentions": ["spend less on dining"],
        }
        client.post("/onboard", json=payload)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "PennyPath" in resp.text


class TestDashboardData:
    def test_spending_endpoint_returns_required_keys(self):
        resp = client.get("/dashboard/spending?period=2026-05")
        assert resp.status_code == 200
        data = resp.json()
        assert "period" in data
        assert "total_spend" in data
        assert "categories" in data
        assert isinstance(data["categories"], list)


class TestChatEndpoint:
    def test_post_chat_returns_response(self):
        # The /chat route calls ingest_statements() before chatting; mock it so
        # the test doesn't re-parse real PDFs through the live LLM (which made
        # this case hang for ~17 min). We're unit-testing the route, not ingest.
        # Companion.chat now returns a ChatReply (Phase 1C).
        from src.chat_agent import ChatReply
        with patch("src.statement_ingester.ingest_statements", return_value=[]), \
             patch("src.companion.Companion.chat", return_value=ChatReply(text="Hey!")):
            resp = client.post("/chat", data={"message": "hello"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "Hey!"
        assert body["text"] == "Hey!"
        assert "blocks" not in body  # empty blocks list is omitted

    def test_post_chat_with_blocks(self):
        from src.chat_agent import ChatReply
        reply = ChatReply(text="Top spots:", blocks=[{
            "type": "table",
            "title": "X",
            "columns": ["a"],
            "rows": [["b"]],
        }])
        with patch("src.statement_ingester.ingest_statements", return_value=[]), \
             patch("src.companion.Companion.chat", return_value=reply):
            resp = client.post("/chat", data={"message": "break down dining"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "Top spots:"
        assert body["blocks"][0]["type"] == "table"

    def test_chat_tools_debug_endpoint(self):
        resp = client.get("/chat/tools")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "query_spending_breakdown" in names
        assert "category_trend" in names
        # 9 read tools + 9 override / rule tools (Phase 1C).
        assert "create_category_rule" in names
        assert "list_override_history" in names
        assert len(names) == 18


class TestDeleteMemory:
    def test_delete_memory_returns_ok(self):
        resp = client.delete("/memory")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class TestModelRoute:
    def test_get_model_returns_model_key(self):
        resp = client.get("/model")
        assert resp.status_code == 200
        assert "model" in resp.json()
