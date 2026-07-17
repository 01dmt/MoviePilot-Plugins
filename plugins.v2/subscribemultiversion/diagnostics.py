import re
from typing import Any


_URL_PATTERN = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\b(authorization)([\"']?\s*[:=]\s*)"
    r"(?:(?:basic|bearer)\s+)?"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b("
    r"(?:[a-z0-9]+[_-])*"
    r"(?:api[_-]?key|cookie|credentials?|password|passwd|pwd|secret|session|token)"
    r"(?:[_-][a-z0-9]+)*"
    r")"
    r"([\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact_diagnostic(value: Any) -> str:
    if value is None:
        return ""
    text = _URL_PATTERN.sub("<redacted-url>", str(value))
    text = _AUTHORIZATION_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    return _SENSITIVE_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )


def exception_diagnostic(exc: BaseException, limit: int) -> str:
    message = redact_diagnostic(exc)
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text[:limit]
