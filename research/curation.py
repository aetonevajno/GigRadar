from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Literal

from normalize import Event

ConcertKind = Literal["concert", "not_concert", "unclear"]


@dataclass(frozen=True)
class Classification:
    kind: ConcertKind
    reason: str


@dataclass(frozen=True)
class ArtistMention:
    name: str
    field: str
    evidence: str
    confidence: Literal["high", "review"]


@dataclass(frozen=True)
class DuplicateAssessment:
    decision: Literal["merge", "review", "distinct"]
    reason: str


def plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def _normalized(value: str) -> str:
    return re.sub(r"[^\w]+", " ", plain_text(value).casefold()).strip()


def classify(event: Event) -> Classification:
    title = _normalized(event.title)
    description = _normalized(str(event.payload.get("description_short") or ""))
    body = f"{title} {description}"
    if not title or not event.city or not event.starts_at:
        return Classification("unclear", "missing_core_fields")
    if re.search(r"\bабонемент\b", title):
        return Classification("unclear", "multi_date_pass")
    if re.search(r"\b(стендап|standup|stand up|квн|комики|импров шоу|квиз)\b", title):
        if re.search(r"\b(концерт|квартирник)\b", title):
            return Classification("unclear", "mixed_program")
        return Classification("not_concert", "non_music_title")
    if re.search(r"\bбалет\b", title) and not re.search(r"\bконцерт\w*\b", title):
        return Classification("unclear", "stage_performance")
    if re.search(r"\b(литературн|поэтическ|спектакл|лекци|викторин)\w*\b", title):
        if "музыкальная лекция" in title and "выступление" in description:
            return Classification("concert", "lecture_with_live_performance")
        if not re.search(r"\b(лекция концерт|концерт|концерта)\b", title):
            if re.search(
                r"\b(литературн|поэтическ|викторин)\w*\b", title
            ) and re.search(r"\b(музык|песн|джаз)\w*\b", body):
                return Classification("unclear", "mixed_program")
            return Classification("not_concert", "non_music_title")
    if re.search(r"\b(открытый джем|совместная импровизационная игра)\b", body):
        return Classification("not_concert", "participant_jam")
    if "открытый микрофон" in description and "сами участники" in description:
        return Classification("unclear", "participant_performance")
    if re.search(r"\b(концерт\w*|квартирник\w*)\b", title):
        if re.search(r"\b(квиз|викторина|шоу)\b", title) and not re.search(
            r"\b(жив|музык|песн)\w*\b", body
        ):
            return Classification("unclear", "mixed_program")
        return Classification("concert", "concert_title")
    if re.search(
        r"\b(концерт\w*|живой музыкой|живой голос|выступят|выступит|играют|музыкальный вечер|на сцене)\b",
        description,
    ):
        if re.search(r"\b(литературн|поэтическ|викторин)\w*\b", body):
            return Classification("unclear", "mixed_program")
        return Classification("concert", "live_music_description")
    if re.search(
        r"\b(джаз|рок|блюз|фортепиан\w*|хоровая музыка|романса|сольный)\b", title
    ) and re.search(
        r"\b(исполнени\w*|солист\w*|музыкант\w*|певиц\w*|пианист\w*|оркестр\w*|сцен\w*|звучит)\b",
        description,
    ):
        return Classification("concert", "music_performance")
    return Classification("unclear", "insufficient_evidence")


_NAME = r"([A-ZА-ЯЁ][\w-]+(?:\s+[A-ZА-ЯЁ][\w-]+){0,5})"
_ROLE_PATTERNS = (
    re.compile(rf"\b(?i:группы|группа|ансамбль|оркестр|хор|дуэт)\s+[«\"\[]?{_NAME}"),
    re.compile(rf"\b(?i:выступит|выступят|играет|поёт|исполняет)\s+[«\"\[]?{_NAME}"),
    re.compile(rf"\b(?i:автор-исполнитель|музыкант|певица|пианист)\s+[«\"\[]?{_NAME}"),
)
_TITLE_PATTERNS = (
    re.compile(
        rf"\b(?i:концерт)\s+(?i:группы|ансамбля|оркестра|хора|дуэта)\s+[«\"\[]?{_NAME}"
    ),
    re.compile(rf"\b(?i:концерт|квартирник)\s+[«\"\[]?{_NAME}"),
)
_STOP_NAMES = {
    "при свечах",
    "старинной музыки",
    "камерной музыки",
    "классической музыки",
    "русской музыки",
    "новой программы",
    "музыкальной программы",
    "искусство хорала",
    "звезда любви",
    "космос внутри",
}


def artist_mentions(event: Event) -> tuple[ArtistMention, ...]:
    mentions: list[ArtistMention] = []
    for name in event.artist_candidates:
        mentions.append(ArtistMention(plain_text(name), "participants", name, "high"))
    for field, content, patterns in (
        ("title", plain_text(event.title), _TITLE_PATTERNS),
        (
            "description_short",
            plain_text(str(event.payload.get("description_short") or "")),
            _ROLE_PATTERNS,
        ),
    ):
        for pattern in patterns:
            for match in pattern.finditer(content):
                name = match.group(1).strip(' «»"[]-.,')
                if (
                    len(name) < 3
                    or name.casefold() in _STOP_NAMES
                    or name.casefold() in {"группы", "музыки"}
                ):
                    continue
                prefix = content[max(0, match.start() - 14) : match.start()].casefold()
                wider_prefix = content[
                    max(0, match.start() - 60) : match.start()
                ].casefold()
                suffix = content[match.end() : match.end() + 25]
                if "танцевальн" in prefix:
                    continue
                confidence = (
                    "high"
                    if field == "description_short"
                    or re.search(
                        r"(?:группы|ансамбля|оркестра|хора|дуэта)",
                        match.group(0),
                        re.IGNORECASE,
                    )
                    else "review"
                )
                if re.match(r"\s+[а-яё]", suffix) and suffix[:1] not in '»"':
                    confidence = "review"
                if field == "description_short" and re.search(
                    r"\b(альбомов|соавтор|продюсер|бывш|экс)\w*\b", wider_prefix
                ):
                    confidence = "review"
                mentions.append(ArtistMention(name, field, match.group(0), confidence))
    return tuple(dict.fromkeys(mentions))


def assess_duplicate(
    first: Event, second: Event, first_artists: set[str], second_artists: set[str]
) -> DuplicateAssessment:
    if (first.source, first.external_id) == (second.source, second.external_id):
        return DuplicateAssessment("distinct", "same_source_record")
    if (
        not first.city
        or first.city != second.city
        or not first.starts_at
        or not second.starts_at
    ):
        return DuplicateAssessment("distinct", "different_or_missing_city_time")
    time_gap = abs(first.starts_at - second.starts_at)
    if time_gap > timedelta(hours=2):
        return DuplicateAssessment("distinct", "different_time")
    if first_artists and second_artists and first_artists.isdisjoint(second_artists):
        return DuplicateAssessment("distinct", "conflicting_artists")
    same_venue = bool(
        first.venue
        and second.venue
        and _normalized(first.venue) == _normalized(second.venue)
    )
    same_title = _normalized(first.title) == _normalized(second.title)
    title_similarity = SequenceMatcher(
        None, _normalized(first.title), _normalized(second.title)
    ).ratio()
    shared_artist = bool(first_artists & second_artists)
    if (
        time_gap == timedelta(0)
        and same_venue
        and same_title
        and (shared_artist or len(_normalized(first.title)) >= 20)
    ):
        return DuplicateAssessment("merge", "exact_time_venue_title")
    if time_gap <= timedelta(minutes=45) and (
        same_title or (same_venue and (shared_artist or title_similarity >= 0.75))
    ):
        return DuplicateAssessment("review", "possible_same_show")
    return DuplicateAssessment("distinct", "insufficient_overlap")
