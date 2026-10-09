from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .session import (
    WEB_SESSION_COOKIE,
    Database,
    get_profile,
    upsert_user,
    web_session_identity,
)
from .web_auth import (
    InvalidIdentity,
    OidcSettings,
    ProviderUnavailable,
    authorization_url,
    csrf_token,
    exchange_code,
    new_token,
    settings,
    token_hash,
    valid_token,
    verify_id_token,
)

router = APIRouter(prefix="/auth/web")
LOGGER = logging.getLogger(__name__)
WEB_LOGIN_COOKIE = "gigradar_login_state"
WEB_SESSION_SECONDS = 7 * 24 * 60 * 60
WEB_LOGIN_SECONDS = 10 * 60


def _oidc_settings() -> OidcSettings:
    try:
        config = settings()
    except ValueError as exc:
        raise HTTPException(503, "Telegram web login is misconfigured") from exc
    if config is None:
        raise HTTPException(503, "Telegram web login is not configured")
    return config


@router.get("/config")
def web_login_config():
    try:
        config = settings()
    except ValueError as exc:
        raise HTTPException(503, "Telegram web login is misconfigured") from exc
    return {
        "enabled": config is not None,
        "login_url": config.redirect_uri.replace(
            "/auth/web/callback", "/auth/web/start"
        )
        if config
        else None,
    }


@router.get("/start")
def web_login_start(database: Database):
    config = _oidc_settings()
    state = new_token()
    verifier = new_token()
    nonce = new_token()
    database.execute("DELETE FROM web_login_attempts WHERE expires_at <= now()")
    database.execute(
        "INSERT INTO web_login_attempts (state_hash, code_verifier, nonce, expires_at) "
        "VALUES (%s, %s, %s, now() + interval '10 minutes')",
        (token_hash(state), verifier, nonce),
    )
    response = RedirectResponse(
        authorization_url(config, state, verifier, nonce), status_code=303
    )
    response.set_cookie(
        WEB_LOGIN_COOKIE,
        state,
        max_age=WEB_LOGIN_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _web_login_redirect(config: OidcSettings, outcome: str) -> RedirectResponse:
    response = RedirectResponse(f"{config.web_origin}/?auth={outcome}", status_code=303)
    response.delete_cookie(WEB_LOGIN_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/callback")
def web_login_callback(
    request: Request,
    database: Database,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    config = _oidc_settings()
    cookie_state = request.cookies.get(WEB_LOGIN_COOKIE, "")
    if (
        state is None
        or not valid_token(state)
        or not hmac.compare_digest(cookie_state, state)
    ):
        return _web_login_redirect(config, "failed")
    attempt = database.execute(
        "DELETE FROM web_login_attempts WHERE state_hash = %s AND expires_at > now() "
        "RETURNING code_verifier, nonce",
        (token_hash(state),),
    ).fetchone()
    if attempt is None:
        return _web_login_redirect(config, "failed")
    if error is not None:
        return _web_login_redirect(config, "cancelled")
    if not code:
        return _web_login_redirect(config, "failed")
    try:
        id_token, access_token = exchange_code(config, code, attempt[0])
        identity = verify_id_token(config, id_token, access_token, attempt[1])
    except ProviderUnavailable as exc:
        LOGGER.warning("Telegram web login provider unavailable: %s", exc)
        return _web_login_redirect(config, "unavailable")
    except InvalidIdentity as exc:
        LOGGER.warning("Telegram web login rejected: %s", exc)
        return _web_login_redirect(config, "failed")
    user = upsert_user(
        database, identity.telegram_id, identity.display_name, identity.username
    )
    session_token = new_token()
    database.execute("DELETE FROM web_sessions WHERE expires_at <= now()")
    old_session = request.cookies.get(WEB_SESSION_COOKIE, "")
    if valid_token(old_session):
        database.execute(
            "DELETE FROM web_sessions WHERE token_hash = %s", (token_hash(old_session),)
        )
    database.execute(
        "INSERT INTO web_sessions (token_hash, user_id, expires_at) "
        "VALUES (%s, %s, now() + interval '7 days')",
        (token_hash(session_token), user["id"]),
    )
    response = RedirectResponse(f"{config.web_origin}/", status_code=303)
    response.delete_cookie(WEB_LOGIN_COOKIE, path="/")
    response.set_cookie(
        WEB_SESSION_COOKIE,
        session_token,
        max_age=WEB_SESSION_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/session")
def web_session(request: Request, response: Response, database: Database):
    user_id, session_token = web_session_identity(request, database)
    response.headers["Cache-Control"] = "no-store"
    return {
        "profile": get_profile(database, user_id),
        "csrf_token": csrf_token(session_token),
    }


@router.post("/logout")
def web_logout(request: Request, database: Database):
    _, session_token = web_session_identity(request, database)
    database.execute(
        "DELETE FROM web_sessions WHERE token_hash = %s", (token_hash(session_token),)
    )
    response = JSONResponse({"signed_out": True})
    response.delete_cookie(WEB_SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response
