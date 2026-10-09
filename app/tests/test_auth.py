import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from app.auth import InvalidInitData, verify_init_data


def signed_data(user_id: int, token: str, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "user": json.dumps(
            {"id": user_id, "first_name": "Тест"}, separators=(",", ":")
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TelegramAuthTests(unittest.TestCase):
    def test_accepts_signed_current_data(self):
        raw = signed_data(123, "token", 1000)
        self.assertEqual(verify_init_data(raw, "token", now=1000).telegram_id, 123)

    def test_rejects_tampered_expired_and_duplicate_fields(self):
        raw = signed_data(123, "token", 1000)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw.replace("123", "124"), "token", now=1000)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw, "token", now=1000 + 86401)
        with self.assertRaises(InvalidInitData):
            verify_init_data(raw + "&auth_date=1000", "token", now=1000)


if __name__ == "__main__":
    unittest.main()
