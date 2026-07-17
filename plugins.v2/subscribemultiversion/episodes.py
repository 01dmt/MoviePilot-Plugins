from collections.abc import Iterable
from typing import Any, Optional


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdecimal():
            return int(text)
    return None


def _positive_ints(values: Any) -> tuple[int, ...]:
    if values is None:
        items: Iterable[Any] = ()
    elif isinstance(values, str) or not isinstance(values, Iterable):
        items = (values,)
    else:
        items = values

    result = []
    for value in items:
        number = _integer(value)
        if number is not None and number > 0:
            result.append(number)
    return tuple(sorted(set(result)))


def event_episodes(data: dict, context: Any) -> tuple[int, ...]:
    direct = _positive_ints(data.get("episodes"))
    if direct:
        return direct
    meta = getattr(context, "meta_info", None)
    return _positive_ints(getattr(meta, "episode_list", None))


def candidate_episodes(context: Any, season: int, pending: set[int]) -> set[int]:
    meta = getattr(context, "meta_info", None)
    candidate_season = getattr(meta, "begin_season", None)
    if candidate_season is None:
        candidate_season = getattr(meta, "season", None)
    episodes = set(_positive_ints(getattr(meta, "episode_list", None)))
    if candidate_season is not None:
        candidate_season_number = _integer(candidate_season)
        season_number = _integer(season)
        if candidate_season_number is None or candidate_season_number != season_number:
            return set()
        if not episodes:
            return set(pending)
    return episodes & set(pending)
