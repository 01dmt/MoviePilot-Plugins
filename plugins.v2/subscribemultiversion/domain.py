import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any, Optional

SCHEMA_VERSION = 1


class TaskStatus(str, Enum):
    WAITING = "waiting"
    MATCHING = "matching"
    ADDING = "adding"
    ADDED = "added"
    EXPIRED = "expired"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class PluginConfig:
    enabled: bool = False
    rule_group: str = ""
    categories: tuple[str, ...] = ()
    watch_subscription_ids: tuple[int, ...] = ()
    watch_snapshot_keys: tuple[str, ...] = ()
    watch_days: float = 3.0

    @classmethod
    def from_dict(cls, value: Optional[dict]) -> "PluginConfig":
        value = value or {}
        enabled_value = value.get("enabled", False)
        enabled = enabled_value if isinstance(enabled_value, bool) else False
        if isinstance(enabled_value, str):
            enabled = enabled_value.strip().lower() == "true"
        ids = tuple(
            dict.fromkeys(
                int(item)
                for item in (value.get("watch_subscription_ids") or ())
                if str(item).isdigit()
            )
        )
        try:
            days = float(value.get("watch_days", 3) or 3)
        except (TypeError, ValueError):
            days = 3.0
        if days <= 0 or not math.isfinite(days):
            days = 3.0
        return cls(
            enabled=enabled,
            rule_group=str(value.get("rule_group") or ""),
            categories=tuple(
                dict.fromkeys(
                    str(item) for item in (value.get("categories") or ()) if item
                )
            ),
            watch_subscription_ids=ids,
            watch_snapshot_keys=tuple(
                dict.fromkeys(
                    str(item)
                    for item in (value.get("watch_snapshot_keys") or ())
                    if item
                )
            ),
            watch_days=days,
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["categories"] = list(self.categories)
        data["watch_subscription_ids"] = list(self.watch_subscription_ids)
        data["watch_snapshot_keys"] = list(self.watch_snapshot_keys)
        return data


@dataclass(frozen=True)
class DownloadContextSnapshot:
    subscription_id: int
    name: str
    year: Optional[str]
    media_type: str
    season: int
    tmdb_id: Optional[int] = None
    douban_id: Optional[str] = None
    tvdb_id: Optional[int] = None
    category: Optional[str] = None
    sites: tuple[int, ...] = ()
    downloader: Optional[str] = None
    save_path: Optional[str] = None
    username: Optional[str] = None
    custom_words: Optional[str] = None
    episode_group: Optional[str] = None
    total_episode: Optional[int] = None

    @property
    def media_identity(self) -> str:
        if self.tmdb_id:
            return f"tmdb:{self.tmdb_id}"
        if self.douban_id:
            return f"douban:{self.douban_id}"
        if self.tvdb_id:
            return f"tvdb:{self.tvdb_id}"
        return f"subscribe:{self.subscription_id}"

    @property
    def snapshot_key(self) -> str:
        return f"{self.media_identity}:S{self.season:02d}"

    def to_subscribe_proxy(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=self.subscription_id,
            name=self.name,
            year=self.year,
            type=self.media_type,
            season=self.season,
            tmdbid=self.tmdb_id,
            doubanid=self.douban_id,
            tvdbid=self.tvdb_id,
            media_category=self.category,
            sites=list(self.sites),
            downloader=self.downloader,
            save_path=self.save_path,
            username=self.username,
            custom_words=self.custom_words,
            episode_group=self.episode_group,
            total_episode=self.total_episode,
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["sites"] = list(self.sites)
        return data

    @classmethod
    def from_dict(cls, value: dict) -> "DownloadContextSnapshot":
        return cls(**{**value, "sites": tuple(value.get("sites") or [])})


@dataclass(frozen=True)
class CompanionTask:
    key: str
    snapshot_key: str
    season: int
    episode: int
    created_at: datetime
    deadline_at: datetime
    updated_at: datetime
    status: TaskStatus = TaskStatus.WAITING
    scope_reason: str = ""
    source_hash: Optional[str] = None
    source_title: Optional[str] = None
    candidate_fingerprint: Optional[str] = None
    candidate_title: Optional[str] = None
    retry_count: int = 0
    last_error: Optional[str] = None
    claim_token: Optional[str] = None

    @classmethod
    def create(
        cls,
        snapshot: DownloadContextSnapshot,
        episode: int,
        now: datetime,
        watch_days: float,
        source_hash: Optional[str] = None,
        source_title: Optional[str] = None,
        scope_reason: str = "",
    ) -> "CompanionTask":
        key = f"{snapshot.media_identity}:S{snapshot.season:02d}:E{episode:02d}:dv"
        return cls(
            key=key,
            snapshot_key=snapshot.snapshot_key,
            season=snapshot.season,
            episode=episode,
            created_at=now,
            deadline_at=now + timedelta(days=watch_days),
            updated_at=now,
            source_hash=source_hash,
            source_title=source_title,
            scope_reason=scope_reason,
        )

    def evolve(self, **changes: Any) -> "CompanionTask":
        return replace(self, **changes)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        for field_name in ("created_at", "deadline_at", "updated_at"):
            data[field_name] = getattr(self, field_name).isoformat()
        return data

    @classmethod
    def from_dict(cls, value: dict) -> "CompanionTask":
        data = dict(value)
        data["status"] = TaskStatus(data["status"])
        for field_name in ("created_at", "deadline_at", "updated_at"):
            data[field_name] = datetime.fromisoformat(data[field_name])
        return cls(**data)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
