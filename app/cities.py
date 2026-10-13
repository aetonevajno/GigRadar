CITY_NAMES = {"msk": "Москва", "spb": "Санкт-Петербург"}
SUPPORTED_CITIES = frozenset(CITY_NAMES.values())


def canonical_city(value: str) -> str | None:
    return next(
        (
            city
            for city in SUPPORTED_CITIES
            if city.casefold() == value.strip().casefold()
        ),
        None,
    )
