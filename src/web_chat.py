import base64
import dataclasses
import os
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

load_dotenv()

app = FastAPI()
_TEMPLATES = Path(__file__).parent / "templates"

# Jinja2 environment so templates that use {% include %} and {{ vars }} render
# properly (the frontend dashboard pulls in _user_menu.html / _chat_drawer.html
# via include, and the settings pages interpolate the user config).
_templates = Jinja2Templates(directory=str(_TEMPLATES))


# --- Dashboard helpers (Phase 1B) -------------------------------------------


def _resolve_user_id() -> str:
    """Slugified user_id used in the transactions table.

    Mirror the convention from statement_ingester: the user's first name,
    lowercased, with spaces stripped. We keep this in one place so all the
    new dashboard endpoints agree.
    """
    from src.storage import UserConfigStore
    cfg = UserConfigStore.load()
    name = (cfg.name or "default").strip().split()[0] if cfg.name else "default"
    return name.lower()


def _parse_period_qs(period: Optional[str], start: Optional[str], end: Optional[str]):
    """Wrap parse_period with HTTP-friendly error handling."""
    from src.dashboard_queries import parse_period
    try:
        return parse_period(period, start, end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _period_key(period: Optional[str], start_d, end_d) -> str:
    """Stable cache key string for an annotation's (chart, period)."""
    if period:
        return period
    return f"range:{start_d.isoformat()}..{end_d.isoformat()}"


def _render_template_or_503(
    name: str,
    request: Request,
    context: Optional[dict] = None,
    fallback_html: str = "",
) -> HTMLResponse:
    """Render a template through Jinja2, or return a 503 placeholder if missing.

    Rendering through Jinja2 (not raw read_text) means {% include %} expands and
    {{ vars }} interpolate. We keep the graceful "templates pending" fallback
    only for files that are genuinely absent.
    """
    path = _TEMPLATES / name
    if path.exists():
        ctx = dict(context) if context else {}
        return _templates.TemplateResponse(request, name, ctx)
    # TODO: remove once the frontend agent ships {name}.
    body = fallback_html or (
        f"<!doctype html><title>{name}</title>"
        f"<h1>templates pending</h1>"
        f"<p>{name} is not yet built. Check back shortly.</p>"
    )
    return HTMLResponse(body, status_code=503)


@app.get("/", response_class=HTMLResponse)
async def index():
    from src.storage import UserConfigStore
    if UserConfigStore.is_complete():
        return (_TEMPLATES / "chat.html").read_text(encoding="utf-8")
    return (_TEMPLATES / "onboarding.html").read_text(encoding="utf-8")


@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    return (_TEMPLATES / "chat.html").read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    # Rendered through Jinja2 so {% include "_user_menu.html" %} and
    # {% include "_chat_drawer.html" %} expand into the page.
    from src.storage import UserConfigStore
    return _render_template_or_503(
        "dashboard.html", request, {"user": UserConfigStore.load()}
    )


@app.get("/onboard", response_class=HTMLResponse)
async def onboarding_page():
    return (_TEMPLATES / "onboarding.html").read_text(encoding="utf-8")


@app.get("/config")
async def get_config():
    from src.storage import UserConfigStore
    return JSONResponse(dataclasses.asdict(UserConfigStore.load()))


@app.post("/config")
async def update_config(body: dict):
    from src.storage import UserConfigStore
    config = UserConfigStore.load()
    for key, value in body.items():
        if hasattr(config, key):
            setattr(config, key, value)
    UserConfigStore.save(config)
    return JSONResponse({"ok": True})


@app.post("/onboard")
async def complete_onboard(body: dict):
    from src.storage import UserConfigStore, UserConfig
    config = UserConfig(
        name=body.get("name", ""),
        finance_profile=body.get("finance_profile", ""),
        custom_profile=body.get("custom_profile", ""),
        goal_type=body.get("goal_type", ""),
        goal_label=body.get("goal_label", ""),
        goal_monthly_target=body.get("goal_monthly_target"),
        intentions=body.get("intentions", []),
        onboarding_complete=True,
    )
    UserConfigStore.save(config)
    return JSONResponse({"ok": True})


# --- Dashboard JSON endpoints (Phase 1B) ------------------------------------
#
# The old `/dashboard/data` route had no external consumer; it's been replaced
# by the per-chart endpoints below. The new frontend reads each chart's data
# from its own URL.


@app.get("/dashboard/spending")
async def dashboard_spending(
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    category: Optional[str] = None,  # comma-separated; currently informational
    account: Optional[str] = None,
):
    from src.dashboard_queries import spending_breakdown
    start_d, end_d = _parse_period_qs(period, start, end)
    user_id = _resolve_user_id()
    payload = spending_breakdown(user_id, start_d, end_d, account_id=account)
    # If the caller passed a category filter, narrow the returned list (the
    # totals stay unfiltered so the donut keeps showing the whole picture).
    if category:
        wanted = {c.strip() for c in category.split(",") if c.strip()}
        if wanted:
            payload = {
                **payload,
                "categories": [c for c in payload["categories"] if c["name"] in wanted],
            }
    return JSONResponse(payload)


@app.get("/dashboard/income")
async def dashboard_income(
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    account: Optional[str] = None,
):
    from src.dashboard_queries import income_breakdown
    start_d, end_d = _parse_period_qs(period, start, end)
    user_id = _resolve_user_id()
    return JSONResponse(income_breakdown(user_id, start_d, end_d, account_id=account))


@app.get("/dashboard/transactions")
async def dashboard_transactions(
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    category: Optional[str] = None,
    account: Optional[str] = None,
    min: Optional[float] = Query(None),
    max: Optional[float] = Query(None),
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
):
    from src.dashboard_queries import transactions_filtered
    start_d, end_d = _parse_period_qs(period, start, end)
    user_id = _resolve_user_id()
    cats: Optional[list] = None
    if category:
        cats = [c.strip() for c in category.split(",") if c.strip()] or None
    return JSONResponse(transactions_filtered(
        user_id, start_d, end_d,
        category=cats,
        account_id=account,
        min_amount=min,
        max_amount=max,
        q=q,
        page=page,
        page_size=page_size,
    ))


@app.get("/dashboard/cashflow")
async def dashboard_cashflow(
    months: int = 12,
    account: Optional[str] = None,
):
    from src.dashboard_queries import cashflow_series
    if months < 1 or months > 36:
        raise HTTPException(status_code=400, detail="months must be between 1 and 36")
    user_id = _resolve_user_id()
    return JSONResponse(cashflow_series(user_id, months=months, account_id=account))


@app.get("/dashboard/pinned")
async def dashboard_pinned():
    """Reserved for Phase 1C. Returns this user's pinned charts (empty in 1B)."""
    from src.storage import PinnedChartStore
    return JSONResponse({"charts": PinnedChartStore.list_for_user(_resolve_user_id())})


# --- Annotation endpoints ---------------------------------------------------


_VALID_CHART_KEYS = {"spending", "income", "transactions", "cashflow"}


def _payload_for_chart(chart_key: str, period: Optional[str],
                      start: Optional[str], end: Optional[str]) -> tuple[dict, str]:
    """Compute the payload the LLM sees + its (chart, period) cache key."""
    start_d, end_d = _parse_period_qs(period, start, end)
    user_id = _resolve_user_id()
    if chart_key == "spending":
        from src.dashboard_queries import spending_breakdown
        payload = spending_breakdown(user_id, start_d, end_d)
    elif chart_key == "income":
        from src.dashboard_queries import income_breakdown
        payload = income_breakdown(user_id, start_d, end_d)
    elif chart_key == "transactions":
        from src.dashboard_queries import transactions_filtered
        # Use a compact payload for the LLM (just the page-1 summary).
        payload = transactions_filtered(user_id, start_d, end_d, page=1, page_size=50)
    elif chart_key == "cashflow":
        from src.dashboard_queries import cashflow_series
        payload = cashflow_series(user_id, months=12)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown chart_key: {chart_key}")
    period_key = _period_key(period, start_d, end_d)
    return payload, period_key


@app.get("/dashboard/insights/{chart_key}")
async def dashboard_insight_get(
    chart_key: str,
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    refresh: int = 0,
):
    if chart_key not in _VALID_CHART_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown chart_key: {chart_key}")
    payload, period_key = _payload_for_chart(chart_key, period, start, end)

    from src.dashboard_insights import get_or_generate_annotation
    from src.storage import WikiStore
    wiki_text = WikiStore.load()
    result = get_or_generate_annotation(
        user_id=_resolve_user_id(),
        chart_key=chart_key,
        period_key=period_key,
        payload=payload,
        wiki_text=wiki_text,
        force=bool(refresh),
    )
    return JSONResponse({"chart_key": chart_key, "period_key": period_key, **result})


@app.post("/dashboard/insights/{chart_key}/refresh")
async def dashboard_insight_refresh(
    chart_key: str,
    period: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    if chart_key not in _VALID_CHART_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown chart_key: {chart_key}")
    payload, period_key = _payload_for_chart(chart_key, period, start, end)

    from src.dashboard_insights import get_or_generate_annotation
    from src.storage import WikiStore
    result = get_or_generate_annotation(
        user_id=_resolve_user_id(),
        chart_key=chart_key,
        period_key=period_key,
        payload=payload,
        wiki_text=WikiStore.load(),
        force=True,
    )
    return JSONResponse({"chart_key": chart_key, "period_key": period_key, **result})


# --- Settings (HTML + goal + derived budget) --------------------------------


@app.get("/settings", response_class=HTMLResponse)
async def settings_index(request: Request):
    return _render_template_or_503("settings_index.html", request)


@app.get("/settings/profile", response_class=HTMLResponse)
async def settings_profile(request: Request):
    from src.storage import UserConfigStore
    return _render_template_or_503(
        "settings_profile.html", request, {"user": UserConfigStore.load()}
    )


@app.get("/settings/goal", response_class=HTMLResponse)
async def settings_goal_page(request: Request):
    from src.storage import UserConfigStore
    return _render_template_or_503(
        "settings_goal.html", request, {"user": UserConfigStore.load()}
    )


_VALID_GOAL_KEYS = {"", "stay_ahead_bills", "pay_off_credit", "build_credit", "custom"}


@app.post("/settings/goal")
async def settings_goal_save(body: dict):
    from src.storage import UserConfigStore
    goal_key = str(body.get("goal_key", "") or "")
    goal_text = str(body.get("goal_text", "") or "")
    if goal_key not in _VALID_GOAL_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid goal_key. Must be one of: {sorted(_VALID_GOAL_KEYS)}",
        )
    config = UserConfigStore.load()
    config.goal_key = goal_key
    config.goal_text = goal_text
    UserConfigStore.save(config)
    return JSONResponse({"ok": True, "goal_key": goal_key, "goal_text": goal_text})


@app.post("/settings/goal/budget/refresh")
async def settings_goal_budget_refresh():
    """Regenerate the LLM-derived budget guidance and persist it."""
    from src.dashboard_insights import get_or_generate_derived_budget
    from src.dashboard_queries import (
        FIXED_LOOKBACK_MONTHS,  # noqa: F401  (kept for clarity)
        _months_back,
        _spending_per_month_with_categories,
    )
    from src.storage import UserConfigStore, WikiStore
    from datetime import date

    user_id = _resolve_user_id()
    months = _months_back(date.today(), 3)
    _, per_cat = _spending_per_month_with_categories(user_id, months)
    avgs = {
        cat: float(sum(by_month.values())) / max(len(months), 1)
        for cat, by_month in per_cat.items()
    }
    # Trim to the top categories by recent spend so the LLM gets a clean prompt.
    top_avgs = dict(sorted(avgs.items(), key=lambda kv: kv[1], reverse=True)[:12])

    hints = get_or_generate_derived_budget(
        user_config=UserConfigStore.load(),
        recent_category_avgs=top_avgs,
        wiki_text=WikiStore.load(),
        force=True,
    )
    return JSONResponse({"ok": True, "derived_budget": hints})


@app.post("/settings/goal/budget/edit")
async def settings_goal_budget_edit(body: dict):
    """Persist a user's inline edit to one derived-budget hint card."""
    from src.storage import BudgetHint, UserConfigStore

    category = str(body.get("category", "") or "")
    hint_text = str(body.get("hint_text", "") or "")
    if not category:
        raise HTTPException(status_code=400, detail="category is required")

    config = UserConfigStore.load()
    for hint in config.derived_budget:
        if hint.category == category:
            hint.hint_text = hint_text
            break
    else:
        config.derived_budget.append(BudgetHint(category=category, hint_text=hint_text))
    UserConfigStore.save(config)
    return JSONResponse({"ok": True, "category": category, "hint_text": hint_text})


@app.get("/analysis/monthly")
async def monthly_analysis():
    from src.statement_ingester import ingest_statements
    from src.llm_orchestrator import generate_monthly_analysis
    from src.storage import UserConfigStore
    from datetime import datetime
    transactions = []
    try:
        transactions = ingest_statements()
    except Exception:
        pass
    config = UserConfigStore.load()
    prefs = {"goals": [{"label": config.goal_label}] if config.goal_label else []}
    narrative = generate_monthly_analysis(transactions, prefs)
    return JSONResponse({"narrative": narrative, "period": datetime.now().strftime("%Y-%m")})


@app.post("/chat")
async def chat_endpoint(
    message: str = Form(""),
    image: Optional[UploadFile] = File(None),
):
    from src.companion import Companion
    from src.statement_ingester import ingest_statements

    transactions = []
    try:
        transactions = ingest_statements()
    except Exception:
        pass

    image_b64 = None
    mime_type = "image/jpeg"
    if image and image.filename:
        data = await image.read()
        image_b64 = base64.b64encode(data).decode()
        mime_type = image.content_type or "image/jpeg"

    try:
        companion = Companion()
        response = companion.chat(message, transactions, image_b64=image_b64, mime_type=mime_type)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"response": response})


@app.get("/model")
async def model_info():
    from src.llm_orchestrator import _model
    return JSONResponse({"model": _model()})


@app.delete("/memory")
async def clear_memory():
    from src.companion import Companion
    Companion().clear_memory()
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("WEB_CHAT_PORT", "8080"))
    print(f"PennyPath dev chat → http://127.0.0.1:{port}")
    uvicorn.run("src.web_chat:app", host="127.0.0.1", port=port, reload=True)
