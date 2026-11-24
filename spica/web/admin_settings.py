"""The model endpoint and the DJ's global prompt."""

from __future__ import annotations

import time
import httpx

from .. import config
from .. import picker
from .. import plan
from .. import auth
from ..picker import prompt as prompt_mod
from fastapi import APIRouter, Form, HTTPException, Request, Depends
from fastapi.responses import (
    HTMLResponse,
    Response,
)

from .shared import (
    templates,
)

router = APIRouter(dependencies=[Depends(auth.require_admin_page)])


def _parse_headers(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, value = line.split(sep, 1)
                if key.strip():
                    out[key.strip()] = value.strip()
                break
    return out


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    llm = config.llm_settings()
    providers = config.provider_settings()
    mode = config.dj_mode()
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "llm": llm,
            "key_set": bool(llm["key"]),
            "headers_text": "\n".join(f"{k}={v}" for k, v in llm["headers"].items()),
            "providers": providers,
            "lastfm_set": bool(providers["lastfm_key"]),
            "mb_default": config.MB_USER_AGENT,
            "dj_mode": mode,
            "mode_label": config.DJ_MODE_LABELS.get(mode, mode),
            "has_model": bool(llm["base"] and llm["key"]),
        },
    )


@router.post("/admin/settings", response_class=HTMLResponse)
async def admin_settings_save(
    request: Request,
    base: str = Form(""),
    key: str = Form(""),
    model: str = Form(""),
    headers: str = Form(""),
    clear_key: str = Form(""),
):
    current = config.llm_settings()
    # An empty key field means "leave it alone"; the explicit checkbox clears it.
    new_key = "" if clear_key else (key.strip() or current["key"])
    config.save_llm_settings(base, new_key, model, _parse_headers(headers))
    return templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": "Saved. The next pick uses it — no restart.", "station_id": ""},
    )


@router.post("/admin/settings/dj", response_class=HTMLResponse)
async def admin_settings_dj(request: Request, mode: str = Form(...)):
    try:
        chosen = config.save_dj_mode(mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # A set already waiting was chosen under the old mode; drop it so the next
    # pick reflects what the operator just chose.
    for station in config.load_stations():
        plan.clear(station, "DJ mode changed")
    response = templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": f"DJ mode is now {config.DJ_MODE_LABELS.get(chosen, chosen)}.", "station_id": ""},
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/admin/settings/providers", response_class=HTMLResponse)
async def admin_settings_providers(
    request: Request,
    lastfm_key: str = Form(""),
    mb_user_agent: str = Form(""),
    clear_lastfm: str = Form(""),
):
    current = config.provider_settings()
    new_key = "" if clear_lastfm else (lastfm_key.strip() or current["lastfm_key"])
    config.save_provider_settings(new_key, mb_user_agent.strip())
    response = templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": "Providers saved. The next job and pick use them — no restart.", "station_id": ""},
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/admin/settings/test", response_class=HTMLResponse)
async def admin_settings_test(
    request: Request,
    base: str = Form(""),
    key: str = Form(""),
    model: str = Form(""),
    headers: str = Form(""),
):
    """Ask the endpoint for one tiny completion, with a tool, and report what came back.

    The fields come from the form beside the button, so this tests what is on screen
    rather than what was last saved — testing the old values is how a typo in a base
    URL survives a green tick.
    """
    saved = config.llm_settings()
    llm = {
        "base": (base or "").strip().rstrip("/") or saved["base"],
        "key": (key or "").strip() or saved["key"],
        "model": (model or "").strip() or saved["model"],
        "headers": _parse_headers(headers) if headers.strip() else saved["headers"],
    }
    result = {"ok": False, "base": llm["base"], "model": llm["model"], "detail": "", "tools": None}
    if not llm["base"] or not llm["key"]:
        result["detail"] = "No endpoint or key to test — fill them in or save them first."
        return templates.TemplateResponse(request, "_llm_test.html", {"result": result})
    headers = {
        "Authorization": f"Bearer {llm['key']}",
        "content-type": "application/json",
        **llm["headers"],
    }
    body = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": "Call the tool with the number 7."}],
        "max_tokens": 64,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Echo a number back.",
                    "parameters": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                    },
                },
            }
        ],
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(f"{llm['base']}/chat/completions", headers=headers, json=body)
        result["status"] = response.status_code
        result["ms"] = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            result["detail"] = f"HTTP {response.status_code}: {response.text[:300]}"
        else:
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            result["tools"] = bool(calls)
            result["reply"] = (message.get("content") or "").strip()[:200]
            result["served_by"] = data.get("model") or ""
            result["ok"] = True
            result["detail"] = (
                "Answered, and called the tool." if calls
                else "Answered, but did not call the tool — picking needs tool calling."
            )
    except Exception as exc:  # network, DNS, timeout, bad JSON
        result["ms"] = int((time.monotonic() - started) * 1000)
        result["detail"] = f"{type(exc).__name__}: {exc}"[:300]
    return templates.TemplateResponse(request, "_llm_test.html", {"result": result})


@router.get("/admin/prompt", response_class=HTMLResponse)
async def admin_prompt(request: Request):
    try:
        text = config.DJ_PERSONA_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    try:
        stations = config.load_stations()
    except Exception:
        stations = []
    wanted = (request.query_params.get("station") or "").strip()
    selected = next((st for st in stations if st.id == wanted), stations[0] if stations else None)
    table_prompt = shelf_prompt = ""
    if selected is not None:
        try:
            from .. import store
            from ..picker import prompt as prompt_mod

            roster = store.library().roster(selected)
            table_prompt = prompt_mod.system_prompt(selected, roster, table=True)
            shelf_prompt = prompt_mod.system_prompt(selected, roster, table=False)
        except Exception:
            pass
    return templates.TemplateResponse(
        request,
        "admin_prompt.html",
        {
            "text": text.strip() or picker.DEFAULT_PERSONA,
            "default": picker.DEFAULT_PERSONA,
            "custom": bool(text.strip()),
            "stations": stations,
            "selected": selected,
            "table_prompt": table_prompt,
            "shelf_prompt": shelf_prompt,
            "dj_mode": config.dj_mode(),
            "mode_label": config.DJ_MODE_LABELS.get(config.dj_mode(), config.dj_mode()),
            "shelf_text": prompt_mod.shelf_guidance(),
            "table_text": prompt_mod.table_guidance(),
            "shelf_custom": config.dj_shelf_file().exists(),
            "table_custom": config.dj_table_file().exists(),
        },
    )


@router.post("/admin/prompt", response_class=HTMLResponse)
async def admin_prompt_save(request: Request, text: str = Form("")):
    config.DJ_PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.DJ_PERSONA_FILE.write_text(text, encoding="utf-8")
    response = templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": "Persona saved — the next pick uses it", "station_id": ""},
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/admin/prompt/path", response_class=HTMLResponse)
async def admin_prompt_path(
    request: Request,
    kind: str = Form(...),
    text: str = Form(""),
    reset: str = Form(""),
):
    path = {"shelf": config.dj_shelf_file, "table": config.dj_table_file}.get(kind)
    if path is None:
        raise HTTPException(400, "unknown path")
    target = path()
    if reset:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        message = f"{kind} path restored to the built-in text"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.strip() + "\n", encoding="utf-8")
        message = f"{kind} path saved — the next pick uses it"
    response = templates.TemplateResponse(
        request, "_saved.html", {"message": message, "station_id": ""}
    )
    response.headers["HX-Refresh"] = "true"
    return response


@router.post("/admin/prompt/reset", response_class=HTMLResponse)
async def admin_prompt_reset(request: Request):
    try:
        config.DJ_PERSONA_FILE.unlink()
    except FileNotFoundError:
        pass
    # The textarea, the "no custom prompt" note and the custom flag all come from
    # the server; reloading is one header and leaves nothing half-updated. The old
    # answer was a whole page swapped into <body>, which put a second <head>, a
    # second player.js and a second <audio> element inside the live document.
    response = Response(status_code=204)
    response.headers["HX-Refresh"] = "true"
    return response
