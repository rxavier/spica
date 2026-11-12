"""The admin password form and its cookie."""

from __future__ import annotations


from .. import config
from .. import auth
from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from .shared import (
    templates,
)

router = APIRouter()


@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/admin/login")
async def login(request: Request, password: str = Form(...)):
    if not auth.check_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Contraseña incorrecta"}, status_code=401
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        auth.COOKIE, auth.issue(), httponly=True, samesite="lax", max_age=config.SESSION_HOURS * 3600
    )
    return response


@router.post("/admin/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response
