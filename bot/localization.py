from locales import translations_for


def messages_for(locale: str) -> dict[str, str]:
    return translations_for(locale)["bot"]
