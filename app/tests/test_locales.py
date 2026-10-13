import string
import unittest

from locales import SUPPORTED_LOCALES, translations_for


class TranslationCatalogTests(unittest.TestCase):
    def test_supported_languages_have_matching_keys_and_placeholders(self):
        self.assertEqual(SUPPORTED_LOCALES, ("ru", "en"))
        russian = translations_for("ru")
        english = translations_for("en")

        def placeholders(template):
            return {
                field for _, field, _, _ in string.Formatter().parse(template) if field
            }

        def compare(russian_value, english_value, path):
            self.assertEqual(type(russian_value), type(english_value), path)
            if isinstance(russian_value, dict):
                self.assertEqual(russian_value.keys(), english_value.keys(), path)
                for key in russian_value:
                    compare(russian_value[key], english_value[key], f"{path}.{key}")
            elif isinstance(russian_value, list):
                self.assertEqual(len(russian_value), len(english_value), path)
                for index, value in enumerate(russian_value):
                    compare(value, english_value[index], f"{path}[{index}]")
            elif isinstance(russian_value, str):
                self.assertEqual(
                    placeholders(russian_value), placeholders(english_value), path
                )

        compare(russian, english, "locale")
        self.assertEqual(
            [city["value"] for city in russian["web"]["cities"]],
            [city["value"] for city in english["web"]["cities"]],
        )
        expected_fields = {
            "http": {"status"},
            "openConcert": {"title"},
            "updatedAt": {"date"},
            "pageRange": {"first", "last", "total"},
        }
        templates = {
            "http": russian["web"]["errors"]["http"],
            **russian["web"]["format"],
        }
        for key, fields in expected_fields.items():
            self.assertEqual(placeholders(templates[key]), fields)

    def test_unsupported_language_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported locale"):
            translations_for("de")
