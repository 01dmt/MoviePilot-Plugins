from dataclasses import dataclass
from typing import Any, Optional


_MISSING = object()
_INVALID_PROGRESS_ERROR = "subscribe_refresh progress is invalid"
_INVALID_FINISHED_AT_ERROR = "subscribe_refresh finished_at is invalid"
_FAILED_ERROR = "subscribe_refresh failed"


def _attribute(progress: Any, name: str) -> Any:
    try:
        return getattr(progress, name)
    except Exception:
        return _MISSING


def _failure_error(error: Any) -> str:
    if isinstance(error, str) and error.strip():
        return error.strip()
    return _FAILED_ERROR


@dataclass(frozen=True)
class RefreshDecision:
    should_scan: bool
    finished_at: Optional[str] = None
    error: Optional[str] = None


class RefreshObserver:
    def inspect(self, progress: Any, last_seen: Optional[str]) -> RefreshDecision:
        if progress is None:
            return RefreshDecision(
                False, error="subscribe_refresh progress is unavailable"
            )

        status = _attribute(progress, "status")
        success = _attribute(progress, "success")
        finished_at = _attribute(progress, "finished_at")
        error = _attribute(progress, "error")
        if status is _MISSING or success is _MISSING or error is _MISSING:
            return RefreshDecision(False, error=_INVALID_PROGRESS_ERROR)
        if finished_at is _MISSING:
            return RefreshDecision(False, error=_INVALID_FINISHED_AT_ERROR)
        if not isinstance(status, str) or status not in {
            "waiting",
            "running",
            "success",
            "failed",
        }:
            return RefreshDecision(False, error=_INVALID_PROGRESS_ERROR)
        if status != "failed" and error is not None:
            if not isinstance(error, str) or error.strip():
                return RefreshDecision(False, error=_INVALID_PROGRESS_ERROR)

        if status in {"waiting", "running"}:
            if success is not None:
                return RefreshDecision(False, error=_INVALID_PROGRESS_ERROR)
            return RefreshDecision(False)
        if status == "failed":
            if success is not False:
                return RefreshDecision(False, error=_INVALID_PROGRESS_ERROR)
            return RefreshDecision(False, error=_failure_error(error))
        if success is not True:
            return RefreshDecision(False, error=_INVALID_PROGRESS_ERROR)
        if not isinstance(finished_at, str) or not finished_at.strip():
            return RefreshDecision(False, error=_INVALID_FINISHED_AT_ERROR)
        if finished_at == last_seen:
            return RefreshDecision(False, finished_at=finished_at)
        return RefreshDecision(True, finished_at=finished_at)
