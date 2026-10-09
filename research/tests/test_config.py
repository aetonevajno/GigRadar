import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class ResearchConfigTests(unittest.TestCase):
    def test_collector_loads_local_env_and_respects_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DATABASE_URL=postgresql://from-file\n"
                "TIMEPAD_API_TOKEN=from-file-timepad\n"
                "TELEGRAM_BOT_TOKEN=from-file-telegram\n",
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ, {"TIMEPAD_API_TOKEN": "explicit-timepad"}, clear=True
                ),
                patch.object(config, "_ENV_FILE", env_file),
            ):
                config.load_environment()
                self.assertEqual(config.database_url(), "postgresql://from-file")
                self.assertEqual(config.timepad_api_token(), "explicit-timepad")
                self.assertEqual(config.telegram_bot_token(), "from-file-telegram")


if __name__ == "__main__":
    unittest.main()
