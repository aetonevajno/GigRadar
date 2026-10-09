import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config


class AppConfigTests(unittest.TestCase):
    def test_local_env_loads_without_overriding_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DATABASE_URL=postgresql://from-file\n"
                "TELEGRAM_BOT_TOKEN=from-file-token\n"
                "TELEGRAM_OIDC_CLIENT_SECRET=from-file-secret\n",
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ, {"DATABASE_URL": "postgresql://explicit"}, clear=True
                ),
                patch.object(config, "_ENV_FILE", env_file),
            ):
                config.load_environment()
                self.assertEqual(config.database_url(), "postgresql://explicit")
                self.assertEqual(config.telegram_bot_token(), "from-file-token")
                self.assertEqual(config.oidc_client_secret(), "from-file-secret")

    def test_database_url_is_required_when_file_and_environment_are_missing(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
            patch.object(config, "_ENV_FILE", Path(directory) / "missing.env"),
        ):
            config.load_environment()
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is required"):
                config.database_url()


if __name__ == "__main__":
    unittest.main()
