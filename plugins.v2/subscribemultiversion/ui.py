from collections.abc import Iterable, Sequence
from datetime import datetime, timezone, tzinfo
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .diagnostics import redact_diagnostic
from .domain import CompanionTask, DownloadContextSnapshot, PluginConfig, TaskStatus


_STATUS_LABELS = {
    TaskStatus.WAITING.value: "待匹配",
    TaskStatus.MATCHING.value: "匹配中",
    TaskStatus.ADDING.value: "添加中",
    TaskStatus.ADDED.value: "已添加",
    TaskStatus.EXPIRED.value: "已过期",
    TaskStatus.OUT_OF_SCOPE.value: "超出范围",
}

_PAGE_HEADERS = [
    {"title": "订阅", "key": "title"},
    {"title": "季/集", "key": "season_episode"},
    {"title": "二级分类", "key": "category"},
    {"title": "范围原因", "key": "scope_reason"},
    {"title": "状态", "key": "status"},
    {"title": "创建时间", "key": "created_at"},
    {"title": "截止/剩余时间", "key": "deadline"},
    {"title": "候选标题", "key": "candidate_title"},
    {"title": "DV 版本", "key": "dv_variant"},
    {"title": "DV 排名", "key": "dv_rank"},
    {"title": "DV 依据", "key": "dv_evidence"},
    {"title": "重试次数", "key": "retry_count"},
    {"title": "最后错误", "key": "last_error"},
]

_OPTION_TITLE_LIMIT = 160
_SUBSCRIPTION_TITLE_LIMIT = 200
_TASK_TITLE_LIMIT = 200
_CATEGORY_LIMIT = 120
_SCOPE_REASON_LIMIT = 240
_CANDIDATE_TITLE_LIMIT = 300
_DV_EVIDENCE_LIMIT = 240
_ALERT_TEXT_LIMIT = 1000
_ERROR_TEXT_LIMIT = 1000


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


def _text(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    result = str(value).strip()
    return result or fallback


def _error_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _text_options(values: Iterable[Any]) -> list[dict[str, str]]:
    options = []
    seen = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        options.append(
            {
                "title": _truncate(text, _OPTION_TITLE_LIMIT),
                "value": text,
            }
        )
    return options


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return None


def _subscription_options(subscriptions: Iterable[Any]) -> list[dict[str, Any]]:
    options = []
    seen = set()
    for subscription in subscriptions:
        subscription_id = _integer(
            getattr(
                subscription,
                "subscription_id",
                getattr(subscription, "id", None),
            )
        )
        if subscription_id is None or subscription_id in seen:
            continue
        seen.add(subscription_id)
        season = _integer(getattr(subscription, "season", None))
        if season is None:
            season = 0
        name = _text(getattr(subscription, "name", None))
        year = _text(getattr(subscription, "year", None), "未知年份")
        category = _text(
            getattr(
                subscription,
                "category",
                getattr(subscription, "media_category", None),
            ),
            "未分类",
        )
        options.append(
            {
                "title": _truncate(
                    f"{name} ({year}) S{season:02d} · {category}",
                    _SUBSCRIPTION_TITLE_LIMIT,
                ),
                "value": subscription_id,
            }
        )
    return options


def _control_column(
    component: str,
    props: dict[str, Any],
    *,
    md: int,
) -> dict[str, Any]:
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": md},
        "content": [{"component": component, "props": props}],
    }


def _provider_warning(warnings: Optional[Iterable[Any]]) -> Optional[dict[str, Any]]:
    names = []
    seen = set()
    for warning in warnings or ():
        name = _text(warning, "")
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return None
    return {
        "component": "VAlert",
        "props": {
            "type": "warning",
            "variant": "tonal",
            "density": "compact",
            "text": _truncate(
                (
                    f"部分选项加载失败：{'、'.join(names)}。"
                    "已保留当前配置值。"
                ),
                _ALERT_TEXT_LIMIT,
            ),
        },
    }


def build_form(
    config: PluginConfig,
    categories: Iterable[Any],
    subscriptions: Iterable[Any],
    warnings: Optional[Iterable[Any]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    form = {
        "component": "VForm",
        "content": [
            {
                "component": "VRow",
                "content": [
                    _control_column(
                        "VSwitch",
                        {"model": "enabled", "label": "启用插件"},
                        md=3,
                    ),
                    _control_column(
                        "VSelect",
                        {
                            "model": "categories",
                            "label": "二级分类",
                            "items": _text_options(categories),
                            "multiple": True,
                            "chips": True,
                        },
                        md=6,
                    ),
                    _control_column(
                        "VAutocomplete",
                        {
                            "model": "watch_subscription_ids",
                            "label": "电视剧订阅",
                            "items": _subscription_options(subscriptions),
                            "multiple": True,
                            "chips": True,
                        },
                        md=6,
                    ),
                    _control_column(
                        "VTextField",
                        {
                            "model": "watch_days",
                            "label": "监听天数",
                            "type": "number",
                            "min": 0.01,
                            "step": 0.5,
                        },
                        md=3,
                    ),
                ],
            }
        ],
    }
    warning = _provider_warning(warnings)
    elements = [form] if warning is None else [warning, form]
    defaults = {
        "enabled": config.enabled,
        "categories": list(config.categories),
        "watch_subscription_ids": list(config.watch_subscription_ids),
        "watch_days": config.watch_days,
    }
    return elements, defaults


def _datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _display_timezone(timezone_name: Any) -> tzinfo:
    try:
        return ZoneInfo(str(timezone_name))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return timezone.utc


def _format_datetime(value: Any, display_timezone: tzinfo) -> str:
    parsed = _datetime(value)
    if parsed is None:
        return "-"
    localized = parsed.astimezone(display_timezone)
    offset = localized.strftime("%z")
    explicit_offset = f"UTC{offset[:3]}:{offset[3:]}"
    return f"{localized:%Y-%m-%d %H:%M:%S} {explicit_offset}"


def _status_value(status: Any) -> str:
    value = getattr(status, "value", status)
    return str(value)


def _status_label(status: Any) -> str:
    value = _status_value(status)
    return _STATUS_LABELS.get(value, value)


def _dv_code_label(value: Any) -> str:
    if value in (None, "unknown"):
        return "未知"
    return _text(value, "未知").upper()


def _remaining_text(
    task: CompanionTask,
    deadline: datetime,
    now: Optional[datetime],
) -> str:
    status = _status_value(task.status)
    if status == TaskStatus.ADDED.value:
        return "已完成"
    if status == TaskStatus.EXPIRED.value:
        return "已过期"
    if now is None:
        return "剩余时间未知"
    try:
        remaining_seconds = int((deadline - now).total_seconds())
    except TypeError:
        return "剩余时间未知"
    if remaining_seconds <= 0:
        return "已过期"
    days, seconds = divmod(remaining_seconds, 24 * 60 * 60)
    hours, seconds = divmod(seconds, 60 * 60)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    if not parts:
        parts.append("不足1分钟")
    return f"剩余 {' '.join(parts)}"


def _deadline_text(
    task: CompanionTask,
    now: Optional[datetime],
    display_timezone: tzinfo,
) -> str:
    deadline = _datetime(task.deadline_at)
    if deadline is None:
        return "-"
    return (
        f"{_format_datetime(deadline, display_timezone)} · "
        f"{_remaining_text(task, deadline, now)}"
    )


def _task_item(
    task: CompanionTask,
    snapshot: Optional[DownloadContextSnapshot],
    now: Optional[datetime],
    display_timezone: tzinfo,
) -> dict[str, Any]:
    profile = _dv_code_label(task.candidate_profile)
    layer = _dv_code_label(task.candidate_layer)
    source = {
        "remux": "Remux",
        "web_dl": "WEB-DL",
        "other": "Other",
    }.get(task.candidate_source, "未知")
    evidence = _truncate(
        ", ".join(task.candidate_evidence) or "-",
        _DV_EVIDENCE_LIMIT,
    )
    return {
        "title": _truncate(
            _text(getattr(snapshot, "name", None)),
            _TASK_TITLE_LIMIT,
        ),
        "season_episode": f"S{task.season:02d}E{task.episode:02d}",
        "category": _truncate(
            _text(getattr(snapshot, "category", None), "未分类"),
            _CATEGORY_LIMIT,
        ),
        "scope_reason": _truncate(
            _text(task.scope_reason),
            _SCOPE_REASON_LIMIT,
        ),
        "status": (
            "失败重试"
            if task.status is TaskStatus.WAITING
            and (task.retry_count > 0 or bool(task.last_error))
            else _status_label(task.status)
        ),
        "created_at": _format_datetime(task.created_at, display_timezone),
        "deadline": _deadline_text(task, now, display_timezone),
        "candidate_title": _truncate(
            _text(task.candidate_title),
            _CANDIDATE_TITLE_LIMIT,
        ),
        "dv_variant": f"{profile} / {layer} / {source}",
        "dv_rank": task.candidate_rank if task.candidate_rank is not None else "-",
        "dv_evidence": evidence,
        "retry_count": task.retry_count,
        "last_error": _truncate(
            _error_text(redact_diagnostic(task.last_error)),
            _ERROR_TEXT_LIMIT,
        ),
    }


def build_page(
    tasks: Sequence[
        tuple[CompanionTask, Optional[DownloadContextSnapshot]]
    ],
    refresh_error: Optional[str],
    now_iso: str,
    timezone_name: str = "UTC",
) -> list[dict[str, Any]]:
    nodes = []
    if refresh_error:
        nodes.append(
            {
                "component": "VAlert",
                "props": {
                    "type": "error",
                    "variant": "tonal",
                    "density": "compact",
                    "text": _truncate(
                        redact_diagnostic(refresh_error),
                        _ALERT_TEXT_LIMIT,
                    ),
                },
            }
        )
    now = _datetime(now_iso)
    display_timezone = _display_timezone(timezone_name)
    nodes.append(
        {
            "component": "VDataTable",
            "props": {
                "headers": [dict(header) for header in _PAGE_HEADERS],
                "items": [
                    _task_item(task, snapshot, now, display_timezone)
                    for task, snapshot in tasks
                ],
                "density": "compact",
                "items-per-page": 25,
                "items-per-page-options": [10, 25, 50, 100],
            },
        }
    )
    return nodes
