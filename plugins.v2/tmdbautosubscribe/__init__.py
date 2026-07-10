import datetime
import http.client
import json
import ssl
import socket
import time
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, build_opener, urlopen
from zoneinfo import ZoneInfo

from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType, NotificationType


TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_WEB_BASE = "https://www.themoviedb.org"
TV_GENRE_LABELS = {
    "16": "动画",
    "18": "剧情",
    "35": "喜剧",
    "10759": "动作冒险",
    "10765": "科幻奇幻",
    "99": "纪录",
}
MOVIE_GENRE_LABELS = {
    "16": "动画",
    "18": "剧情",
    "35": "喜剧",
    "28": "动作",
    "878": "科幻",
    "99": "纪录",
}
COUNTRY_LABELS = {
    "JP": "日本",
    "US": "美国",
    "CN": "中国",
    "KR": "韩国",
    "GB": "英国",
    "FR": "法国",
    "DE": "德国",
    "HK": "中国香港",
    "TW": "中国台湾",
    "TH": "泰国",
    "IN": "印度",
    "ID": "印尼",
    "PH": "菲律宾",
    "VN": "越南",
    "MY": "马来西亚",
    "SG": "新加坡",
    "CA": "加拿大",
    "AU": "澳大利亚",
}
LANGUAGE_LABELS = {
    "ja": "日语",
    "en": "英语",
    "zh": "普通话",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "th": "泰语",
    "hi": "印地语",
    "id": "印尼语",
    "tr": "土耳其语",
    "ar": "阿拉伯语",
}
FILTER_OPTION_CACHE_TTL = 24 * 60 * 60
FILTER_OPTION_COMMON_ORDER = {
    "movie_genres": ["16", "18", "35", "28", "12", "80", "99", "10751", "14", "36", "27", "10402", "9648", "10749", "878", "10770", "53", "10752", "37"],
    "tv_genres": ["16", "18", "35", "10759", "10765", "99", "80", "10751", "10762", "9648", "10763", "10764", "10766", "10767", "10768", "37"],
    "origin_countries": ["JP", "US", "CN", "KR", "GB", "FR", "DE", "HK", "TW", "TH", "IN", "ID", "PH", "VN", "MY", "SG", "CA", "AU"],
    "original_languages": ["ja", "en", "zh", "ko", "fr", "de", "es", "it", "pt", "ru", "th", "hi", "id", "tr", "ar"],
}
ACTION_KIND_LABELS = {
    "new_movie": "新电影上映",
    "new_tv_show": "新剧首播",
    "new_season": "老剧新季",
    "movie": "电影",
    "tv": "剧集",
}
STOP_REASON_LABELS = {
    "all_pages": "已到最后一页",
    "fixed_pages": "固定页数完成",
    "max_pages": "达到最多页数",
    "low_new_items": "连续低新数据",
    "empty": "无可拉取数据",
}
REGISTRY_CLASS_LABELS = {
    "matched": "已命中建议",
    "known_active": "已知活跃",
    "long_running": "长期连载/反复出现",
}
REGISTRY_RESULT_LABELS = {
    "new_movie": "新电影上映",
    "new_tv_show": "新剧首播",
    "new_season": "老剧新季",
    "ignored_old_season": "忽略：不是新剧或新季",
}


def _parse_date(value: Optional[str]) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_text(value: Optional[datetime.date]) -> Optional[str]:
    return value.isoformat() if value else None


def _image_url(base: str, path: Optional[str]) -> Optional[str]:
    return f"{base}{path}" if path else None


def _as_int(value: Any, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_int(value: Any, default: int, minimum: int) -> int:
    return max(minimum, _as_int(value, default))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "y", "on"):
            return True
        if normalized in ("0", "false", "no", "n", "off", ""):
            return False
    return bool(value)


def _as_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _as_blacklist(value: Any) -> List[str]:
    raw_items = _as_list(value)
    output: List[str] = []
    seen = set()
    for raw in raw_items:
        normalized = str(raw).replace("，", ",").replace("；", ",").replace(";", ",")
        normalized = normalized.replace("\r", "\n").replace("\t", " ")
        for part in normalized.replace(",", "\n").splitlines():
            token = part.strip().lower()
            if not token:
                continue
            if ":" in token:
                media_type, tmdb_id = token.split(":", 1)
                media_type = media_type.strip()
                tmdb_id = tmdb_id.strip()
                if media_type not in ("movie", "tv", "all") or not tmdb_id.isdigit():
                    continue
                token = f"{media_type}:{int(tmdb_id)}"
            elif token.isdigit():
                token = str(int(token))
            else:
                continue
            if token not in seen:
                seen.add(token)
                output.append(token)
    return output


def _tmdb_href(media_type: str, tmdb_id: Any) -> Optional[str]:
    if not tmdb_id:
        return None
    path = "movie" if media_type == "movie" else "tv"
    return f"{TMDB_WEB_BASE}/{path}/{tmdb_id}"


@dataclass
class ScanAction:
    kind: str
    media_type: str
    tmdb_id: int
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    total_episode: Optional[int] = None
    date: Optional[datetime.date] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    overview: str = ""
    subscribed: bool = False
    subscribe_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "total_episode": self.total_episode,
            "date": _date_text(self.date),
            "poster_url": _image_url(TMDB_IMAGE_BASE, self.poster_path),
            "backdrop_url": _image_url(TMDB_BACKDROP_BASE, self.backdrop_path),
            "overview": self.overview,
            "subscribed": self.subscribed,
            "subscribe_message": self.subscribe_message,
        }


class TmdbAutoClient:
    def __init__(
            self,
            api_key: str,
            language: str,
            region: str,
            base_url: str = "https://api.themoviedb.org/3",
            timeout: int = 10,
            retries: int = 2,
            proxy_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.language = language
        self.region = region
        self.base_url = base_url.rstrip("/")
        self.timeout = max(3, int(timeout or 10))
        self.retries = max(1, int(retries or 2))
        self.ssl_context = self._ssl_context()
        self.proxy_url = str(proxy_url or "").strip()
        self.opener = self._opener()

    def discover_movie(self, start: datetime.date, end: datetime.date, pages: int,
                       filters: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        params = {
            **filters,
            "sort_by": "primary_release_date.desc",
            "primary_release_date.gte": start.isoformat(),
            "primary_release_date.lte": end.isoformat(),
            "region": self.region,
        }
        return self._discover("/discover/movie", params, pages)

    def discover_tv_first_air(self, start: datetime.date, end: datetime.date, pages: int,
                              filters: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        params = {
            **filters,
            "sort_by": "first_air_date.desc",
            "first_air_date.gte": start.isoformat(),
            "first_air_date.lte": end.isoformat(),
        }
        return self._discover("/discover/tv", params, pages)

    def discover_tv_airing_adaptive(
            self,
            start: datetime.date,
            end: datetime.date,
            filters: Dict[str, Any],
            registry: Dict[str, Any],
            min_pages: int,
            max_pages: int,
            low_new_pages: int,
            min_new_items_per_page: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        params = {
            **filters,
            "sort_by": "air_date.desc",
            "air_date.gte": start.isoformat(),
            "air_date.lte": end.isoformat(),
        }
        items: List[Dict[str, Any]] = []
        seen: set[int] = set()
        low_streak = 0
        stop_reason = "max_pages"
        total_pages = 1
        total_results = 0
        page_stats: List[Dict[str, Any]] = []
        max_pages = max(1, max_pages)
        min_pages = max(1, min(min_pages, max_pages))

        for page in range(1, max_pages + 1):
            payload = self._get("/discover/tv", {**params, "page": page})
            results = payload.get("results") or []
            total_pages = int(payload.get("total_pages") or total_pages or 1)
            total_results = int(payload.get("total_results") or total_results or 0)

            new_count = 0
            known_count = 0
            for item in results:
                tmdb_id = int(item.get("id") or 0)
                if tmdb_id <= 0 or tmdb_id in seen:
                    continue
                seen.add(tmdb_id)
                items.append(item)
                if str(tmdb_id) in registry:
                    known_count += 1
                else:
                    new_count += 1

            page_stats.append({
                "page": page,
                "count": len(results),
                "new_ids": new_count,
                "known_ids": known_count,
            })

            if page >= total_pages:
                stop_reason = "all_pages"
                break
            if page >= min_pages:
                if new_count < min_new_items_per_page:
                    low_streak += 1
                else:
                    low_streak = 0
                if low_streak >= low_new_pages:
                    stop_reason = "low_new_items"
                    break
            time.sleep(0.05)

        return items, {
            "pages_fetched": len(page_stats),
            "total_pages": total_pages,
            "total_results": total_results,
            "stop_reason": stop_reason,
            "page_stats": page_stats,
        }

    def tv_detail(self, tmdb_id: int) -> Dict[str, Any]:
        return self._get(f"/tv/{tmdb_id}", {})

    def tv_season_detail(self, tmdb_id: int, season_number: int) -> Dict[str, Any]:
        return self._get(f"/tv/{tmdb_id}/season/{season_number}", {})

    def movie_detail(self, tmdb_id: int) -> Dict[str, Any]:
        return self._get(f"/movie/{tmdb_id}", {})

    def movie_genres(self) -> List[Dict[str, Any]]:
        return self._get("/genre/movie/list", {}).get("genres") or []

    def tv_genres(self) -> List[Dict[str, Any]]:
        return self._get("/genre/tv/list", {}).get("genres") or []

    def countries(self) -> List[Dict[str, Any]]:
        payload = self._get("/configuration/countries", {})
        return payload if isinstance(payload, list) else payload.get("results") or []

    def languages(self) -> List[Dict[str, Any]]:
        payload = self._get("/configuration/languages", {})
        return payload if isinstance(payload, list) else payload.get("results") or []

    def _discover(self, path: str, params: Dict[str, Any], pages: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        total_pages = 1
        total_results = 0
        pages = max(1, pages)
        for page in range(1, pages + 1):
            payload = self._get(path, {**params, "page": page})
            total_pages = int(payload.get("total_pages") or total_pages or 1)
            total_results = int(payload.get("total_results") or total_results or 0)
            items.extend(payload.get("results") or [])
            if page >= total_pages:
                break
            time.sleep(0.05)
        return items, {
            "pages_fetched": min(pages, total_pages),
            "total_pages": total_pages,
            "total_results": total_results,
            "stop_reason": "fixed_pages" if pages < total_pages else "all_pages",
        }

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        query = {
            "api_key": self.api_key,
            "language": self.language,
            **{key: value for key, value in params.items() if value not in (None, "", [])},
        }
        url = f"{self.base_url}{path}?{urlencode(query)}"
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                if self.opener:
                    response_ctx = self.opener.open(url, timeout=self.timeout)
                else:
                    response_ctx = urlopen(url, timeout=self.timeout, context=self.ssl_context)
                with response_ctx as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as err:
                raise RuntimeError(self._format_http_error(err)) from err
            except (URLError, TimeoutError, socket.timeout, http.client.HTTPException, OSError) as err:
                last_error = err
                if attempt < self.retries:
                    time.sleep(0.3 * attempt)
                    continue
                raise RuntimeError(f"TMDB 请求失败：{err}") from err
        raise RuntimeError(f"TMDB 请求失败：{last_error}")

    @staticmethod
    def _format_http_error(err: HTTPError) -> str:
        body = ""
        try:
            body = err.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        message = body.strip()
        if body:
            try:
                payload = json.loads(body)
                message = payload.get("status_message") or payload.get("errors") or message
                if isinstance(message, list):
                    message = "；".join(str(item) for item in message)
            except (TypeError, ValueError):
                pass
        return f"TMDB 请求失败 {err.code}：{message or err.reason}"

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()

    def _opener(self):
        if not self.proxy_url:
            return None
        return build_opener(
            ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}),
            HTTPSHandler(context=self.ssl_context),
        )


class TmdbAutoSubscribe(_PluginBase):
    plugin_name = "TMDB自动订阅"
    plugin_desc = "按 TMDB 新上映、新剧首播和老剧新季生成 MoviePilot 订阅建议，支持自动订阅、缓存和细分筛选。"
    plugin_icon = "https://raw.githubusercontent.com/01dmt/MoviePilot-Plugins/9b5ba8d3d0fe32ae34fb23a9b72b47d67ce2569d/icons/tmdbautosubscribe-256.png?v=1.0.5"
    plugin_version = "1.0.5"
    plugin_author = "Codex"
    author_url = "https://github.com/jxxghp/MoviePilot"
    plugin_config_prefix = "tmdbautosubscribe_"
    plugin_order = 35
    auth_level = 1

    _event = Event()
    _scheduler: Any = None

    def init_plugin(self, config: dict = None):
        raw_config = config or {}
        self._config = self._merge_config(raw_config)
        if "long_gap_days" in raw_config:
            self.update_config(self._config)
        if self._should_migrate_scan_window(raw_config):
            self._config["lookback_days"] = 0
            self._config["lookahead_days"] = 7
            self._config["window_direction_migrated"] = True
            self.update_config(self._config)
        self.stop_service()
        if self._config.get("clear_cache"):
            self.save_data("series_registry", {})
            self.save_data("last_result", {})
            self._config["clear_cache"] = False
            self.update_config(self._config)
        if self._config.get("onlyonce"):
            BackgroundScheduler, _ = self._apscheduler()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.scan,
                trigger="date",
                run_date=datetime.datetime.now(tz=ZoneInfo(settings.TZ)) + datetime.timedelta(seconds=3),
                name="TMDB自动订阅立即扫描",
            )
            self._config["onlyonce"] = False
            self.update_config(self._config)
            self._scheduler.start()
        elif self._config.get("enabled"):
            try:
                BackgroundScheduler, CronTrigger = self._apscheduler()
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                cron = self._config.get("cron") or "0 9 * * *"
                self._scheduler.add_job(
                    func=self.scan,
                    trigger=CronTrigger.from_crontab(cron),
                    name="TMDB自动订阅",
                )
                self._scheduler.start()
                self._config["last_init_error"] = ""
            except Exception as err:
                self._config["enabled"] = False
                self._config["last_init_error"] = f"定时任务配置错误：{err}"
                self.update_config(self._config)
                logger.error(f"TMDB自动订阅定时任务启动失败：{err}")

    @staticmethod
    def _should_migrate_scan_window(config: Dict[str, Any]) -> bool:
        if not config or config.get("window_direction_migrated"):
            return False
        return (
            "lookback_days" in config
            and "lookahead_days" in config
            and _as_int(config.get("lookback_days"), 0) == 7
            and _as_int(config.get("lookahead_days"), 0) == 0
        )

    @staticmethod
    def _apscheduler():
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        return BackgroundScheduler, CronTrigger

    def get_state(self) -> bool:
        return bool(getattr(self, "_config", {}).get("enabled"))

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET", "POST"],
                "summary": "运行一次 TMDB 自动订阅扫描",
                "description": "按当前配置运行扫描，并返回候选和订阅建议。",
            },
            {
                "path": "/result",
                "endpoint": self.api_result,
                "methods": ["GET"],
                "summary": "查询最近一次 TMDB 自动订阅结果",
                "description": "返回最近一次扫描缓存。",
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["GET", "POST"],
                "summary": "清空 TMDB 自动订阅扫描缓存",
                "description": "清空最近扫描结果和剧集观察缓存，不修改插件配置。",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        config = self._merge_config(getattr(self, "_config", None) or self.get_config() or {})
        filter_options = self._filter_options(config)
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "class": "mb-3"},
                        "content": [
                            {"component": "VCardTitle", "props": {"class": "text-subtitle-1 pb-0"}, "text": "基础配置"},
                            {"component": "VCardText", "content": [
                                self._row([
                                    self._switch("enabled", "启用插件", 3),
                                    self._switch("onlyonce", "立即运行一次", 3),
                                    self._switch("auto_subscribe", "自动订阅", 3),
                                    self._switch("notify", "启动通知", 3),
                                ]),
                                self._row([
                                    self._number("lookback_days", "\u8fc7\u53bb\u626b\u63cf\u5929\u6570", 3),
                                    self._number("lookahead_days", "\u672a\u6765\u626b\u63cf\u5929\u6570", 3),
                                    self._select("media_types", "媒体类型", [
                                        {"title": "电影", "value": "movie"},
                                        {"title": "剧集", "value": "tv"},
                                    ], 6, multiple=True),
                                ]),
                            ]},
                        ],
                    },
                    {
                        "component": "VExpansionPanels",
                        "props": {"multiple": True, "variant": "accordion", "class": "mb-3"},
                        "content": [
                            self._detail_filter_panel(filter_options),
                            self._blacklist_panel(),
                            {
                                "component": "VExpansionPanel",
                                "content": [
                                    {"component": "VExpansionPanelTitle", "text": "扫描参数"},
                                    {"component": "VExpansionPanelText", "content": [
                                        {
                                            "component": "VCard",
                                            "props": {"variant": "tonal", "class": "mb-3"},
                                            "content": [
                                                {"component": "VCardTitle", "props": {"class": "text-subtitle-2 pb-0"}, "text": "参数说明"},
                                                {"component": "VCardText", "props": {"class": "text-body-2 pt-2"}, "content": [
                                                    {"component": "VList", "props": {"density": "compact", "class": "bg-transparent py-0"}, "content": [
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "TMDB API Key：留空时使用 MoviePilot 系统 TMDB 配置。"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "执行周期：定时扫描 cron 表达式。"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "清空缓存：下次初始化时清理筛选项和观察缓存。"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "电影/新剧页数：控制按上映/首播日期发现电影和新剧时最多拉取多少页。"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "播出最少页数/最多页数：控制老剧新季队列的自适应拉取范围。"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "低新连续页数：连续多页新增很少时提前停止拉取老剧新季。"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "\u6bcf\u9875\u6709\u6548\u65b0\u6570\u636e\uff1a\u5224\u65ad\u4e00\u9875\u662f\u5426\u8fd8\u503c\u5f97\u7ee7\u7eed\u7ffb\u9875\u62c9\u53d6\u3002"},
                                                        {"component": "VListItem", "props": {"class": "px-0"}, "text": "TMDB超时秒数/重试次数：控制接口请求等待时间和失败重试次数。"},
                                                    ]},
                                                ]},
                                            ],
                                        },
                                        self._row([
                                            self._text("tmdb_api_key", "TMDB API Key", 6, "留空时尝试使用系统 TMDB 配置"),
                                            self._text("cron", "执行周期", 3, "cron，默认每天 09:00"),
                                            self._switch("clear_cache", "清空缓存", 3),
                                            self._switch("use_proxy", "使用MP代理", 3),
                                        ]),
                                        self._row([
                                            self._number("discover_pages", "电影/新剧页数", 3),
                                            self._number("airing_min_pages", "播出最少页数", 3),
                                            self._number("airing_max_pages", "播出最多页数", 3),
                                            self._number("low_new_pages", "低新连续页数", 3),
                                        ]),
                                        self._row([
                                            self._number("min_new_items_per_page", "每页有效新数据", 3),
                                            self._number("tmdb_timeout", "TMDB超时秒数", 3),
                                            self._number("tmdb_retries", "TMDB重试次数", 3),
                                        ]),
                                    ]},
                                ],
                            },
                        ],
                    },
                ],
            }
        ], self._default_config()

    def get_page(self) -> Optional[List[dict]]:
        result = self.get_data("last_result") or {}
        status_panel = self._scan_status_panel(self._scan_status())
        if not result:
            blocks = [
                self._page_toolbar(None),
                status_panel,
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "class": "mt-3"},
                    "text": "暂无扫描结果，点击立即扫描会按当前配置拉取 TMDB 数据并生成调试明细。",
                },
            ]
            init_alert = self._init_error_alert()
            return [blocks[0], init_alert, *blocks[1:]] if init_alert else blocks
        summary = result.get("summary") or {}
        if summary.get("error"):
            blocks = [
                self._page_toolbar(summary),
                status_panel,
                {
                    "component": "VAlert",
                    "props": {"type": "error", "variant": "tonal", "class": "mb-3"},
                    "text": f"扫描失败：{summary.get('error')}",
                },
            ]
            init_alert = self._init_error_alert()
            return [blocks[0], init_alert, *blocks[1:]] if init_alert else blocks
        actions = result.get("actions") or []
        candidates = result.get("candidates") or []
        registry = self.get_data("series_registry") or {}
        filters = summary.get("filters") or {}
        filter_labels = self._filter_option_labels()
        filter_text = self._filter_text(summary, filters, filter_labels)
        blocks = [
            self._page_toolbar(summary, self._merge_config(getattr(self, "_config", None) or self.get_config() or {})),
            status_panel,
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mb-3"},
                "text": (
                    f"最近扫描：{summary.get('finished_at', '-')}；窗口 {summary.get('window_start')} 至 "
                    f"{summary.get('window_end')}；候选 {summary.get('candidate_count', 0)}；"
                    f"建议 {summary.get('actions_count', 0)}；缓存剧集 {len(registry)}"
                ),
            },
        ]
        init_alert = self._init_error_alert()
        if init_alert:
            blocks.append(init_alert)
        if summary.get("detail_error_count"):
            detail_errors = result.get("detail_errors") or []
            examples = "；".join(
                f"{item.get('source') or 'TMDB'} {item.get('tmdb_id') or '-'}: {item.get('error')}"
                for item in detail_errors[:3]
            )
            blocks.append({
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "class": "mb-3"},
                "text": f"有 {summary.get('detail_error_count')} 条 TMDB 请求异常，已跳过对应来源或条目并继续扫描。{examples}",
            })
        blocks.extend([
            {
                "component": "VAlert",
                "props": {"type": "success", "variant": "tonal", "class": "mb-3"},
                "text": filter_text,
            },
            self._metrics(
                summary,
                self._merge_config(getattr(self, "_config", None) or self.get_config() or {}),
                registry_count=len(registry),
            ),
            self._queue_panel(summary.get("queue_stats") or {}),
            self._poster_wall("订阅建议", actions[:60], action=True),
            self._poster_wall("候选明细", candidates[:120], action=False),
        ])
        return blocks

    def _init_error_alert(self) -> Optional[dict]:
        message = (getattr(self, "_config", {}) or self.get_config() or {}).get("last_init_error")
        if not message:
            return None
        return {
            "component": "VAlert",
            "props": {"type": "warning", "variant": "tonal", "class": "mb-3"},
            "text": message,
        }

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown()
            except Exception:
                pass
            self._scheduler = None

    def _api_scan_sync_legacy(self, apikey: str = "") -> Dict[str, Any]:
        result = self.scan()
        summary = result.get("summary") or {}
        return {
            "success": not bool(summary.get("error")),
            "message": summary.get("error") or (
                f"扫描完成：候选 {summary.get('candidate_count', 0)}，建议 {summary.get('actions_count', 0)}，"
                f"黑名单跳过 {summary.get('blacklist_skip_count', 0)}；刷新详情页查看最新结果"
            ),
            "result": result,
        }

    def api_scan(self, apikey: str = "") -> Dict[str, Any]:
        current_status = self._scan_status()
        if current_status.get("running"):
            return {
                "success": True,
                "message": f"扫描正在进行：{current_status.get('message') or current_status.get('phase') or '-'}",
                "status": current_status,
            }
        config = self._merge_config(getattr(self, "_config", None) or self.get_config() or {})
        started_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = self._set_scan_status(
            phase="queued",
            percent=1,
            running=True,
            message="扫描已进入后台队列",
            started_at=started_at,
        )
        Thread(target=self.scan, kwargs={"config": config, "started_at": started_at}, daemon=True).start()
        return {
            "success": True,
            "message": "扫描已开始，进度会显示在插件页面的扫描状态里",
            "status": status,
        }

    def api_result(self, apikey: str = "") -> Dict[str, Any]:
        return self.get_data("last_result") or {}

    def api_clear(self, apikey: str = "") -> Dict[str, Any]:
        self.save_data("last_result", {})
        self.save_data("series_registry", {})
        self.save_data("scan_status", {})
        return {
            "success": True,
            "message": "已清空扫描结果和缓存观察数据",
        }

    def _scan_sync_legacy(self) -> Dict[str, Any]:
        config = self._merge_config(getattr(self, "_config", None) or self.get_config() or {})
        started_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = self._scan(config=config, started_at=started_at)
            self.save_data("last_result", result)
            self._notify_scan_result(config, result)
            logger.info(f"TMDB自动订阅扫描完成：{result.get('summary')}")
            return result
        except Exception as err:
            logger.error(f"TMDB自动订阅扫描失败：{err}")
            result = {
                "summary": {
                    "started_at": started_at,
                    "finished_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "error": str(err),
                },
                "actions": [],
                "candidates": [],
            }
            self.save_data("last_result", result)
            self._notify_scan_result(config, result)
            return result

    def scan(self, config: Optional[Dict[str, Any]] = None, started_at: Optional[str] = None) -> Dict[str, Any]:
        config = self._merge_config(config or getattr(self, "_config", None) or self.get_config() or {})
        started_at = started_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._set_scan_status("preparing", 5, True, "准备扫描参数", started_at=started_at)
        try:
            result = self._scan(config=config, started_at=started_at)
            self.save_data("last_result", result)
            summary = result.get("summary") or {}
            self._set_scan_status(
                "finished",
                100,
                False,
                f"扫描完成：候选 {summary.get('candidate_count', 0)}，建议 {summary.get('actions_count', 0)}",
                started_at=started_at,
                finished_at=summary.get("finished_at"),
            )
            self._notify_scan_result(config, result)
            logger.info(f"TMDB自动订阅扫描完成：{result.get('summary')}")
            return result
        except Exception as err:
            finished_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"TMDB自动订阅扫描失败：{err}")
            result = {
                "summary": {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error": str(err),
                },
                "actions": [],
                "candidates": [],
            }
            self.save_data("last_result", result)
            self._set_scan_status(
                "failed",
                100,
                False,
                "扫描失败",
                started_at=started_at,
                finished_at=finished_at,
                error=str(err),
            )
            self._notify_scan_result(config, result)
            return result

    def _scan_status(self) -> Dict[str, Any]:
        status = self.get_data("scan_status") or {}
        if not isinstance(status, dict):
            return {}
        return status

    def _set_scan_status(
            self,
            phase: str,
            percent: int,
            running: bool,
            message: str,
            started_at: Optional[str] = None,
            finished_at: Optional[str] = None,
            error: Optional[str] = None,
    ) -> Dict[str, Any]:
        current = self._scan_status()
        status = {
            "phase": phase,
            "percent": max(0, min(100, int(percent))),
            "running": bool(running),
            "message": message,
            "started_at": started_at or current.get("started_at"),
            "finished_at": finished_at,
            "error": error,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data("scan_status", status)
        return status

    def _notify_scan_result(self, config: Dict[str, Any], result: Dict[str, Any]) -> None:
        if not config.get("notify"):
            return
        summary = result.get("summary") or {}
        title = "TMDB自动订阅扫描失败" if summary.get("error") else "TMDB自动订阅扫描完成"
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text=self._notification_text(summary),
            )
        except Exception as err:
            logger.error(f"TMDB自动订阅发送通知失败：{err}")

    @staticmethod
    def _notification_text(summary: Dict[str, Any]) -> str:
        if summary.get("error"):
            return (
                f"时间：{summary.get('finished_at') or summary.get('started_at') or '-'}\n"
                f"结果：失败\n"
                f"错误：{summary.get('error')}"
            )

        action_counts = summary.get("actions_by_kind") or {}
        action_text = "，".join(
            f"{ACTION_KIND_LABELS.get(kind, kind)} {count}"
            for kind, count in action_counts.items()
        ) or "无"
        queue_stats = summary.get("queue_stats") or {}
        pages = sum(
            _as_int(stats.get("pages_fetched"), 0)
            for stats in queue_stats.values()
            if isinstance(stats, dict)
        )
        return (
            f"时间：{summary.get('finished_at', '-')}\n"
            f"范围：{summary.get('window_start', '-')} 至 {summary.get('window_end', '-')}\n"
            f"媒体：{','.join(summary.get('media_types') or []) or '-'}\n"
            f"拉取页数：{pages}\n"
            f"候选 {summary.get('candidate_count', 0)}，建议 {summary.get('actions_count', 0)}，"
            f"黑名单跳过 {summary.get('blacklist_skip_count', 0)}，详情错误 {summary.get('detail_error_count', 0)}\n"
            f"建议分类：{action_text}\n"
            f"模式：{'自动订阅' if summary.get('auto_subscribe') else '仅生成建议'}"
        )

    def _scan(self, config: Dict[str, Any], started_at: str) -> Dict[str, Any]:
        today = datetime.date.today()
        window_start = today - datetime.timedelta(days=_as_int(config.get("lookback_days"), 7))
        window_end = today + datetime.timedelta(days=_as_int(config.get("lookahead_days"), 0))
        client = self._tmdb_client(config)
        registry = self.get_data("series_registry") or {}
        media_types = set(_as_list(config.get("media_types")) or ["tv"])
        movie_filters = self._discover_filters(config, "movie")
        tv_filters = self._discover_filters(config, "tv")
        blacklist_skip_count = 0
        candidates: List[Dict[str, Any]] = []
        actions: List[ScanAction] = []
        queue_stats: Dict[str, Any] = {}
        detail_errors: List[Dict[str, Any]] = []

        def stage(phase: str, percent: int, message: str):
            self._set_scan_status(phase, percent, True, message, started_at=started_at)

        def fetch_queue(source: str, fetcher):
            try:
                return fetcher()
            except Exception as err:
                queue_stats[source] = {
                    "pages_fetched": 0,
                    "total_pages": 0,
                    "total_results": 0,
                    "stop_reason": "error",
                    "error": str(err),
                }
                detail_errors.append({
                    "media_type": "tv" if source.startswith("tv_") else "movie",
                    "tmdb_id": None,
                    "source": source,
                    "error": str(err),
                })
                logger.error(f"TMDB 自动订阅拉取 {source} 队列失败：{err}")
                return [], queue_stats[source]

        stage("movie", 12, "拉取电影上映列表" if "movie" in media_types else "跳过电影扫描")
        if "movie" in media_types:
            movies, queue_stats["movie"] = fetch_queue(
                "movie",
                lambda: client.discover_movie(
                    window_start,
                    window_end,
                    pages=_as_int(config.get("discover_pages"), 3),
                    filters=movie_filters,
                ),
            )
            for movie in self._dedupe(movies):
                if self._is_blacklisted("movie", movie.get("id"), config):
                    blacklist_skip_count += 1
                    continue
                action = self._analyze_movie(movie, window_start, window_end)
                candidates.append(self._movie_candidate(movie, action))
                if action:
                    actions.append(action)

        tv_details: Dict[int, Dict[str, Any]] = {}
        if "tv" in media_types:
            def fetch_tv_detail(tmdb_id: int, source: str) -> Optional[Dict[str, Any]]:
                try:
                    return client.tv_detail(tmdb_id)
                except Exception as err:
                    detail_errors.append({
                        "media_type": "tv",
                        "tmdb_id": tmdb_id,
                        "source": source,
                        "error": str(err),
                    })
                    logger.error(f"TMDB 自动订阅拉取剧集详情失败：{tmdb_id} 来源 {source} - {err}")
                    return None

            def fetch_tv_season_detail(tmdb_id: int, season_number: int) -> Optional[Dict[str, Any]]:
                try:
                    return client.tv_season_detail(tmdb_id, season_number)
                except Exception as err:
                    detail_errors.append({
                        "media_type": "tv",
                        "tmdb_id": tmdb_id,
                        "source": f"season:{season_number}",
                        "error": str(err),
                    })
                    logger.error(f"TMDB 自动订阅拉取剧集季详情失败：{tmdb_id} S{season_number} - {err}")
                    return None

            def process_tv_detail(detail: Dict[str, Any]):
                tmdb_id = int(detail["id"])
                tv_details[tmdb_id] = detail
                action = self._analyze_tv(
                    detail,
                    window_start,
                    window_end,
                    season_detail_getter=lambda season_number: fetch_tv_season_detail(tmdb_id, season_number),
                )
                candidates.append(self._tv_candidate(detail, action))
                if action:
                    actions.append(action)
                self._update_registry(registry, detail, action)

            stage("tv_first_air", 32, "拉取新剧首播列表")
            first_air_items, queue_stats["tv_first_air"] = fetch_queue(
                "tv_first_air",
                lambda: client.discover_tv_first_air(
                    window_start,
                    window_end,
                    pages=_as_int(config.get("discover_pages"), 3),
                    filters=tv_filters,
                ),
            )
            stage("tv_airing", 52, "拉取近期播出剧集")
            airing_items, queue_stats["tv_airing"] = fetch_queue(
                "tv_airing",
                lambda: client.discover_tv_airing_adaptive(
                    window_start,
                    window_end,
                    filters=tv_filters,
                    registry=registry,
                    min_pages=_as_int(config.get("airing_min_pages"), 3),
                    max_pages=_as_int(config.get("airing_max_pages"), 10),
                    low_new_pages=_as_int(config.get("low_new_pages"), 2),
                    min_new_items_per_page=_as_int(config.get("min_new_items_per_page"), 3),
                ),
            )
            tv_items = self._dedupe(first_air_items + airing_items)
            total_tv_items = len(tv_items)
            for index, item in enumerate(tv_items, start=1):
                if total_tv_items:
                    percent = 60 + int(index / total_tv_items * 25)
                    stage("tv_detail", min(percent, 85), f"拉取剧集详情 {index}/{total_tv_items}")
                tmdb_id = int(item["id"])
                if self._is_blacklisted("tv", tmdb_id, config):
                    blacklist_skip_count += 1
                    continue
                detail = fetch_tv_detail(tmdb_id, "discover")
                if detail:
                    process_tv_detail(detail)

        stage("actions", 90, "生成订阅建议")
        action_dicts = [self._action_dict(action, config) for action in actions]
        if config.get("auto_subscribe"):
            stage("subscribe", 94, "提交 MoviePilot 订阅")
            self._subscribe_actions(actions)
            action_dicts = [self._action_dict(action, config) for action in actions]

        stage("saving", 98, "保存扫描结果")
        self.save_data("series_registry", registry)
        action_counts: Dict[str, int] = {}
        for action in action_dicts:
            action_counts[action["kind"]] = action_counts.get(action["kind"], 0) + 1
        finished_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "summary": {
                "started_at": started_at,
                "finished_at": finished_at,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "media_types": sorted(media_types),
                "candidate_count": len(candidates),
                "actions_count": len(action_dicts),
                "actions_by_kind": action_counts,
                "auto_subscribe": bool(config.get("auto_subscribe")),
                "filters": self._filter_snapshot(config),
                "queue_stats": queue_stats,
                "detail_error_count": len(detail_errors),
                "blacklist_skip_count": blacklist_skip_count,
            },
            "actions": action_dicts,
            "candidates": candidates,
            "detail_errors": detail_errors[:20],
        }

    @staticmethod
    def _action_dict(action: ScanAction, config: Dict[str, Any]) -> Dict[str, Any]:
        data = action.to_dict()
        data["auto_subscribe"] = bool(config.get("auto_subscribe"))
        return data

    def _analyze_movie(self, movie: Dict[str, Any], start: datetime.date, end: datetime.date) -> Optional[ScanAction]:
        release_date = _parse_date(movie.get("release_date"))
        if not release_date or not start <= release_date <= end:
            return None
        return ScanAction(
            kind="new_movie",
            media_type="movie",
            tmdb_id=int(movie["id"]),
            title=movie.get("title") or movie.get("original_title") or str(movie["id"]),
            year=release_date.year,
            date=release_date,
            poster_path=movie.get("poster_path"),
            backdrop_path=movie.get("backdrop_path"),
            overview=movie.get("overview") or "",
        )

    def _analyze_tv(
            self,
            detail: Dict[str, Any],
            start: datetime.date,
            end: datetime.date,
            season_detail_getter: Optional[Callable[[int], Optional[Dict[str, Any]]]] = None,
    ) -> Optional[ScanAction]:
        first_air = _parse_date(detail.get("first_air_date"))
        title = detail.get("name") or detail.get("original_name") or str(detail.get("id"))
        if first_air and start <= first_air <= end:
            season = self._first_regular_season(detail)
            return ScanAction(
                kind="new_tv_show",
                media_type="tv",
                tmdb_id=int(detail["id"]),
                title=title,
                year=first_air.year,
                season=season.get("season_number") if season else 1,
                total_episode=season.get("episode_count") if season else None,
                date=first_air,
                poster_path=detail.get("poster_path"),
                backdrop_path=detail.get("backdrop_path"),
                overview=detail.get("overview") or "",
            )
        if not first_air:
            first_season = self._first_regular_season(detail)
            first_season_air = _parse_date((first_season or {}).get("air_date"))
            if first_season and int(first_season.get("season_number") or 0) == 1 and first_season_air and start <= first_season_air <= end:
                return ScanAction(
                    kind="new_tv_show",
                    media_type="tv",
                    tmdb_id=int(detail["id"]),
                    title=title,
                    year=first_season_air.year,
                    season=1,
                    total_episode=int(first_season.get("episode_count") or 0) or None,
                    date=first_season_air,
                    poster_path=detail.get("poster_path"),
                    backdrop_path=detail.get("backdrop_path"),
                    overview=detail.get("overview") or "",
                )
        candidate_seasons = self._candidate_season_numbers(detail, start, end)
        season_details: Dict[int, Optional[Dict[str, Any]]] = {}
        for season_number in candidate_seasons:
            season_detail = season_detail_getter(season_number) if season_detail_getter else None
            season_details[season_number] = season_detail
            if season_detail:
                e01_air_date = self._episode_air_date(season_detail, 1)
                if e01_air_date and start <= e01_air_date <= end and season_number > 1:
                    season = self._season_summary(detail, season_number)
                    return ScanAction(
                        kind="new_season",
                        media_type="tv",
                        tmdb_id=int(detail["id"]),
                        title=title,
                        year=first_air.year if first_air else e01_air_date.year,
                        season=season_number,
                        episode=1,
                        total_episode=int((season or {}).get("episode_count") or 0) or None,
                        date=e01_air_date,
                        poster_path=detail.get("poster_path"),
                        backdrop_path=detail.get("backdrop_path"),
                        overview=detail.get("overview") or "",
                    )

        fallback_e01 = self._episode_pointer_e01_in_window(detail, start, end)
        if fallback_e01:
            season_number, air_date = fallback_e01
            if season_number > 1 and not season_details.get(season_number):
                season = self._season_summary(detail, season_number)
                return ScanAction(
                    kind="new_season",
                    media_type="tv",
                    tmdb_id=int(detail["id"]),
                    title=title,
                    year=first_air.year if first_air else air_date.year,
                    season=season_number,
                    episode=1,
                    total_episode=int((season or {}).get("episode_count") or 0) or None,
                    date=air_date,
                    poster_path=detail.get("poster_path"),
                    backdrop_path=detail.get("backdrop_path"),
                    overview=detail.get("overview") or "",
                )

        if not season_detail_getter:
            season = self._season_in_window(detail, start, end)
            if season:
                air_date = _parse_date(season.get("air_date"))
                season_number = int(season.get("season_number") or 0)
                return ScanAction(
                    kind="new_season",
                    media_type="tv",
                    tmdb_id=int(detail["id"]),
                    title=title,
                    year=first_air.year if first_air else (air_date.year if air_date else None),
                    season=season_number,
                    total_episode=int(season.get("episode_count") or 0) or None,
                    date=air_date,
                    poster_path=detail.get("poster_path"),
                    backdrop_path=detail.get("backdrop_path"),
                    overview=detail.get("overview") or "",
                )
        return None

    def _subscribe_actions(self, actions: List[ScanAction]):
        chain = SubscribeChain()
        for action in actions:
            try:
                sid, msg = chain.add(
                    title=action.title,
                    year=str(action.year) if action.year else None,
                    mtype=MediaType.MOVIE if action.media_type == "movie" else MediaType.TV,
                    tmdbid=action.tmdb_id,
                    season=action.season if action.media_type == "tv" else None,
                    total_episode=action.total_episode,
                    exist_ok=True,
                    username="TMDB自动订阅",
                )
                action.subscribed = bool(sid)
                action.subscribe_message = msg or ("已添加订阅" if sid else "未添加订阅")
            except Exception as err:
                action.subscribed = False
                action.subscribe_message = str(err)

    def _movie_candidate(self, movie: Dict[str, Any], action: Optional[ScanAction]) -> Dict[str, Any]:
        return {
            "media_type": "movie",
            "tmdb_id": int(movie["id"]),
            "title": movie.get("title") or movie.get("original_title") or str(movie["id"]),
            "date": movie.get("release_date"),
            "poster_url": _image_url(TMDB_IMAGE_BASE, movie.get("poster_path")),
            "overview": movie.get("overview") or "",
            "matched_actions": [action.kind] if action else [],
            "matched": bool(action),
            "debug_reason": "已生成订阅建议" if action else "上映日期不在扫描窗口内",
        }

    def _tv_candidate(self, detail: Dict[str, Any], action: Optional[ScanAction]) -> Dict[str, Any]:
        seasons = [
            {
                "season": season.get("season_number"),
                "air_date": season.get("air_date"),
                "episode_count": season.get("episode_count"),
            }
            for season in detail.get("seasons") or []
            if season.get("season_number") and int(season.get("season_number")) > 0
        ]
        return {
            "media_type": "tv",
            "tmdb_id": int(detail["id"]),
            "title": detail.get("name") or detail.get("original_name") or str(detail["id"]),
            "date": detail.get("first_air_date"),
            "poster_url": _image_url(TMDB_IMAGE_BASE, detail.get("poster_path")),
            "overview": detail.get("overview") or "",
            "seasons": seasons,
            "matched_actions": [action.kind] if action else [],
            "matched": bool(action),
            "debug_reason": "已生成订阅建议" if action else "不是新剧首播或老剧新季",
        }

    def _update_registry(self, registry: Dict[str, Any], detail: Dict[str, Any], action: Optional[ScanAction]):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        key = str(detail["id"])
        previous = registry.get(key) or {}
        seen_count = int(previous.get("seen_count") or 0) + 1
        seasons = [
            {
                "season": season.get("season_number"),
                "air_date": season.get("air_date"),
                "episode_count": season.get("episode_count"),
            }
            for season in detail.get("seasons") or []
            if season.get("season_number") and int(season.get("season_number")) > 0
        ]
        previous_seasons = {
            int(season.get("season") or season.get("season_number") or 0): season
            for season in previous.get("seasons") or []
            if int(season.get("season") or season.get("season_number") or 0) > 0
        }
        changed_seasons = []
        season_update_detected = False
        episode_update_detected = False
        if previous:
            for season in seasons:
                number = int(season.get("season") or 0)
                old = previous_seasons.get(number)
                if not old:
                    changed_seasons.append({
                        "season": number,
                        "previous_air_date": None,
                        "air_date": season.get("air_date"),
                        "previous_episode_count": None,
                        "episode_count": season.get("episode_count"),
                    })
                    season_update_detected = True
                    continue
                old_air_date = old.get("air_date")
                old_episode_count = int(old.get("episode_count") or 0)
                episode_count = int(season.get("episode_count") or 0)
                if old_air_date != season.get("air_date") or old_episode_count != episode_count:
                    changed_seasons.append({
                        "season": number,
                        "previous_air_date": old_air_date,
                        "air_date": season.get("air_date"),
                        "previous_episode_count": old_episode_count,
                        "episode_count": episode_count,
                    })
                    if old_air_date != season.get("air_date"):
                        season_update_detected = True
                    if old_episode_count != episode_count:
                        episode_update_detected = True
            previous_episode = previous.get("last_episode_to_air") or {}
            current_episode = detail.get("last_episode_to_air") or {}
            if previous_episode and current_episode:
                previous_key = (
                    previous_episode.get("season_number"),
                    previous_episode.get("episode_number"),
                    previous_episode.get("air_date"),
                )
                current_key = (
                    current_episode.get("season_number"),
                    current_episode.get("episode_number"),
                    current_episode.get("air_date"),
                )
                if previous_key != current_key:
                    episode_update_detected = True
        registry[key] = {
            "tmdb_id": int(detail["id"]),
            "title": detail.get("name") or detail.get("original_name") or str(detail["id"]),
            "first_seen": previous.get("first_seen") or now,
            "last_seen": now,
            "seen_count": seen_count,
            "first_air_date": detail.get("first_air_date"),
            "last_episode_to_air": detail.get("last_episode_to_air") or {},
            "next_episode_to_air": detail.get("next_episode_to_air") or {},
            "seasons": seasons,
            "changed_seasons": changed_seasons,
            "season_update_detected": season_update_detected,
            "episode_update_detected": episode_update_detected,
            "last_season_change_seen_at": now if season_update_detected else previous.get("last_season_change_seen_at"),
            "last_episode_change_seen_at": now if episode_update_detected else previous.get("last_episode_change_seen_at"),
            "classification": "matched" if action else ("long_running" if seen_count >= 3 else "known_active"),
            "last_result": action.kind if action else "ignored_old_season",
        }

    def _discover_filters(self, config: Dict[str, Any], media_type: str) -> Dict[str, Any]:
        genres = _as_list(config.get("movie_genres") if media_type == "movie" else config.get("tv_genres"))
        excluded_genres = _as_list(config.get("exclude_movie_genres") if media_type == "movie" else config.get("exclude_tv_genres"))
        filters: Dict[str, Any] = {}
        if genres:
            filters["with_genres"] = "|".join(genres)
        if excluded_genres:
            filters["without_genres"] = "|".join(excluded_genres)
        origin_countries = _as_list(config.get("origin_countries"))
        if origin_countries:
            filters["with_origin_country"] = "|".join(origin_countries)
        original_languages = _as_list(config.get("original_languages"))
        if original_languages:
            filters["with_original_language"] = "|".join(original_languages)
        return filters

    @staticmethod
    def _tv_detail_matches_filters(detail: Dict[str, Any], config: Dict[str, Any]) -> bool:
        genre_filters = set(_as_list(config.get("tv_genres")))
        if genre_filters:
            genre_ids = {
                str(genre_id)
                for genre_id in (detail.get("genre_ids") or [])
                if genre_id not in (None, "")
            }
            genre_ids.update(
                str(genre.get("id"))
                for genre in (detail.get("genres") or [])
                if genre.get("id") not in (None, "")
            )
            if not genre_ids.intersection(genre_filters):
                return False

        country_filters = set(_as_list(config.get("origin_countries")))
        if country_filters:
            countries = {str(country) for country in (detail.get("origin_country") or []) if country}
            if not countries.intersection(country_filters):
                return False

        language_filters = set(_as_list(config.get("original_languages")))
        if language_filters and str(detail.get("original_language") or "") not in language_filters:
            return False

        return True

    @staticmethod
    def _filter_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "movie_genres": _as_list(config.get("movie_genres")),
            "tv_genres": _as_list(config.get("tv_genres")),
            "exclude_movie_genres": _as_list(config.get("exclude_movie_genres")),
            "exclude_tv_genres": _as_list(config.get("exclude_tv_genres")),
            "origin_countries": _as_list(config.get("origin_countries")),
            "original_languages": _as_list(config.get("original_languages")),
            "blacklist": _as_blacklist(config.get("blacklist")),
            "discover_pages": _as_int(config.get("discover_pages"), 3),
            "airing_min_pages": _as_int(config.get("airing_min_pages"), 3),
            "airing_max_pages": _as_int(config.get("airing_max_pages"), 10),
            "low_new_pages": _as_int(config.get("low_new_pages"), 2),
            "min_new_items_per_page": _as_int(config.get("min_new_items_per_page"), 3),
            "tmdb_timeout": _as_int(config.get("tmdb_timeout"), 10),
            "tmdb_retries": _as_int(config.get("tmdb_retries"), 2),
        }

    def _filter_option_labels(self) -> Dict[str, Dict[str, str]]:
        cached = self.get_data("tmdb_filter_options") or {}
        options = cached.get("options") or self._fallback_filter_options()
        labels = {
            "movie_genres": dict(MOVIE_GENRE_LABELS),
            "tv_genres": dict(TV_GENRE_LABELS),
            "origin_countries": dict(COUNTRY_LABELS),
            "original_languages": dict(LANGUAGE_LABELS),
        }
        for key, rows in options.items():
            labels.setdefault(key, {})
            for row in rows or []:
                value = str(row.get("value") or "")
                title = str(row.get("title") or value)
                if value:
                    labels[key][value] = title.rsplit(f" ({value})", 1)[0]
        return labels

    @staticmethod
    def _filter_text(summary: Dict[str, Any], filters: Dict[str, Any], labels: Optional[Dict[str, Dict[str, str]]] = None) -> str:
        labels = labels or {}
        return (
            f"按已保存配置扫描：媒体 {','.join(summary.get('media_types') or []) or '-'}；"
            f"剧集类型 {TmdbAutoSubscribe._labeled_values(filters.get('tv_genres') or [], labels.get('tv_genres') or TV_GENRE_LABELS)}；"
            f"排除剧集类型 {TmdbAutoSubscribe._labeled_values(filters.get('exclude_tv_genres') or [], labels.get('tv_genres') or TV_GENRE_LABELS)}；"
            f"电影类型 {TmdbAutoSubscribe._labeled_values(filters.get('movie_genres') or [], labels.get('movie_genres') or MOVIE_GENRE_LABELS)}；"
            f"排除电影类型 {TmdbAutoSubscribe._labeled_values(filters.get('exclude_movie_genres') or [], labels.get('movie_genres') or MOVIE_GENRE_LABELS)}；"
            f"国家 {TmdbAutoSubscribe._labeled_values(filters.get('origin_countries') or [], labels.get('origin_countries') or COUNTRY_LABELS)}；"
            f"语言 {TmdbAutoSubscribe._labeled_values(filters.get('original_languages') or [], labels.get('original_languages') or LANGUAGE_LABELS)}；"
            f"页数 {filters.get('discover_pages', '-')}/{filters.get('airing_min_pages', '-')}-{filters.get('airing_max_pages', '-')}；"
            f"超时 {filters.get('tmdb_timeout', '-')}s×{filters.get('tmdb_retries', '-')}"
        )

    @staticmethod
    def _labeled_values(values: List[str], labels: Dict[str, str]) -> str:
        if not values:
            return "不限"
        return ",".join(f"{labels.get(str(value), str(value))}({value})" for value in values)

    @staticmethod
    def _tmdb_client(config: Dict[str, Any]) -> TmdbAutoClient:
        api_key = config.get("tmdb_api_key") or getattr(settings, "TMDB_API_KEY", "")
        if not api_key:
            raise ValueError("缺少 TMDB API Key")
        domain = getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org") or "api.themoviedb.org"
        if str(domain).startswith(("http://", "https://")):
            base_url = f"{str(domain).rstrip('/')}/3"
        else:
            base_url = f"https://{str(domain).strip('/')}/3"
        return TmdbAutoClient(
            api_key=api_key,
            language=config.get("language") or "zh-CN",
            region=config.get("region") or "CN",
            base_url=base_url,
            timeout=_as_int(config.get("tmdb_timeout"), 10),
            retries=_as_int(config.get("tmdb_retries"), 2),
            proxy_url=TmdbAutoSubscribe._tmdb_proxy_url(config),
        )

    @staticmethod
    def _tmdb_proxy_url(config: Dict[str, Any]) -> Optional[str]:
        if not _as_bool(config.get("use_proxy"), True):
            return None
        proxy_host = getattr(settings, "PROXY_HOST", "") or ""
        if proxy_host:
            return str(proxy_host)
        proxy = getattr(settings, "PROXY", None)
        if isinstance(proxy, dict):
            return str(proxy.get("https") or proxy.get("http") or "") or None
        return None

    @staticmethod
    def _first_regular_season(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        seasons = [
            season for season in detail.get("seasons") or []
            if season.get("season_number") and int(season.get("season_number")) > 0
        ]
        return sorted(seasons, key=lambda item: int(item.get("season_number") or 0))[0] if seasons else None

    @staticmethod
    def _season_in_window(detail: Dict[str, Any], start: datetime.date, end: datetime.date) -> Optional[Dict[str, Any]]:
        seasons = []
        for season in detail.get("seasons") or []:
            number = int(season.get("season_number") or 0)
            if number <= 1:
                continue
            air_date = _parse_date(season.get("air_date"))
            if air_date and start <= air_date <= end:
                seasons.append((air_date, season))
        if not seasons:
            return None
        seasons.sort(key=lambda item: item[0], reverse=True)
        return seasons[0][1]

    @staticmethod
    def _season_summary(detail: Dict[str, Any], season_number: int) -> Optional[Dict[str, Any]]:
        for season in detail.get("seasons") or []:
            if int(season.get("season_number") or 0) == season_number:
                return season
        return None

    @staticmethod
    def _candidate_season_numbers(detail: Dict[str, Any], start: datetime.date, end: datetime.date) -> List[int]:
        numbers: List[int] = []

        def add(value: Any) -> None:
            try:
                number = int(value or 0)
            except (TypeError, ValueError):
                return
            if number > 0 and number not in numbers:
                numbers.append(number)

        for pointer_key in ("last_episode_to_air", "next_episode_to_air"):
            pointer = detail.get(pointer_key) or {}
            pointer_air_date = _parse_date(pointer.get("air_date"))
            if pointer_air_date and start <= pointer_air_date <= end:
                add(pointer.get("season_number"))

        for season in detail.get("seasons") or []:
            season_air_date = _parse_date(season.get("air_date"))
            if season_air_date and start <= season_air_date <= end:
                add(season.get("season_number"))

        return numbers

    @staticmethod
    def _episode_air_date(season_detail: Dict[str, Any], episode_number: int) -> Optional[datetime.date]:
        for episode in season_detail.get("episodes") or []:
            if int(episode.get("episode_number") or 0) == episode_number:
                return _parse_date(episode.get("air_date"))
        return None

    @staticmethod
    def _episode_pointer_e01_in_window(
            detail: Dict[str, Any],
            start: datetime.date,
            end: datetime.date,
    ) -> Optional[Tuple[int, datetime.date]]:
        for key in ("last_episode_to_air", "next_episode_to_air"):
            pointer = detail.get(key) or {}
            if int(pointer.get("episode_number") or 0) != 1:
                continue
            air_date = _parse_date(pointer.get("air_date"))
            season_number = int(pointer.get("season_number") or 0)
            if season_number > 0 and air_date and start <= air_date <= end:
                return season_number, air_date
        return None

    @staticmethod
    def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = set()
        output = []
        for item in items:
            tmdb_id = int(item.get("id") or 0)
            if tmdb_id <= 0 or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            output.append(item)
        return output

    @staticmethod
    def _is_blacklisted(media_type: str, tmdb_id: Any, config: Dict[str, Any]) -> bool:
        try:
            normalized_id = str(int(tmdb_id))
        except (TypeError, ValueError):
            return False
        blacklist = set(_as_blacklist(config.get("blacklist")))
        return (
            normalized_id in blacklist
            or f"{media_type}:{normalized_id}" in blacklist
            or f"all:{normalized_id}" in blacklist
        )

    def _merge_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        merged = self._default_config()
        merged.update(config or {})
        merged.pop("detail_dialog", None)
        merged.pop("long_gap_days", None)
        for key in ("media_types", "tv_genres", "movie_genres", "exclude_tv_genres", "exclude_movie_genres", "origin_countries", "original_languages"):
            merged[key] = _as_list(merged.get(key))
        merged["blacklist"] = _as_blacklist(merged.get("blacklist"))
        for key in ("enabled", "onlyonce", "auto_subscribe", "notify", "clear_cache", "use_proxy"):
            merged[key] = _as_bool(merged.get(key), self._default_config()[key])
        for key in ("lookback_days", "lookahead_days", "min_new_items_per_page"):
            merged[key] = _clamp_int(merged.get(key), self._default_config()[key], 0)
        for key in ("discover_pages", "airing_min_pages", "airing_max_pages", "low_new_pages"):
            merged[key] = _clamp_int(merged.get(key), self._default_config()[key], 1)
        merged["tmdb_timeout"] = _clamp_int(merged.get("tmdb_timeout"), self._default_config()["tmdb_timeout"], 3)
        merged["tmdb_retries"] = _clamp_int(merged.get("tmdb_retries"), self._default_config()["tmdb_retries"], 1)
        if merged["airing_max_pages"] < merged["airing_min_pages"]:
            merged["airing_max_pages"] = merged["airing_min_pages"]
        return merged

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        return {
            "enabled": False,
            "onlyonce": False,
            "auto_subscribe": False,
            "notify": False,
            "clear_cache": False,
            "use_proxy": True,
            "tmdb_api_key": "",
            "language": "zh-CN",
            "region": "CN",
            "cron": "0 9 * * *",
            "lookback_days": 0,
            "lookahead_days": 7,
            "discover_pages": 3,
            "airing_min_pages": 3,
            "airing_max_pages": 10,
            "low_new_pages": 2,
            "min_new_items_per_page": 3,
            "tmdb_timeout": 10,
            "tmdb_retries": 2,
            "media_types": ["tv"],
            "tv_genres": ["16"],
            "movie_genres": [],
            "exclude_tv_genres": [],
            "exclude_movie_genres": [],
            "origin_countries": ["JP"],
            "original_languages": ["ja"],
            "blacklist": [],
        }

    @staticmethod
    def _row(cols: List[dict]) -> dict:
        return {"component": "VRow", "content": cols}

    @staticmethod
    def _switch(model: str, label: str, cols: int) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [
            {"component": "VSwitch", "props": {"model": model, "label": label}}
        ]}

    @staticmethod
    def _text(model: str, label: str, cols: int, placeholder: str = "") -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [
            {"component": "VTextField", "props": {"model": model, "label": label, "placeholder": placeholder}}
        ]}

    @staticmethod
    def _textarea(model: str, label: str, placeholder: str = "") -> dict:
        return {"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VTextarea", "props": {
                "model": model,
                "label": label,
                "placeholder": placeholder,
                "auto-grow": True,
                "rows": 3,
                "clearable": True,
            }}
        ]}

    @staticmethod
    def _number(model: str, label: str, cols: int) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [
            {"component": "VTextField", "props": {"model": model, "label": label, "type": "number"}}
        ]}

    @staticmethod
    def _select(
            model: str,
            label: str,
            items: List[Dict[str, str]],
            cols: int,
            multiple: bool = False,
            autocomplete: bool = False,
    ) -> dict:
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": [
            {"component": "VAutocomplete" if autocomplete else "VSelect", "props": {
                "model": model,
                "label": label,
                "items": items,
                "multiple": multiple,
                "chips": multiple,
                "clearable": True,
                "hide-selected": False,
            }}
        ]}

    def _filter_options(self, config: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
        cached = self.get_data("tmdb_filter_options") or {}
        options = cached.get("options")
        if options:
            return options

        options = self._fallback_filter_options()
        self.save_data("tmdb_filter_options", {
            "updated_at": int(time.time()),
            "options": options,
            "fallback": True,
        })
        return options

    @staticmethod
    def _filter_options_cache_valid(cached: Dict[str, Any]) -> bool:
        updated_at = int((cached or {}).get("updated_at") or 0)
        options = (cached or {}).get("options") or {}
        return bool(options) and time.time() - updated_at < FILTER_OPTION_CACHE_TTL

    @staticmethod
    def _fallback_filter_options() -> Dict[str, List[Dict[str, str]]]:
        return {
            "movie_genres": TmdbAutoSubscribe._label_options(MOVIE_GENRE_LABELS, "movie_genres"),
            "tv_genres": TmdbAutoSubscribe._label_options(TV_GENRE_LABELS, "tv_genres"),
            "origin_countries": TmdbAutoSubscribe._label_options(COUNTRY_LABELS, "origin_countries"),
            "original_languages": TmdbAutoSubscribe._label_options(LANGUAGE_LABELS, "original_languages"),
        }

    @staticmethod
    def _genre_options(rows: List[Dict[str, Any]], fallback: Dict[str, str], key: str) -> List[Dict[str, str]]:
        labels = {
            str(row.get("id")): str(row.get("name") or row.get("id"))
            for row in rows
            if row.get("id")
        }
        return TmdbAutoSubscribe._label_options(labels or fallback, key)

    @staticmethod
    def _country_options(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        labels = {}
        for row in rows:
            code = str(row.get("iso_3166_1") or "").upper()
            if not code:
                continue
            if code in COUNTRY_LABELS:
                labels[code] = COUNTRY_LABELS[code]
                continue
            name = row.get("native_name") or row.get("english_name") or code
            english = row.get("english_name")
            labels[code] = f"{name} / {english}" if english and english != name else str(name)
        return TmdbAutoSubscribe._label_options(labels or COUNTRY_LABELS, "origin_countries")

    @staticmethod
    def _language_options(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        labels = {}
        for row in rows:
            code = str(row.get("iso_639_1") or "").lower()
            if not code:
                continue
            if code in LANGUAGE_LABELS:
                labels[code] = LANGUAGE_LABELS[code]
                continue
            name = row.get("name") or row.get("english_name") or code
            english = row.get("english_name")
            labels[code] = f"{name} / {english}" if english and english != name else str(name)
        return TmdbAutoSubscribe._label_options(labels or LANGUAGE_LABELS, "original_languages")

    @staticmethod
    def _label_options(labels: Dict[str, str], key: str) -> List[Dict[str, str]]:
        order = {value: index for index, value in enumerate(FILTER_OPTION_COMMON_ORDER.get(key) or [])}
        rows = [
            {"title": f"{title} ({value})", "value": value}
            for value, title in labels.items()
        ]
        return sorted(rows, key=lambda item: (order.get(str(item["value"]), 9999), item["title"]))

    def _detail_filter_panel(self, filter_options: Dict[str, List[Dict[str, str]]]) -> dict:
        return {
            "component": "VExpansionPanel",
            "content": [
                {
                    "component": "VExpansionPanelTitle",
                    "text": "细节分类",
                },
                {
                    "component": "VExpansionPanelText",
                    "content": [
                        {
                            "component": "VAlert",
                            "props": {"type": "info", "variant": "tonal", "class": "mb-3"},
                            "text": "这些筛选会直接参与 TMDB 拉取；不选表示不限，多选表示任一匹配。",
                        },
                        {
                            "component": "VCard",
                            "props": {"variant": "tonal", "class": "mb-3"},
                            "content": [
                                {"component": "VCardTitle", "props": {"class": "text-subtitle-2 pb-0"}, "text": "基础信息"},
                                {"component": "VCardText", "props": {"class": "text-body-2 pt-2"}, "text": "基础配置只保留是否启用、是否自动订阅、扫描时间范围和媒体类型；更细的类型、国家和语言在这里展开配置。"},
                            ],
                        },
                        self._row([
                            self._select("tv_genres", "剧集类型", filter_options["tv_genres"], 6, multiple=True, autocomplete=True),
                            self._select("movie_genres", "电影类型", filter_options["movie_genres"], 6, multiple=True, autocomplete=True),
                        ]),
                        self._row([
                            self._select("exclude_tv_genres", "排除剧集类型", filter_options["tv_genres"], 6, multiple=True, autocomplete=True),
                            self._select("exclude_movie_genres", "排除电影类型", filter_options["movie_genres"], 6, multiple=True, autocomplete=True),
                        ]),
                        self._row([
                            self._select("origin_countries", "原产国", filter_options["origin_countries"], 6, multiple=True, autocomplete=True),
                            self._select("original_languages", "原始语言", filter_options["original_languages"], 6, multiple=True, autocomplete=True),
                        ]),
                    ],
                },
            ],
        }

    def _blacklist_panel(self) -> dict:
        return {
            "component": "VExpansionPanel",
            "content": [
                {
                    "component": "VExpansionPanelTitle",
                    "text": "黑名单",
                },
                {
                    "component": "VExpansionPanelText",
                    "content": [
                        {
                            "component": "VAlert",
                            "props": {"type": "warning", "variant": "tonal", "class": "mb-3"},
                            "text": "命中黑名单的 TMDB ID 会在扫描最前面直接跳过，不拉详情、不生成建议、不自动订阅。",
                        },
                        self._textarea(
                            "blacklist",
                            "TMDB ID 黑名单",
                            "一行一个：60625；也支持 tv:60625、movie:12345",
                        ),
                    ],
                },
            ],
        }

    @staticmethod
    def _scan_status_panel(status: Dict[str, Any]) -> dict:
        running = bool((status or {}).get("running"))
        error = (status or {}).get("error")
        percent = int((status or {}).get("percent") or 0)
        message = (status or {}).get("message") or ("等待扫描" if not status else "-")
        started_at = (status or {}).get("started_at") or "-"
        finished_at = (status or {}).get("finished_at") or "-"
        color = "error" if error else ("primary" if running else "success")
        state_text = "运行中" if running else ("失败" if error else ("已完成" if status else "未开始"))
        meta = f"状态：{state_text}；开始：{started_at}"
        if not running and finished_at != "-":
            meta += f"；结束：{finished_at}"
        if error:
            meta += f"；错误：{error}"
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "color": color, "class": "mb-3"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-2 pa-3 pb-1"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "text-subtitle-1 pa-0"}, "text": "扫描状态"},
                        {"component": "VChip", "props": {"size": "small", "variant": "tonal", "color": color}, "text": state_text},
                    ],
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-2"},
                    "content": [
                        {"component": "div", "props": {"class": "text-body-2 mb-2"}, "text": message},
                        {
                            "component": "VProgressLinear",
                            "props": {
                                "model-value": percent,
                                "height": 8,
                                "rounded": True,
                                "striped": running,
                                "indeterminate": running and percent <= 1,
                                "color": color,
                            },
                        },
                        {"component": "div", "props": {"class": "text-caption mt-2"}, "text": meta},
                    ],
                },
            ],
        }

    @staticmethod
    def _page_toolbar(summary: Optional[Dict[str, Any]], config: Optional[Dict[str, Any]] = None) -> dict:
        if summary:
            subtitle = (
                f"上次扫描 {summary.get('finished_at', '-')}，"
                f"{summary.get('window_start', '-')} 至 {summary.get('window_end', '-')}；扫描后刷新详情页查看最新结果"
            )
            last_mode_text = "上次扫描：自动订阅" if summary.get("auto_subscribe") else "上次扫描：仅生成建议"
            last_mode_color = "success" if summary.get("auto_subscribe") else "info"
            current_auto = bool((config or {}).get("auto_subscribe"))
            current_mode_text = "当前配置：自动订阅" if current_auto else "当前配置：仅生成建议"
            current_mode_color = "success" if current_auto else "info"
        else:
            subtitle = "按当前配置立即拉取 TMDB 候选；未开启自动订阅时只生成建议，不会改动订阅。"
            last_mode_text = "等待扫描"
            last_mode_color = "info"
            current_mode_text = None
            current_mode_color = "info"
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-3"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap align-center justify-space-between ga-3 pa-3"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "min-w-0"},
                            "content": [
                                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-medium"}, "text": "TMDB 自动订阅"},
                                {"component": "div", "props": {"class": "text-body-2 text-medium-emphasis"}, "text": subtitle},
                            ],
                        },
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-wrap align-center ga-2"},
                            "content": [
                                *([{
                                    "component": "VChip",
                                    "props": {"color": current_mode_color, "variant": "tonal", "size": "small"},
                                    "text": current_mode_text,
                                }] if current_mode_text else []),
                                {
                                    "component": "VChip",
                                    "props": {"color": last_mode_color, "variant": "tonal", "size": "small"},
                                    "text": last_mode_text,
                                },
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "primary",
                                        "variant": "flat",
                                        "prepend-icon": "mdi-refresh",
                                    },
                                    "text": "立即扫描",
                                    "events": {
                                        "click": {
                                            "api": f"plugin/TmdbAutoSubscribe/scan?apikey={settings.API_TOKEN}",
                                            "method": "get",
                                        }
                                    },
                                },
                                {
                                    "component": "VBtn",
                                    "props": {
                                        "color": "warning",
                                        "variant": "tonal",
                                        "prepend-icon": "mdi-delete-sweep",
                                    },
                                    "text": "清空缓存",
                                    "events": {
                                        "click": {
                                            "api": f"plugin/TmdbAutoSubscribe/clear?apikey={settings.API_TOKEN}",
                                            "method": "get",
                                        }
                                    },
                                },
                            ],
                        },
                    ],
                }
            ],
        }

    @staticmethod
    def _metrics(
            summary: Dict[str, Any],
            config: Optional[Dict[str, Any]] = None,
            registry_count: int = 0,
    ) -> dict:
        last_mode = "自动订阅" if summary.get("auto_subscribe") else "仅生成建议"
        current_mode = "自动订阅" if (config or {}).get("auto_subscribe") else "仅生成建议"
        items = [
            ("缓存观察", str(registry_count), "info"),
            ("候选", str(summary.get("candidate_count", 0)), "primary"),
            ("建议", str(summary.get("actions_count", 0)), "success"),
            ("黑名单跳过", str(summary.get("blacklist_skip_count", 0)), "warning"),
            ("当前配置", current_mode, "success" if (config or {}).get("auto_subscribe") else "info"),
            ("上次扫描模式", last_mode, "success" if summary.get("auto_subscribe") else "info"),
            ("媒体", ",".join(summary.get("media_types") or []) or "-", "info"),
        ]
        return {
            "component": "VRow",
            "props": {"class": "mb-1"},
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 6, "md": 3},
                    "content": [{
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": color, "class": "pa-2"},
                        "content": [
                            {"component": "VCardSubtitle", "props": {"class": "pb-0"}, "text": title},
                            {"component": "VCardTitle", "props": {"class": "text-h6 pt-1"}, "text": value},
                        ],
                    }],
                }
                for title, value, color in items
            ],
        }

    @staticmethod
    def _queue_panel(queue_stats: Dict[str, Any]) -> dict:
        labels = {
            "movie": "电影上映",
            "tv_first_air": "新剧首播",
            "tv_airing": "近期播出",
        }
        if not queue_stats:
            text = "暂无队列诊断"
        else:
            parts = []
            for key, value in queue_stats.items():
                stop_reason = STOP_REASON_LABELS.get(value.get("stop_reason"), value.get("stop_reason", "-"))
                page_stats = value.get("page_stats") or []
                page_text = ""
                if page_stats:
                    recent_pages = page_stats[-3:]
                    page_text = "；近页 " + " / ".join(
                        f"P{page.get('page')} 新{page.get('new_ids', 0)} 已知{page.get('known_ids', 0)}"
                        for page in recent_pages
                    )
                parts.append(
                    f"{labels.get(key, key)}：{value.get('pages_fetched', 0)}/{value.get('total_pages', 0)} 页，"
                    f"总量 {value.get('total_results', 0)}，停止原因 {stop_reason}"
                    f"{'，错误 ' + str(value.get('error')) if value.get('error') else ''}{page_text}"
                )
            text = "；".join(parts)
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1 pb-0"}, "text": "拉取诊断"},
                {"component": "VCardText", "props": {"class": "pt-2 text-body-2"}, "text": text},
            ],
        }

    @staticmethod
    def _registry_panel(registry: Dict[str, Any]) -> dict:
        rows = sorted(
            (registry or {}).values(),
            key=lambda item: (item.get("last_seen") or item.get("first_seen") or ""),
            reverse=True,
        )[:12]
        if not rows:
            content = [{"component": "VCardText", "text": "暂无缓存观察数据"}]
        else:
            content = []
            for item in rows:
                signals = []
                if item.get("season_update_detected"):
                    signals.append("新季变化")
                if item.get("episode_update_detected"):
                    signals.append("同季新集")
                changed = item.get("changed_seasons") or []
                if changed:
                    changed_text = ",".join(
                        f"S{change.get('season')}" for change in changed[:3] if change.get("season")
                    )
                    if changed_text:
                        signals.append(changed_text)
                signal_text = " / ".join(signals) if signals else "无新增变化"
                last_change_seen_at = (
                    item.get("last_season_change_seen_at")
                    or item.get("last_episode_change_seen_at")
                    or "-"
                )
                subtitle = (
                    f"{REGISTRY_CLASS_LABELS.get(item.get('classification'), item.get('classification', '-'))} / "
                    f"出现 {item.get('seen_count', 0)} 次 / "
                    f"{REGISTRY_RESULT_LABELS.get(item.get('last_result'), item.get('last_result', '-'))} / "
                    f"{signal_text} / 发现 {last_change_seen_at}"
                )
                content.append({
                    "component": "VListItem",
                    "props": {
                        "title": item.get("title") or str(item.get("tmdb_id") or "-"),
                        "subtitle": subtitle,
                    },
                })
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1 pb-0"}, "text": "缓存观察"},
                {"component": "VList", "props": {"lines": "two", "density": "compact"}, "content": content},
            ],
        }

    @staticmethod
    def _poster_wall(title: str, rows: List[Dict[str, Any]], action: bool) -> dict:
        if not rows:
            body = [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VCard", "props": {"variant": "tonal"}, "content": [
                    {"component": "VCardText", "text": "暂无数据"}
                ]}
            ]}]
        else:
            body = []
            for item in rows:
                body.append({
                    "component": "VCol",
                    "props": {"cols": 12, "sm": 6, "md": 4, "lg": 3, "xl": 2},
                    "content": [TmdbAutoSubscribe._poster_card(item, action)],
                })
        return {
            "component": "VCard",
            "props": {"variant": "flat", "class": "mb-3"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "px-0"}, "text": title},
                {"component": "VRow", "content": body},
            ],
        }

    @staticmethod
    def _poster_card(item: Dict[str, Any], action: bool) -> dict:
        media_type = item.get("media_type") or "tv"
        tmdb_id = item.get("tmdb_id")
        title = item.get("title") or str(tmdb_id)
        kind = TmdbAutoSubscribe._action_kind_text(item.get("kind"), item.get("matched_actions"), media_type)
        date = item.get("date") or "-"
        subtitle = TmdbAutoSubscribe._poster_subtitle(item, kind, date)
        season_text = TmdbAutoSubscribe._season_title_text(item.get("season")) if media_type == "tv" else ""
        matched = action or bool(item.get("matched"))
        reason = TmdbAutoSubscribe._poster_reason(item, action)
        chip_color = "success" if matched else "grey"
        media_label = "电影" if media_type == "movie" else "剧集"
        media_color = "primary" if media_type == "movie" else "info"
        chip_text = "符合" if matched else "未命中"
        poster = item.get("poster_url")
        poster_node = {
            "component": "VImg",
            "props": {
                "src": poster,
                "aspect-ratio": "2/3",
                "cover": True,
                "class": "bg-grey-lighten-3",
                "style": {
                    "width": "100%",
                    "height": "100%",
                },
            },
        } if poster else {
            "component": "div",
            "props": {
                "class": "d-flex align-center justify-center bg-grey-lighten-3 text-caption text-medium-emphasis",
                "style": {"aspect-ratio": "2 / 3"},
            },
            "text": "无海报",
        }

        season_band = {
            "component": "div",
            "props": {
                "class": "text-caption font-weight-bold text-white px-2 d-flex align-center justify-center text-center",
                "style": {
                    "position": "absolute",
                    "left": "0",
                    "right": "0",
                    "bottom": "0",
                    "height": "28px",
                    "line-height": "1.2",
                    "background": "rgba(0, 0, 0, 0.62)",
                    "white-space": "nowrap",
                    "overflow": "hidden",
                    "text-overflow": "ellipsis",
                    "text-shadow": "0 1px 2px rgba(0, 0, 0, 0.75)",
                },
            },
            "text": season_text or "\u00a0",
        }
        status_badge = {
            "component": "VChip",
            "props": {
                "size": "small",
                "color": chip_color,
                "variant": "flat",
                "class": "font-weight-bold",
                "style": {
                    "position": "absolute",
                    "top": "6px",
                    "right": "6px",
                    "box-shadow": "0 2px 6px rgba(0, 0, 0, 0.28)",
                },
            },
            "text": chip_text,
        }
        poster_content = [poster_node]
        poster_content.append(status_badge)
        if season_text:
            poster_content.append(season_band)
        poster_node = {
            "component": "div",
            "props": {
                "class": "overflow-hidden bg-grey-lighten-3 position-relative",
                "style": {
                    "width": "100%",
                    "aspect-ratio": "2 / 3",
                    "flex": "0 0 auto",
                },
            },
            "content": poster_content,
        }

        title_text = str(title).strip()
        title_node = {
            "component": "a",
            "props": {
                "href": _tmdb_href(media_type, tmdb_id),
                "target": "_blank",
                "class": "text-decoration-none text-high-emphasis",
            },
            "text": title_text,
        }
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "h-100 overflow-hidden d-flex flex-column"},
            "content": [
                poster_node,
                {"component": "VCardTitle", "props": {
                    "class": "text-subtitle-2 pb-1",
                    "style": {
                        "white-space": "normal",
                        "line-height": "1.35",
                        "height": "48px",
                        "overflow": "hidden",
                        "display": "-webkit-box",
                        "-webkit-line-clamp": "2",
                        "-webkit-box-orient": "vertical",
                    },
                }, "content": [title_node]},
                {"component": "VCardSubtitle", "props": {
                    "class": "pt-0 text-caption",
                    "style": {
                        "white-space": "normal",
                        "overflow": "hidden",
                        "line-height": "1.35",
                        "height": "38px",
                    },
                }, "text": subtitle},
                {"component": "VCardText", "props": {
                    "class": "py-2 text-caption",
                    "style": {
                        "height": "40px",
                        "line-height": "1.35",
                        "overflow": "hidden",
                    },
                }, "text": reason},
                {"component": "VCardActions", "props": {
                    "class": "pt-0 mt-auto flex-nowrap overflow-hidden",
                    "style": {"height": "44px"},
                }, "content": [
                    {"component": "VChip", "props": {"size": "small", "color": media_color, "variant": "tonal"}, "text": media_label},
                    {"component": "VChip", "props": {"size": "small", "variant": "tonal"}, "text": f"TMDB {tmdb_id}"},
                ]},
            ],
        }

    @staticmethod
    def _poster_subtitle(item: Dict[str, Any], kind: str, date: str) -> str:
        return " / ".join(str(part) for part in (kind, date) if part not in (None, ""))

    @staticmethod
    def _season_title_text(value: Any) -> str:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return ""
        if number <= 0:
            return ""
        digits = "零一二三四五六七八九"
        if number <= 10:
            text = "十" if number == 10 else digits[number]
        elif number < 20:
            text = f"十{digits[number % 10]}"
        elif number < 100:
            tens, ones = divmod(number, 10)
            text = f"{digits[tens]}十{digits[ones] if ones else ''}"
        else:
            text = str(number)
        return f"第{text}季"

    @staticmethod
    def _poster_reason(item: Dict[str, Any], action: bool) -> str:
        message = item.get("subscribe_message")
        if message:
            return message
        if action:
            if item.get("subscribed"):
                return "已订阅"
            return "仅扫描未订阅"
        return item.get("debug_reason") or "-"

    @staticmethod
    def _action_kind_text(kind: Optional[str], matched_actions: Optional[List[str]], media_type: str) -> str:
        values = [kind] if kind else list(matched_actions or [])
        if not values:
            values = [media_type]
        return ",".join(ACTION_KIND_LABELS.get(value, value) for value in values)
