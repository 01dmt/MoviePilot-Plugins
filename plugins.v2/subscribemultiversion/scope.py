from dataclasses import dataclass

from .domain import DownloadContextSnapshot, PluginConfig


@dataclass(frozen=True)
class ScopeDecision:
    in_scope: bool
    reason: str = ""


class ScopePolicy:
    def __init__(self, config: PluginConfig):
        self._categories = set(config.categories)
        self._subscription_ids = set(config.watch_subscription_ids)
        self._snapshot_keys = set(config.watch_snapshot_keys)

    def decide(self, snapshot: DownloadContextSnapshot) -> ScopeDecision:
        if snapshot.category and snapshot.category in self._categories:
            return ScopeDecision(True, "category")
        if (
            snapshot.subscription_id in self._subscription_ids
            or snapshot.snapshot_key in self._snapshot_keys
        ):
            return ScopeDecision(True, "watchlist")
        return ScopeDecision(False, "")
