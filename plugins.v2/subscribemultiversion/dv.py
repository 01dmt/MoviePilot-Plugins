from dataclasses import dataclass
import re
from typing import Any


_TOKEN_LEFT = r"(?<![a-z0-9])"
_TOKEN_RIGHT = r"(?![a-z0-9])"

_DV_PATTERNS = (
    re.compile(_TOKEN_LEFT + r"dv" + _TOKEN_RIGHT, re.IGNORECASE),
    re.compile(_TOKEN_LEFT + r"dovi" + _TOKEN_RIGHT, re.IGNORECASE),
    re.compile(
        _TOKEN_LEFT + r"dolby[\s._-]*vision" + _TOKEN_RIGHT,
        re.IGNORECASE,
    ),
    re.compile(r"杜比视界"),
    re.compile(
        _TOKEN_LEFT + r"dvhe[\s._-]*0?(?:5|7|8)" + _TOKEN_RIGHT,
        re.IGNORECASE,
    ),
)
_RESOLUTION_PATTERNS = (
    re.compile(_TOKEN_LEFT + r"2160p" + _TOKEN_RIGHT, re.IGNORECASE),
    re.compile(_TOKEN_LEFT + r"4k" + _TOKEN_RIGHT, re.IGNORECASE),
    re.compile(_TOKEN_LEFT + r"uhd" + _TOKEN_RIGHT, re.IGNORECASE),
)
_PROFILE_PATTERNS = {
    profile: (
        re.compile(
            _TOKEN_LEFT + rf"p0?{number}" + _TOKEN_RIGHT,
            re.IGNORECASE,
        ),
        re.compile(
            _TOKEN_LEFT + rf"profile[\s._-]*0?{number}" + _TOKEN_RIGHT,
            re.IGNORECASE,
        ),
        re.compile(
            _TOKEN_LEFT + rf"dvhe[\s._-]*0?{number}" + _TOKEN_RIGHT,
            re.IGNORECASE,
        ),
    )
    for profile, number in (("p5", 5), ("p7", 7), ("p8", 8))
}
_LAYER_PATTERNS = {
    layer: re.compile(_TOKEN_LEFT + layer + _TOKEN_RIGHT, re.IGNORECASE)
    for layer in ("fel", "mel")
}
_SOURCE_PATTERNS = {
    "remux": (
        re.compile(_TOKEN_LEFT + r"remux" + _TOKEN_RIGHT, re.IGNORECASE),
    ),
    "web_dl": (
        re.compile(_TOKEN_LEFT + r"web[\s._-]*dl" + _TOKEN_RIGHT, re.IGNORECASE),
        re.compile(_TOKEN_LEFT + r"webrip" + _TOKEN_RIGHT, re.IGNORECASE),
    ),
    "disc_medium": tuple(
        re.compile(_TOKEN_LEFT + pattern + _TOKEN_RIGHT, re.IGNORECASE)
        for pattern in (
            r"blu[\s._-]*ray",
            r"hd[\s._-]*dvd",
        )
    ),
    "other_release": tuple(
        re.compile(_TOKEN_LEFT + pattern + _TOKEN_RIGHT, re.IGNORECASE)
        for pattern in (r"bdrip", r"hdtv")
    ),
}
_META_FIELDS = (
    "resource_effect",
    "resource_pix",
    "resource_type",
    "video_encode",
    "audio_encode",
    "edition",
    "web_source",
)
_TORRENT_FIELDS = ("title", "description", "labels")


@dataclass(frozen=True)
class DvClassification:
    is_dv: bool
    is_2160p: bool
    profile: str = "unknown"
    layer: str = "unknown"
    source: str = "unknown"
    rank: int = 0
    evidence: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return self.is_dv and self.is_2160p

    @property
    def variant(self) -> str:
        parts = []
        if self.profile != "unknown":
            parts.append(self.profile.upper())
        if self.layer != "unknown":
            parts.append(self.layer.upper())
        if self.source != "unknown":
            parts.append("WEB-DL" if self.source == "web_dl" else self.source.title())
        return " ".join(parts) or "Dolby Vision"


@dataclass(frozen=True)
class RankedCandidate:
    context: Any
    classification: DvClassification


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _context_text(context: Any) -> str:
    values = []
    meta_info = getattr(context, "meta_info", None)
    torrent_info = getattr(context, "torrent_info", None)
    for owner, fields in ((meta_info, _META_FIELDS), (torrent_info, _TORRENT_FIELDS)):
        for field in fields:
            values.extend(_strings(getattr(owner, field, None)))
    return "\n".join(values)


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _single_match(text: str, patterns_by_value: dict[str, Any]) -> str:
    matches = {
        value
        for value, patterns in patterns_by_value.items()
        if _matches_any(
            text,
            patterns if isinstance(patterns, tuple) else (patterns,),
        )
    }
    return next(iter(matches)) if len(matches) == 1 else "unknown"


def _source(text: str) -> str:
    matches = {
        source
        for source, patterns in _SOURCE_PATTERNS.items()
        if _matches_any(text, patterns)
    }
    if "remux" in matches:
        if "web_dl" in matches or "other_release" in matches:
            return "unknown"
        return "remux"
    if "web_dl" in matches:
        return "web_dl" if len(matches) == 1 else "unknown"
    return "other" if matches else "unknown"


def _rank(profile: str, layer: str, source: str) -> int:
    if profile == "unknown" or source == "unknown":
        return 100
    if profile == "p7" and source == "remux":
        return {"fel": 800, "mel": 700, "unknown": 600}[layer]
    if profile == "p8" and source == "remux":
        return 500
    if profile == "p8" and source == "web_dl":
        return 400
    if profile == "p5" and source == "web_dl":
        return 300
    return 200


def classify_dv(context: Any) -> DvClassification:
    text = _context_text(context)
    is_dv = _matches_any(text, _DV_PATTERNS)
    is_2160p = _matches_any(text, _RESOLUTION_PATTERNS)
    profile = _single_match(text, _PROFILE_PATTERNS)
    layer = _single_match(text, _LAYER_PATTERNS)
    source = _source(text)
    eligible = is_dv and is_2160p

    evidence = []
    if is_dv:
        evidence.append("dv")
    if is_2160p:
        evidence.append("2160p")
    if profile != "unknown":
        evidence.append(f"profile:{profile}")
    if layer != "unknown":
        evidence.append(f"layer:{layer}")
    if source != "unknown":
        evidence.append(f"source:{source}")

    return DvClassification(
        is_dv=is_dv,
        is_2160p=is_2160p,
        profile=profile,
        layer=layer,
        source=source,
        rank=_rank(profile, layer, source) if eligible else 0,
        evidence=tuple(evidence[:8]),
    )
