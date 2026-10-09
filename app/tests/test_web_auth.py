import hashlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.web_auth import (
    InvalidIdentity,
    OidcSettings,
    ProviderUnavailable,
    WebIdentity,
    authorization_url,
    code_challenge,
    exchange_code,
    settings,
    verify_id_token,
)


class WebAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.settings = OidcSettings(
            "12345",
            "test-secret",
            "https://concerts.test/api/auth/web/callback",
            "https://concerts.test",
        )

    def signed_token(self, **changes):
        now = datetime.now(timezone.utc)
        claims = {
            "iss": "https://oauth.telegram.org",
            "aud": "12345",
            "sub": "telegram-subject",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "nonce": "expected-nonce",
            "id": 123456,
            "name": "Тестовый слушатель",
            "preferred_username": "listener",
        }
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test"})

    def test_authorization_url_uses_pkce_and_minimal_scope(self):
        from urllib.parse import parse_qs, urlsplit

        url = authorization_url(self.settings, "state", "verifier", "nonce")
        parameters = parse_qs(urlsplit(url).query)
        self.assertEqual(parameters["scope"], ["openid profile"])
        self.assertEqual(parameters["code_challenge"], [code_challenge("verifier")])
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertEqual(parameters["nonce"], ["nonce"])

    def test_signed_identity_and_claims_are_checked(self):
        with patch(
            "app.web_auth.JWKS_CLIENT.get_signing_key_from_jwt",
            return_value=SimpleNamespace(key=self.key.public_key()),
        ):
            identity = verify_id_token(
                self.settings, self.signed_token(), "access-token", "expected-nonce"
            )
            self.assertEqual(
                identity, WebIdentity(123456, "Тестовый слушатель", "listener")
            )
            for claims in (
                {"aud": "other-bot"},
                {"iss": "https://another-provider.test"},
                {
                    "exp": int(
                        (datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()
                    )
                },
                {"nonce": "wrong"},
                {"id": True},
                {"aud": ["12345", "other-bot"], "azp": "other-bot"},
                {"at_hash": hashlib.sha256(b"wrong-token").hexdigest()},
            ):
                with self.subTest(claims=claims), self.assertRaises(InvalidIdentity):
                    verify_id_token(
                        self.settings,
                        self.signed_token(**claims),
                        "access-token",
                        "expected-nonce",
                    )

    def test_signature_is_checked(self):
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode(
            jwt.decode(self.signed_token(), options={"verify_signature": False}),
            other_key,
            algorithm="RS256",
            headers={"kid": "test"},
        )
        with (
            patch(
                "app.web_auth.JWKS_CLIENT.get_signing_key_from_jwt",
                return_value=SimpleNamespace(key=self.key.public_key()),
            ),
            self.assertRaises(InvalidIdentity),
        ):
            verify_id_token(self.settings, token, "access-token", "expected-nonce")

    def test_code_exchange_uses_client_secret_and_verifier(self):
        with patch(
            "app.web_auth.httpx.post",
            return_value=httpx.Response(
                200, json={"id_token": "signed", "access_token": "access"}
            ),
        ) as post:
            self.assertEqual(
                exchange_code(self.settings, "approved", "verifier"),
                ("signed", "access"),
            )
        self.assertEqual(post.call_args.kwargs["auth"], ("12345", "test-secret"))
        self.assertEqual(post.call_args.kwargs["data"]["code_verifier"], "verifier")
        self.assertEqual(
            post.call_args.kwargs["data"]["redirect_uri"],
            self.settings.redirect_uri,
        )
        with (
            patch("app.web_auth.httpx.post", return_value=httpx.Response(401)),
            self.assertRaises(InvalidIdentity),
        ):
            exchange_code(self.settings, "rejected", "verifier")
        with (
            patch("app.web_auth.httpx.post", side_effect=httpx.ConnectError("offline")),
            self.assertRaises(ProviderUnavailable),
        ):
            exchange_code(self.settings, "approved", "verifier")

    def test_configuration_requires_https_registered_callback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(settings())
        with (
            patch.dict(os.environ, {"TELEGRAM_OIDC_CLIENT_ID": "12345"}, clear=True),
            self.assertRaises(ValueError),
        ):
            settings()
        with (
            patch.dict(
                os.environ,
                {
                    "TELEGRAM_OIDC_CLIENT_ID": "12345",
                    "TELEGRAM_OIDC_CLIENT_SECRET": "secret",
                    "TELEGRAM_OIDC_REDIRECT_URI": "http://concerts.test/api/auth/web/callback",
                },
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            settings()


if __name__ == "__main__":
    unittest.main()
