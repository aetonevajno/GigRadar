from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

AUTHORIZATION_URL = "https://oauth.telegram.org/auth"
TOKEN_URL = "https://oauth.telegram.org/token"
ISSUER = "https://oauth.telegram.org"
JWKS_CLIENT = PyJWKClient(
    "https://oauth.telegram.org/.well-known/jwks.json", timeout=5, lifespan=300
)


class WebAuthError(Exception):
    pass


class ProviderUnavailable(WebAuthError):
    pass


class InvalidIdentity(WebAuthError):
    pass


@dataclass(frozen=True)
class OidcSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    web_origin: str


@dataclass(frozen=True)
class WebIdentity:
    telegram_id: int
    display_name: str
    username: str | None


def settings() -> OidcSettings | None:
    client_id = os.environ.get("TELEGRAM_OIDC_CLIENT_ID", "")
    client_secret = os.environ.get("TELEGRAM_OIDC_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("TELEGRAM_OIDC_REDIRECT_URI", "")
    if not any((client_id, client_secret, redirect_uri)):
        return None
    parsed = urlsplit(redirect_uri)
    if (
        not all((client_id, client_secret, redirect_uri))
        or not client_id.isdigit()
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/auth/web/callback"
    ):
        raise ValueError("Telegram OIDC configuration is incomplete or invalid")
    return OidcSettings(
        client_id,
        client_secret,
        redirect_uri,
        f"{parsed.scheme}://{parsed.netloc}",
    )


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def valid_token(token: str) -> bool:
    return len(token) == 64 and all(
        character in "0123456789abcdef" for character in token
    )


def csrf_token(session_token: str) -> str:
    return hmac.new(
        session_token.encode("ascii"), b"gigradar-web-csrf", hashlib.sha256
    ).hexdigest()


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(
    config: OidcSettings, state: str, verifier: str, nonce: str
) -> str:
    parameters = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": "openid profile",
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        "nonce": nonce,
    }
    return f"{AUTHORIZATION_URL}?{urlencode(parameters)}"


def exchange_code(config: OidcSettings, code: str, verifier: str) -> tuple[str, str]:
    try:
        response = httpx.post(
            TOKEN_URL,
            auth=(config.client_id, config.client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "code_verifier": verifier,
            },
            timeout=5,
        )
    except httpx.RequestError as exc:
        raise ProviderUnavailable("Telegram token endpoint is unavailable") from exc
    if response.status_code >= 500:
        raise ProviderUnavailable("Telegram token endpoint failed")
    if response.status_code != 200:
        raise InvalidIdentity("Telegram rejected the authorization code")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderUnavailable(
            "Telegram returned an invalid token response"
        ) from exc
    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if (
        not isinstance(id_token, str)
        or not id_token
        or not isinstance(access_token, str)
        or not access_token
        or not access_token.isascii()
    ):
        raise ProviderUnavailable("Telegram token response is incomplete")
    return id_token, access_token


def verify_id_token(
    config: OidcSettings, id_token: str, access_token: str, nonce: str
) -> WebIdentity:
    try:
        key = JWKS_CLIENT.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=config.client_id,
            issuer=ISSUER,
            options={"require": ["iss", "sub", "aud", "iat", "exp"]},
        )
    except PyJWKClientConnectionError as exc:
        raise ProviderUnavailable("Telegram signing keys are unavailable") from exc
    except (PyJWKClientError, jwt.InvalidTokenError) as exc:
        raise InvalidIdentity("Telegram identity token is invalid") from exc
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise InvalidIdentity("Telegram identity nonce does not match")
    if (
        isinstance(claims.get("aud"), list)
        and len(claims["aud"]) > 1
        and claims.get("azp") != config.client_id
    ):
        raise InvalidIdentity("Telegram authorized party does not match")
    if "at_hash" in claims:
        digest = hashlib.sha256(access_token.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=")
        if not hmac.compare_digest(str(claims["at_hash"]), expected.decode("ascii")):
            raise InvalidIdentity("Telegram access token hash does not match")
    telegram_id = claims.get("id")
    display_name = claims.get("name")
    username = claims.get("preferred_username")
    if (
        type(telegram_id) is not int
        or telegram_id <= 0
        or not isinstance(display_name, str)
        or not display_name.strip()
        or (username is not None and not isinstance(username, str))
    ):
        raise InvalidIdentity("Telegram profile claims are invalid")
    return WebIdentity(telegram_id, display_name.strip(), username)


def new_token() -> str:
    return secrets.token_hex(32)
