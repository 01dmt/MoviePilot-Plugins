import copy
import hashlib
import json
import unicodedata
from collections.abc import Collection, Mapping
from decimal import Decimal, InvalidOperation

from .dv import RankedCandidate, classify_dv


class MoviePilotGateway:
    def __init__(
        self,
        chain,
        torrents_chain,
        scheduler,
        site_resolver,
        not_exist_factory,
        candidate_matcher,
        classifier=classify_dv,
    ):
        self._chain = chain
        self._torrents = torrents_chain
        self._scheduler = scheduler
        self._site_resolver = site_resolver
        self._not_exist_factory = not_exist_factory
        self._candidate_matcher = candidate_matcher
        self._classifier = classifier

    def progress(self):
        return self._scheduler.get_progress("subscribe_refresh")

    def load_cache(self):
        return self._torrents.get_torrents() or {}

    def source_matches_target(self, context) -> bool:
        return self._classifier(copy.deepcopy(context)).eligible

    def ranked_candidates(self, snapshot, cache):
        subscribe = snapshot.to_subscribe_proxy()
        allowed_sites = set(self._site_resolver(subscribe) or [])
        ranked = []
        for site_contexts in (cache or {}).values():
            for context in site_contexts or []:
                if not context or not self._candidate_matcher(context, subscribe):
                    continue
                if allowed_sites and (
                    getattr(getattr(context, "torrent_info", None), "site", None)
                    not in allowed_sites
                ):
                    continue
                if getattr(context, "media_info", None) is None:
                    continue
                copied = copy.deepcopy(context)
                copied.media_info.category = snapshot.category
                copied.media_info.episode_group = snapshot.episode_group
                classification = self._classifier(copied)
                if classification.eligible:
                    ranked.append(RankedCandidate(copied, classification))
        return sorted(
            ranked,
            key=lambda candidate: candidate.classification.rank,
            reverse=True,
        )

    def fingerprint(self, context):
        torrent = getattr(context, "torrent_info", None)
        fields = [
            self._normalize_fingerprint_field(getattr(torrent, name, None))
            for name in ("site", "title", "enclosure")
        ]
        fields.append(self._normalize_fingerprint_size(getattr(torrent, "size", None)))
        if not fields[1] and not fields[2]:
            raise ValueError("Torrent fingerprint identity is empty")
        serialized = json.dumps(
            fields,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def add(self, snapshot, contexts, episodes, source):
        requested = set(episodes or [])
        if not requested:
            return set()

        mid = snapshot.tmdb_id or snapshot.douban_id
        if not mid:
            raise ValueError(
                "MoviePilot batch download requires TMDB or Douban identity"
            )

        no_exists = {
            mid: {
                snapshot.season: self._not_exist_factory(
                    season=snapshot.season,
                    episodes=sorted(requested),
                    total_episode=snapshot.total_episode or max(requested),
                    start_episode=min(requested),
                    require_complete_coverage=False,
                )
            }
        }
        result = self._chain.batch_download(
            contexts=contexts,
            no_exists=no_exists,
            save_path=snapshot.save_path,
            source=source,
            username=snapshot.username or "SubscribeMultiVersion",
            downloader=snapshot.downloader,
            custom_words=snapshot.custom_words,
        )

        if not isinstance(result, (tuple, list)) or len(result) != 2:
            raise ValueError("invalid batch_download result")
        downloads, lefts = result
        if not self._is_collection(downloads) or (
            lefts is not None and not isinstance(lefts, dict)
        ):
            raise ValueError("invalid batch_download result")

        remaining_tree = no_exists if lefts is None else lefts
        remaining = self._remaining_episodes(
            remaining_tree,
            mid=mid,
            season=snapshot.season,
            requested=requested,
        )
        added = requested - remaining
        if added and not downloads:
            raise ValueError("batch_download reported progress without downloads")
        return added

    @classmethod
    def _remaining_episodes(cls, tree, *, mid, season, requested):
        if not isinstance(tree, dict):
            raise ValueError("invalid batch_download remaining episodes")
        if not tree:
            return set()
        if set(tree) != {mid}:
            raise ValueError("invalid batch_download remaining episodes")

        seasons = tree[mid]
        if not isinstance(seasons, dict) or set(seasons) != {season}:
            raise ValueError("invalid batch_download remaining episodes")

        info = seasons[season]
        missing = object()
        if isinstance(info, Mapping):
            episodes = info.get("episodes", missing)
        else:
            episodes = getattr(info, "episodes", missing)
        if episodes is missing or not cls._is_collection(episodes):
            raise ValueError("invalid batch_download remaining episodes")
        if any(type(episode) is not int for episode in episodes):
            raise ValueError("invalid batch_download remaining episodes")

        remaining = set(episodes)
        if not remaining.issubset(requested):
            raise ValueError("invalid batch_download remaining episodes")
        return remaining

    @staticmethod
    def _is_collection(value):
        return isinstance(value, Collection) and not isinstance(
            value, (str, bytes, bytearray, Mapping)
        )

    @staticmethod
    def _normalize_fingerprint_field(value):
        if value is None:
            return ""
        return unicodedata.normalize("NFKC", str(value)).strip()

    @classmethod
    def _normalize_fingerprint_size(cls, value):
        normalized = cls._normalize_fingerprint_field(value)
        if not normalized:
            return ""
        try:
            number = Decimal(normalized)
        except InvalidOperation:
            return normalized
        if not number.is_finite():
            return normalized
        canonical = format(number.normalize(), "f")
        return "0" if canonical == "-0" else canonical
