from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from auth import fetch_user, get_current_user_optional, verify_password
from flash import base_context, set_flash
from web import templates

router = APIRouter(tags=["auth"])


@router.get("/login")
def login_form(request: Request):
    if get_current_user_optional(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", base_context(request))


@router.post("/login")
def login(request: Request, account: str = Form(...), password: str = Form(...)):
    user = fetch_user(account)
    if not user or not user["is_active"] or not verify_password(password, user["password_hash"]):
        set_flash(request, "danger", "Invalid credentials.")
        return RedirectResponse("/login", status_code=303)

    request.session["account"] = user["account"]
    request.session["session_version"] = user["session_version"]
    set_flash(request, "success", "Logged in successfully.")
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
