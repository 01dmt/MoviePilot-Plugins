import copy
import hashlib
import json
import unicodedata
from collections.abc import Collection, Mapping
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace


class MoviePilotGateway:
    def __init__(
        self,
        chain,
        torrents_chain,
        rule_helper,
        scheduler,
        site_resolver,
        not_exist_factory,
        candidate_matcher,
    ):
        self._chain = chain
        self._torrents = torrents_chain
        self._rules = rule_helper
        self._scheduler = scheduler
        self._site_resolver = site_resolver
        self._not_exist_factory = not_exist_factory
        self._candidate_matcher = candidate_matcher

    def progress(self):
        return self._scheduler.get_progress("subscribe_refresh")

    def load_cache(self):
        return self._torrents.get_torrents() or {}

    def context_matches_rule(self, context, snapshot, rule_group) -> bool:
        signature = self._rule_signature(snapshot, rule_group)
        if signature is None:
            return False

        copied = copy.deepcopy(context)
        copied.media_info.category = snapshot.category
        copied.media_info.episode_group = snapshot.episode_group
        result = self._chain.filter_torrents(
            rule_groups=[rule_group],
            torrent_list=[copied.torrent_info],
            mediainfo=copied.media_info,
        )
        if self._rule_signature(snapshot, rule_group) != signature:
            return False
        return bool(result)

    def filtered_candidates(self, snapshot, rule_group, cache):
        signature = self._rule_signature(snapshot, rule_group)
        if signature is None:
            raise ValueError(
                f"DV rule group is unavailable: {rule_group or '<empty>'}"
            )

        subscribe = snapshot.to_subscribe_proxy()
        contexts = [
            copy.deepcopy(context)
            for site_contexts in (cache or {}).values()
            for context in (site_contexts or [])
            if context and self._candidate_matcher(context, subscribe)
        ]

        allowed_sites = set(
            self._site_resolver(snapshot.to_subscribe_proxy()) or []
        )
        if allowed_sites:
            contexts = [
                context
                for context in contexts
                if getattr(context.torrent_info, "site", None) in allowed_sites
            ]

        for context in contexts:
            context.media_info.category = snapshot.category
            context.media_info.episode_group = snapshot.episode_group

        if not contexts:
            return []

        torrent_to_context = {
            id(context.torrent_info): context for context in contexts
        }
        filtered = self._chain.filter_torrents(
            rule_groups=[rule_group],
            torrent_list=[context.torrent_info for context in contexts],
            mediainfo=contexts[0].media_info,
        )
        if filtered is None:
            raise ValueError(
                f"DV rule group returned no filter result: {rule_group}"
            )
        if self._rule_signature(snapshot, rule_group) != signature:
            raise ValueError(
                f"DV rule group changed during filtering: {rule_group}"
            )
        return [
            torrent_to_context[id(torrent)]
            for torrent in filtered
            if id(torrent) in torrent_to_context
        ]

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

    def _rule_signature(self, snapshot, rule_group):
        if not isinstance(rule_group, str) or not rule_group.strip():
            return None

        get_group = getattr(self._rules, "get_rule_group", None)
        get_by_media = getattr(self._rules, "get_rule_group_by_media", None)
        if not callable(get_group) or not callable(get_by_media):
            return None

        try:
            group = get_group(rule_group)
            signature = self._group_signature(group)
            if (
                signature is None
                or signature[0] != rule_group
                or not isinstance(signature[1], str)
                or not signature[1].strip()
            ):
                return None
            applicable = get_by_media(
                media=self._snapshot_media(snapshot),
                group_names=[rule_group],
            )
        except Exception:
            return None

        for candidate in applicable or []:
            if self._group_signature(candidate) == signature:
                return signature
        return None

    @staticmethod
    def _group_signature(group):
        if group is None:
            return None
        return (
            getattr(group, "name", None),
            getattr(group, "rule_string", None),
            getattr(group, "media_type", None),
            getattr(group, "category", None),
        )

    @staticmethod
    def _snapshot_media(snapshot):
        media_type = getattr(snapshot, "media_type", None)
        media_type = getattr(media_type, "value", media_type)
        return SimpleNamespace(
            type=SimpleNamespace(value=media_type),
            category=getattr(snapshot, "category", None),
        )

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
