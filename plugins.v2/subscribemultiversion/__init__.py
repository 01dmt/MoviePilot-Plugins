from dataclasses import replace
from pathlib import Path
import threading
from typing import Any, Optional

from fastapi.responses import Response

from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.chain.torrents import TorrentsChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.helper.rule import RuleHelper
from app.log import logger
from app.plugins import _PluginBase
from app.scheduler import Scheduler
from app.schemas import NotExistMediaInfo
from app.schemas.types import ChainEventType, EventType, MediaType

from .domain import DownloadContextSnapshot, PluginConfig, utc_now
from .episodes import event_episodes
from .gateway import MoviePilotGateway
from .refresh import RefreshObserver
from .scope import ScopePolicy
from .service import CompanionService
from .store import TaskStore
from .ui import build_form, build_page


_SCHEDULER_ERROR = "subscribe_refresh scheduler read failed"
_OBSERVER_ERROR = "subscribe_refresh observer compatibility error"
_CACHE_READ_ERROR = "subscribe_refresh cache read failed"
_ADAPTER_REFRESH_ERRORS = frozenset({_SCHEDULER_ERROR, _OBSERVER_ERROR})
PLUGIN_ICON_ASSET = "subscribemultiversion.png"
PLUGIN_ICON_URL = "../api/v1/plugin/SubscribeMultiVersion/icon?v=0.1.1"


def _plugin_icon_response() -> Response:
    source = Path(__file__).with_name(PLUGIN_ICON_ASSET)
    try:
        return Response(
            content=source.read_bytes(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )
    except OSError as error:
        logger.error("订阅多版本读取内置图标失败：%s", error)
        return Response(status_code=404)


class SubscribeMultiVersion(_PluginBase):
    plugin_name = "订阅多版本"
    plugin_desc = "为电视剧订阅按独立规则追补多个版本"
    plugin_icon = PLUGIN_ICON_URL
    plugin_version = "0.1.1"
    plugin_author = "01dmt"
    plugin_order = 35
    auth_level = 1

    _lifecycle_creation_lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self._lifecycle_lock = threading.RLock()
        self._runtime_generation = 0

    def init_plugin(self, config: dict = None):
        with self._get_lifecycle_lock():
            self._init_plugin_locked(config)

    def _init_plugin_locked(self, config: dict = None):
        self._reset_runtime()
        self._config = self._safe_config(config)
        self._enabled = self._config.enabled

        try:
            self._store = TaskStore(
                lambda: self.get_data("state"),
                lambda value: self.save_data("state", value),
            )
            self._download_chain = DownloadChain()
            self._torrents_chain = TorrentsChain()
            self._rule_helper = RuleHelper()
            self._scheduler = Scheduler()
            self._gateway = MoviePilotGateway(
                chain=self._download_chain,
                torrents_chain=self._torrents_chain,
                rule_helper=self._rule_helper,
                scheduler=self._scheduler,
                site_resolver=SubscribeChain.get_sub_sites,
                not_exist_factory=NotExistMediaInfo,
                candidate_matcher=TorrentsChain._context_matches_subscribe,
            )
            self._subscribe_oper = SubscribeOper()
            self._refresh_observer = RefreshObserver()
            self._service = CompanionService(
                self._store,
                self._gateway,
                lambda: self._config,
                utc_now,
            )
        except Exception as exc:
            logger.error(
                "SubscribeMultiVersion plugin initialization failed: error_type=%s",
                type(exc).__name__,
            )
            self._enabled = False
            raise

        self._seed_selected_snapshots()
        self._derive_runtime_scope()
        now = utc_now()
        try:
            self._store.reconcile_scope(ScopePolicy(self._config), now)
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion scope reconciliation failed: error_type=%s",
                type(exc).__name__,
            )
            return

        if self._enabled and self._store.has_unfinished():
            try:
                self._service.scan("startup_recovery")
            except Exception as exc:
                logger.warning(
                    "SubscribeMultiVersion recovery scan failed: error_type=%s",
                    type(exc).__name__,
                )

    def _get_lifecycle_lock(self):
        lock = getattr(self, "_lifecycle_lock", None)
        if lock is not None:
            return lock
        with self._lifecycle_creation_lock:
            lock = getattr(self, "_lifecycle_lock", None)
            if lock is None:
                lock = threading.RLock()
                self._lifecycle_lock = lock
        return lock

    def _reset_runtime(self):
        self._runtime_generation = self._runtime_generation_value() + 1
        self._enabled = False
        self._config = PluginConfig()
        self._store = None
        self._download_chain = None
        self._torrents_chain = None
        self._rule_helper = None
        self._scheduler = None
        self._gateway = None
        self._subscribe_oper = None
        self._refresh_observer = None
        self._service = None

    @staticmethod
    def _safe_config(config: Optional[dict]) -> PluginConfig:
        try:
            return PluginConfig.from_dict(config if isinstance(config, dict) else {})
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "SubscribeMultiVersion plugin configuration rejected: error_type=%s",
                type(exc).__name__,
            )
            return PluginConfig()

    def _seed_selected_snapshots(self):
        selected_ids = set(self._config.watch_subscription_ids)
        if not selected_ids:
            return
        try:
            subscribes = self._subscribe_oper.list() or []
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion subscription listing failed: error_type=%s",
                type(exc).__name__,
            )
            return

        tv_value = self._enum_value(MediaType.TV)
        for subscribe in subscribes:
            if (
                self._optional_int(getattr(subscribe, "id", None)) not in selected_ids
                or self._enum_value(getattr(subscribe, "type", None)) != tv_value
            ):
                continue
            try:
                self._store.save_snapshot(self._snapshot_from_subscribe(subscribe))
            except Exception as exc:
                logger.warning(
                    "SubscribeMultiVersion subscription snapshot failed: error_type=%s",
                    type(exc).__name__,
                )

    def _derive_runtime_scope(self):
        selected_ids = set(self._config.watch_subscription_ids)
        snapshot_keys = tuple(
            dict.fromkeys(
                snapshot.snapshot_key
                for snapshot in self._store.known_snapshots()
                if snapshot.subscription_id in selected_ids
            )
        )
        self._config = replace(
            self._config,
            watch_snapshot_keys=snapshot_keys,
        )

    def _snapshot_from_subscribe(
        self, subscribe: Any, category: Optional[Any] = None
    ) -> DownloadContextSnapshot:
        subscription_id = self._required_positive_int(
            getattr(subscribe, "id", None), "subscription id"
        )
        name = self._optional_text(getattr(subscribe, "name", None))
        if not name:
            raise ValueError("Subscription name is unavailable")
        media_type = self._optional_text(
            self._enum_value(getattr(subscribe, "type", None))
        )
        if not media_type:
            raise ValueError("Subscription media type is unavailable")

        season_value = getattr(subscribe, "season", None)
        if season_value in (None, "") and media_type == self._enum_value(MediaType.TV):
            season = 1
        else:
            season = self._required_nonnegative_int(
                season_value, "subscription season"
            )

        preferred_category = category or getattr(subscribe, "media_category", None)
        sites = getattr(subscribe, "sites", None) or ()
        if isinstance(sites, (str, bytes, bytearray)):
            sites = (sites,)

        return DownloadContextSnapshot(
            subscription_id=subscription_id,
            name=name,
            year=self._optional_text(getattr(subscribe, "year", None)),
            media_type=media_type,
            season=season,
            tmdb_id=self._optional_int(getattr(subscribe, "tmdbid", None)),
            douban_id=self._optional_text(getattr(subscribe, "doubanid", None)),
            tvdb_id=self._optional_int(getattr(subscribe, "tvdbid", None)),
            category=self._optional_text(self._enum_value(preferred_category)),
            sites=tuple(
                site_id
                for site in sites
                if (site_id := self._optional_int(site)) is not None
            ),
            downloader=self._optional_text(
                self._enum_value(getattr(subscribe, "downloader", None))
            ),
            save_path=self._optional_text(getattr(subscribe, "save_path", None)),
            username=self._optional_text(getattr(subscribe, "username", None)),
            custom_words=self._optional_text(
                getattr(subscribe, "custom_words", None)
            ),
            episode_group=self._optional_text(
                getattr(subscribe, "episode_group", None)
            ),
            total_episode=self._optional_int(
                getattr(subscribe, "total_episode", None)
            ),
        )

    @eventmanager.register(ChainEventType.SubscribeCompletionCheck)
    def on_subscribe_completion_check(self, event: Event):
        with self._get_lifecycle_lock():
            self._on_subscribe_completion_check_locked(event)

    def _on_subscribe_completion_check_locked(self, event: Event):
        if not self._runtime_ready(event):
            return
        data = event.event_data
        subscribe = getattr(data, "subscribe", None)
        if subscribe is None:
            return
        try:
            mediainfo = getattr(data, "mediainfo", None)
            category = getattr(mediainfo, "category", None)
            snapshot = self._snapshot_from_subscribe(subscribe, category=category)
            if ScopePolicy(self._config).decide(snapshot).in_scope:
                self._store.save_completion_snapshot(snapshot)
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion completion event ignored: error_type=%s",
                type(exc).__name__,
            )

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event):
        with self._get_lifecycle_lock():
            self._on_download_added_locked(event)

    def _on_download_added_locked(self, event: Event):
        try:
            data = event.event_data or {}
            source = data.get("source")
            source_info = SubscribeChain.parse_subscribe_source_keyword(source)
            context = data.get("context")
            if not self._enabled or not source_info or not context:
                return
            if getattr(context.media_info, "type", None) != MediaType.TV:
                return
            snapshot = self._resolve_snapshot(source_info, data, context)
            episodes = event_episodes(data, context)
            if snapshot and episodes:
                self._service.ingest(
                    snapshot,
                    episodes,
                    context,
                    data.get("hash"),
                    getattr(context.torrent_info, "title", None),
                )
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion download event ignored: error_type=%s",
                type(exc).__name__,
            )

    def _resolve_snapshot(self, source_info: dict, data: dict, context: Any):
        if not self._store:
            return None
        subscription_id = self._optional_int(source_info.get("id"))
        if subscription_id is None or subscription_id <= 0:
            return None

        snapshot = None
        try:
            subscribe = self._subscribe_oper.get(subscription_id)
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion active subscription lookup failed: error_type=%s",
                type(exc).__name__,
            )
            subscribe = None
        if subscribe is not None:
            try:
                snapshot = self._snapshot_from_subscribe(subscribe)
            except Exception as exc:
                logger.warning(
                    "SubscribeMultiVersion active subscription snapshot failed: error_type=%s",
                    type(exc).__name__,
                )

        if snapshot is None:
            snapshot = self._store.completion_snapshot(subscription_id)
        if snapshot is None or snapshot.media_type != self._enum_value(MediaType.TV):
            return None

        changes = {}
        downloader = self._optional_text(self._enum_value(data.get("downloader")))
        if downloader:
            changes["downloader"] = downloader
        category = self._optional_text(
            self._enum_value(getattr(context.media_info, "category", None))
        )
        if category:
            changes["category"] = category
        return replace(snapshot, **changes) if changes else snapshot

    def get_service(self):
        with self._get_lifecycle_lock():
            if not getattr(self, "_enabled", False):
                return []
            return [
                {
                    "id": "refresh_observer",
                    "name": "订阅多版本刷新观察",
                    "trigger": "interval",
                    "func": self._observe_refresh,
                    "kwargs": {"seconds": 60},
                }
            ]

    def _observe_refresh(self):
        with self._get_lifecycle_lock():
            if not self._runtime_ready():
                return
            gateway = self._gateway
            generation = self._runtime_generation_value()

        progress_error = None
        try:
            progress = gateway.progress()
        except Exception as exc:
            progress = None
            progress_error = exc

        with self._get_lifecycle_lock():
            if not self._refresh_runtime_matches_locked(generation, gateway):
                return
            if progress_error is not None:
                old_timestamp, _ = self._store.refresh_state()
                logger.warning(
                    "SubscribeMultiVersion refresh scheduler read failed: error_type=%s",
                    type(progress_error).__name__,
                )
                self._set_refresh_result(old_timestamp, _SCHEDULER_ERROR)
                return
            self._observe_refresh_locked(progress)

    def _observe_refresh_locked(self, progress):
        old_timestamp, old_error = self._store.refresh_state()

        try:
            decision = self._refresh_observer.inspect(progress, old_timestamp)
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion refresh observer failed: error_type=%s",
                type(exc).__name__,
            )
            self._set_refresh_result(old_timestamp, _OBSERVER_ERROR)
            return

        if decision.error:
            refresh_error = (
                decision.error
                if self._progress_is_failed(progress)
                else _OBSERVER_ERROR
            )
            self._set_refresh_result(old_timestamp, refresh_error)
            return
        if not decision.should_scan:
            if old_error in _ADAPTER_REFRESH_ERRORS:
                self._set_refresh_result(old_timestamp, None)
            return

        try:
            scan_succeeded = self._service.scan("subscribe_refresh")
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion refresh scan failed: error_type=%s",
                type(exc).__name__,
            )
            return
        if scan_succeeded is True:
            self._set_refresh_result(decision.finished_at, None)
        else:
            self._set_refresh_result(old_timestamp, _CACHE_READ_ERROR)

    def _set_refresh_result(
        self, timestamp: Optional[str], error: Optional[str]
    ) -> None:
        try:
            self._store.set_refresh_result(timestamp, error)
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion refresh state persistence failed: error_type=%s",
                type(exc).__name__,
            )

    def _runtime_ready(self, event: Optional[Event] = None) -> bool:
        if event is not None and not getattr(event, "event_data", None):
            return False
        return bool(
            getattr(self, "_enabled", False)
            and getattr(self, "_config", None) is not None
            and getattr(self, "_store", None) is not None
            and getattr(self, "_service", None) is not None
        )

    def _runtime_generation_value(self) -> int:
        generation = getattr(self, "_runtime_generation", None)
        if generation is None:
            generation = 0
            self._runtime_generation = generation
        return generation

    def _refresh_runtime_matches_locked(self, generation: int, gateway: Any) -> bool:
        return bool(
            self._runtime_ready()
            and self._runtime_generation_value() == generation
            and self._gateway is gateway
        )

    @staticmethod
    def _progress_is_failed(progress: Any) -> bool:
        try:
            return getattr(progress, "status", None) == "failed"
        except Exception:
            return False

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return getattr(value, "value", value)

    @staticmethod
    def _optional_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdecimal():
            return int(value.strip())
        return None

    @classmethod
    def _required_positive_int(cls, value: Any, label: str) -> int:
        number = cls._optional_int(value)
        if number is None or number <= 0:
            raise ValueError(f"{label} is unavailable")
        return number

    @classmethod
    def _required_nonnegative_int(cls, value: Any, label: str) -> int:
        number = cls._optional_int(value)
        if number is None or number < 0:
            raise ValueError(f"{label} is unavailable")
        return number

    def get_state(self) -> bool:
        with self._get_lifecycle_lock():
            return bool(getattr(self, "_enabled", False))

    @staticmethod
    def get_command():
        return []

    def get_api(self):
        return [
            {
                "path": "/icon",
                "endpoint": _plugin_icon_response,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "订阅多版本插件图标",
                "description": "返回插件随包携带的 PNG 图标。",
            }
        ]

    @staticmethod
    def _ui_rule_groups(
        rule_helper: Any,
    ) -> tuple[tuple[Any, ...], bool]:
        if rule_helper is None:
            return (), True
        try:
            groups = rule_helper.get_rule_groups() or ()
            return (
                tuple(getattr(group, "name", None) for group in groups),
                False,
            )
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion UI rule options unavailable: error_type=%s",
                type(exc).__name__,
            )
            return (), True

    @classmethod
    def _ui_categories(
        cls,
        chain: Any,
    ) -> tuple[tuple[Any, ...], bool]:
        if chain is None:
            return (), True
        try:
            categories = (chain.media_category() or {}).get(
                cls._enum_value(MediaType.TV),
                (),
            )
            if isinstance(categories, str):
                return (categories,), False
            return tuple(categories or ()), False
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion UI category options unavailable: error_type=%s",
                type(exc).__name__,
            )
            return (), True

    def _ui_current_snapshots(
        self,
        subscribe_oper: Any,
    ) -> tuple[tuple[DownloadContextSnapshot, ...], bool]:
        if subscribe_oper is None:
            return (), True
        try:
            subscribes = subscribe_oper.list() or ()
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion UI subscription options unavailable: error_type=%s",
                type(exc).__name__,
            )
            return (), True

        snapshots = []
        failed = False
        tv_value = self._enum_value(MediaType.TV)
        for subscribe in subscribes:
            try:
                if (
                    self._enum_value(getattr(subscribe, "type", None))
                    != tv_value
                ):
                    continue
                snapshots.append(self._snapshot_from_subscribe(subscribe))
            except Exception as exc:
                failed = True
                logger.warning(
                    "SubscribeMultiVersion UI subscription snapshot unavailable: "
                    "error_type=%s",
                    type(exc).__name__,
                )
        return tuple(snapshots), failed

    @classmethod
    def _ui_known_snapshots(
        cls,
        store: Any,
        selected_ids: tuple[int, ...],
    ) -> tuple[tuple[DownloadContextSnapshot, ...], bool]:
        if not selected_ids:
            return (), False
        if store is None:
            return (), True
        try:
            snapshots = store.known_snapshots() or ()
        except Exception as exc:
            logger.warning(
                "SubscribeMultiVersion UI retained subscriptions unavailable: error_type=%s",
                type(exc).__name__,
            )
            return (), True

        retained = []
        failed = False
        tv_value = cls._enum_value(MediaType.TV)
        selected = set(selected_ids)
        for snapshot in snapshots:
            try:
                if (
                    getattr(snapshot, "subscription_id", None) in selected
                    and cls._enum_value(
                        getattr(snapshot, "media_type", None)
                    )
                    == tv_value
                ):
                    retained.append(snapshot)
            except Exception as exc:
                failed = True
                logger.warning(
                    "SubscribeMultiVersion UI retained subscription unavailable: "
                    "error_type=%s",
                    type(exc).__name__,
                )
        return tuple(retained), failed

    @staticmethod
    def _ui_merge_snapshots(
        current: tuple[DownloadContextSnapshot, ...],
        known: tuple[DownloadContextSnapshot, ...],
    ) -> tuple[DownloadContextSnapshot, ...]:
        merged = []
        seen = set()
        for snapshot in (*current, *known):
            subscription_id = getattr(snapshot, "subscription_id", None)
            if subscription_id in seen:
                continue
            seen.add(subscription_id)
            merged.append(snapshot)
        return tuple(merged)

    def get_form(self):
        with self._get_lifecycle_lock():
            config = getattr(self, "_config", None) or PluginConfig()
            store = getattr(self, "_store", None)
            subscribe_oper = getattr(self, "_subscribe_oper", None)
            rule_helper = getattr(self, "_rule_helper", None)
            chain = getattr(self, "chain", None)

            if rule_helper is None:
                try:
                    rule_helper = RuleHelper()
                except Exception as exc:
                    logger.warning(
                        "SubscribeMultiVersion UI rule helper unavailable: error_type=%s",
                        type(exc).__name__,
                    )
            if subscribe_oper is None:
                try:
                    subscribe_oper = SubscribeOper()
                except Exception as exc:
                    logger.warning(
                        "SubscribeMultiVersion UI subscription operator unavailable: "
                        "error_type=%s",
                        type(exc).__name__,
                    )

            rule_groups, rule_failed = self._ui_rule_groups(rule_helper)
            categories, category_failed = self._ui_categories(chain)
            current_snapshots, current_failed = self._ui_current_snapshots(
                subscribe_oper
            )
            known_snapshots, retained_failed = self._ui_known_snapshots(
                store,
                config.watch_subscription_ids,
            )
            subscriptions = self._ui_merge_snapshots(
                current_snapshots,
                known_snapshots,
            )
            warnings = []
            if rule_failed:
                warnings.append("规则组")
            if category_failed:
                warnings.append("二级分类")
            if current_failed:
                warnings.append("电视剧订阅")
            if retained_failed:
                warnings.append("已选订阅")
            return build_form(
                config,
                rule_groups=rule_groups,
                categories=categories,
                subscriptions=subscriptions,
                warnings=warnings,
            )

    def get_page(self):
        with self._get_lifecycle_lock():
            store = getattr(self, "_store", None)
            if store is None:
                return build_page(
                    tasks=(),
                    refresh_error=None,
                    now_iso=utc_now().isoformat(),
                    timezone_name=getattr(settings, "TZ", "UTC"),
                )
            tasks = store.tasks_for_page()
            _, refresh_error = store.refresh_state()
            return build_page(
                tasks=tasks,
                refresh_error=refresh_error,
                now_iso=utc_now().isoformat(),
                timezone_name=getattr(settings, "TZ", "UTC"),
            )

    def stop_service(self):
        with self._get_lifecycle_lock():
            self._reset_runtime()
