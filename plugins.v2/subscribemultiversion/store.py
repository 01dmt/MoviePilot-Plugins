import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

from .diagnostics import redact_diagnostic
from .domain import (
    SCHEMA_VERSION,
    CompanionTask,
    DownloadContextSnapshot,
    TaskStatus,
)


_MAX_TERMINAL_TASKS = 200
_UNFINISHED_STATUSES = frozenset(
    {
        TaskStatus.WAITING,
        TaskStatus.MATCHING,
        TaskStatus.ADDING,
        TaskStatus.OUT_OF_SCOPE,
    }
)
_TERMINAL_STATUSES = frozenset({TaskStatus.ADDED, TaskStatus.EXPIRED})
_IN_FLIGHT_STATUSES = frozenset({TaskStatus.MATCHING, TaskStatus.ADDING})
_UNSET = object()


def _load_collection(payload: dict, field_name: str) -> dict:
    value = payload.get(field_name)
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError(f"Invalid task store {field_name}: expected dict")
    return value


class TaskStore:
    def __init__(
        self, load: Callable[[], Optional[dict]], save: Callable[[dict], None]
    ):
        self._load = load
        self._save = save
        self._lock = threading.RLock()
        self._persisting = False
        self._mutation_depth = 0
        with self._lock:
            loaded = load()
            if loaded is None:
                payload = {}
            elif type(loaded) is not dict:
                raise ValueError("Invalid task store payload: expected dict")
            else:
                payload = loaded
            version = payload.get("schema_version", 1)
            if type(version) is not int or version not in (1, SCHEMA_VERSION):
                raise ValueError(f"Unsupported task schema version: {version}")
            tasks_payload = _load_collection(payload, "tasks")
            snapshots_payload = _load_collection(payload, "snapshots")
            completion_snapshots_payload = _load_collection(
                payload, "completion_snapshots"
            )
            self._tasks = {
                key: CompanionTask.from_dict(value)
                for key, value in tasks_payload.items()
            }
            for key, task in tuple(self._tasks.items()):
                if task.last_error is not None:
                    self._tasks[key] = task.evolve(
                        last_error=redact_diagnostic(task.last_error)
                    )
            self._snapshots = {
                key: DownloadContextSnapshot.from_dict(value)
                for key, value in snapshots_payload.items()
            }
            completion_snapshots = (
                DownloadContextSnapshot.from_dict(value)
                for value in completion_snapshots_payload.values()
            )
            self._completion_snapshots = {
                str(snapshot.subscription_id): snapshot
                for snapshot in completion_snapshots
            }
            self._last_seen_finished_at = payload.get("last_seen_finished_at")
            loaded_refresh_error = payload.get("last_refresh_error")
            self._last_refresh_error = (
                redact_diagnostic(loaded_refresh_error)
                if loaded_refresh_error is not None
                else None
            )
            for key, task in tuple(self._tasks.items()):
                if task.status in _IN_FLIGHT_STATUSES:
                    self._tasks[key] = task.evolve(
                        status=TaskStatus.WAITING,
                        claim_token=None,
                    )
                elif task.claim_token is not None:
                    self._tasks[key] = task.evolve(claim_token=None)
            self._persist()

    def _capture_state(self) -> tuple:
        return (
            self._tasks.copy(),
            self._snapshots.copy(),
            self._completion_snapshots.copy(),
            self._last_seen_finished_at,
            self._last_refresh_error,
        )

    def _restore_state(self, state: tuple) -> None:
        (
            self._tasks,
            self._snapshots,
            self._completion_snapshots,
            self._last_seen_finished_at,
            self._last_refresh_error,
        ) = state

    def _state_changed(self, state: tuple) -> bool:
        tasks, snapshots, completion_snapshots, finished_at, refresh_error = state
        return (
            tuple(self._tasks.items()) != tuple(tasks.items())
            or tuple(self._snapshots.items()) != tuple(snapshots.items())
            or tuple(self._completion_snapshots.items())
            != tuple(completion_snapshots.items())
            or self._last_seen_finished_at != finished_at
            or self._last_refresh_error != refresh_error
        )

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._lock:
            if self._persisting:
                raise RuntimeError("Task store persistence is already in progress")
            before = self._capture_state()
            is_outermost = self._mutation_depth == 0
            self._mutation_depth += 1
            try:
                yield
                if is_outermost and self._state_changed(before):
                    self._persist()
            except BaseException:
                self._restore_state(before)
                raise
            finally:
                self._mutation_depth -= 1

    def _persist(self) -> None:
        with self._lock:
            if self._persisting:
                raise RuntimeError("Task store persistence is already in progress")
            self._persisting = True
            try:
                self._prune_terminal()
                self._save(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "tasks": {
                            key: task.to_dict() for key, task in self._tasks.items()
                        },
                        "snapshots": {
                            key: value.to_dict()
                            for key, value in self._snapshots.items()
                        },
                        "completion_snapshots": {
                            key: value.to_dict()
                            for key, value in self._completion_snapshots.items()
                        },
                        "last_seen_finished_at": self._last_seen_finished_at,
                        "last_refresh_error": self._last_refresh_error,
                    }
                )
            finally:
                self._persisting = False

    def _prune_terminal(self) -> None:
        # The bounded terminal history intentionally does not retain tombstones.
        newest_terminal = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status in _TERMINAL_STATUSES
            ),
            key=lambda task: (task.updated_at, task.key),
            reverse=True,
        )[:_MAX_TERMINAL_TASKS]
        retained_terminal_keys = {task.key for task in newest_terminal}
        self._tasks = {
            key: task
            for key, task in self._tasks.items()
            if task.status in _UNFINISHED_STATUSES or key in retained_terminal_keys
        }

        newest_completion_keys = set(
            tuple(self._completion_snapshots)[-_MAX_TERMINAL_TASKS:]
        )
        referenced_snapshot_keys = {task.snapshot_key for task in self._tasks.values()}
        self._completion_snapshots = {
            key: snapshot
            for key, snapshot in self._completion_snapshots.items()
            if key in newest_completion_keys
            or snapshot.snapshot_key in referenced_snapshot_keys
        }

    def upsert(
        self,
        snapshot: DownloadContextSnapshot,
        episodes: Iterable[int],
        now: datetime,
        watch_days: float,
        scope_reason: str,
        source_hash: Optional[str],
        source_title: Optional[str],
    ) -> tuple[CompanionTask, ...]:
        with self._mutation():
            group_exists = any(
                task.snapshot_key == snapshot.snapshot_key
                for task in self._tasks.values()
            )
            if not group_exists or snapshot.snapshot_key not in self._snapshots:
                self._snapshots[snapshot.snapshot_key] = snapshot
            tasks = []
            seen_keys = set()
            for episode in episodes:
                candidate = CompanionTask.create(
                    snapshot=snapshot,
                    episode=episode,
                    now=now,
                    watch_days=watch_days,
                    source_hash=source_hash,
                    source_title=source_title,
                    scope_reason=scope_reason,
                )
                if candidate.key in seen_keys:
                    continue
                seen_keys.add(candidate.key)
                task = self._tasks.get(candidate.key)
                if task is None:
                    task = candidate
                    self._tasks[task.key] = task
                tasks.append(task)
            return tuple(tasks)

    def get(self, task_key: str) -> Optional[CompanionTask]:
        with self._lock:
            return self._tasks.get(task_key)

    def snapshot(self, snapshot_key: str) -> Optional[DownloadContextSnapshot]:
        with self._lock:
            return self._snapshots.get(snapshot_key)

    def save_snapshot(self, snapshot: DownloadContextSnapshot) -> None:
        with self._mutation():
            self._snapshots[snapshot.snapshot_key] = snapshot

    def known_snapshots(self) -> tuple[DownloadContextSnapshot, ...]:
        with self._lock:
            snapshots_by_subscription = {
                snapshot.subscription_id: snapshot
                for snapshot in self._completion_snapshots.values()
            }
            snapshots_by_subscription.update(
                {
                    snapshot.subscription_id: snapshot
                    for snapshot in self._snapshots.values()
                }
            )
            return tuple(snapshots_by_subscription.values())

    def save_completion_snapshot(self, snapshot: DownloadContextSnapshot) -> None:
        with self._mutation():
            key = str(snapshot.subscription_id)
            self._completion_snapshots.pop(key, None)
            self._completion_snapshots[key] = snapshot

    def completion_snapshot(
        self, subscription_id: int
    ) -> Optional[DownloadContextSnapshot]:
        with self._lock:
            return self._completion_snapshots.get(str(subscription_id))

    def has_unfinished(self) -> bool:
        with self._lock:
            return any(
                task.status in _UNFINISHED_STATUSES for task in self._tasks.values()
            )

    def actionable(self, now: datetime) -> tuple[CompanionTask, ...]:
        with self._lock:
            return tuple(
                task
                for task in self._tasks.values()
                if task.status is TaskStatus.WAITING and task.deadline_at > now
            )

    def transition(
        self,
        task_keys: Iterable[str],
        status: TaskStatus,
        now: datetime,
        *,
        candidate_fingerprint: Any = _UNSET,
        candidate_title: Any = _UNSET,
        candidate_profile: Any = _UNSET,
        candidate_layer: Any = _UNSET,
        candidate_source: Any = _UNSET,
        candidate_rank: Any = _UNSET,
        candidate_evidence: Any = _UNSET,
        last_error: Any = _UNSET,
        claim_token: Any = _UNSET,
        expected_statuses: Optional[Iterable[TaskStatus]] = None,
        expected_claim_token: Any = _UNSET,
    ) -> tuple[CompanionTask, ...]:
        with self._mutation():
            expected = (
                frozenset(expected_statuses) if expected_statuses is not None else None
            )
            changed = []
            for key in dict.fromkeys(task_keys):
                task = self._tasks.get(key)
                if (
                    task is None
                    or task.status in _TERMINAL_STATUSES
                    or (expected is not None and task.status not in expected)
                    or (
                        expected_claim_token is not _UNSET
                        and task.claim_token != expected_claim_token
                    )
                ):
                    continue
                changes = {"status": status, "updated_at": now}
                if candidate_fingerprint is not _UNSET:
                    changes["candidate_fingerprint"] = candidate_fingerprint
                if candidate_title is not _UNSET:
                    changes["candidate_title"] = candidate_title
                if candidate_profile is not _UNSET:
                    changes["candidate_profile"] = candidate_profile
                if candidate_layer is not _UNSET:
                    changes["candidate_layer"] = candidate_layer
                if candidate_source is not _UNSET:
                    changes["candidate_source"] = candidate_source
                if candidate_rank is not _UNSET:
                    changes["candidate_rank"] = candidate_rank
                if candidate_evidence is not _UNSET:
                    changes["candidate_evidence"] = candidate_evidence
                if last_error is not _UNSET:
                    safe_last_error = (
                        redact_diagnostic(last_error)
                        if last_error is not None
                        else None
                    )
                    changes["last_error"] = safe_last_error
                    if status is TaskStatus.WAITING and safe_last_error:
                        changes["retry_count"] = task.retry_count + 1
                if status not in _IN_FLIGHT_STATUSES:
                    changes["claim_token"] = None
                elif claim_token is not _UNSET:
                    changes["claim_token"] = claim_token
                updated = task.evolve(**changes)
                if updated == task:
                    continue
                self._tasks[key] = updated
                changed.append(updated)
            return tuple(changed)

    def reconcile_scope(
        self, policy_or_decide: Any, now: datetime
    ) -> tuple[CompanionTask, ...]:
        with self._mutation():
            decide = getattr(policy_or_decide, "decide", policy_or_decide)
            changed = []
            for key, task in tuple(self._tasks.items()):
                if (
                    task.status not in _UNFINISHED_STATUSES
                    or task.status is TaskStatus.ADDING
                ):
                    continue
                snapshot = self._snapshots.get(task.snapshot_key)
                if snapshot is None:
                    continue
                decision = decide(snapshot)
                explicit_in_scope = getattr(decision, "in_scope", _UNSET)
                in_scope = (
                    bool(decision)
                    if explicit_in_scope is _UNSET
                    else bool(explicit_in_scope)
                )
                reason = str(getattr(decision, "reason", "") or "")
                updated = None
                if task.status is TaskStatus.OUT_OF_SCOPE:
                    if in_scope and task.deadline_at > now:
                        updated = task.evolve(
                            status=TaskStatus.WAITING,
                            scope_reason=reason,
                            updated_at=now,
                            claim_token=None,
                        )
                    elif in_scope:
                        updated = task.evolve(
                            status=TaskStatus.EXPIRED,
                            updated_at=now,
                            claim_token=None,
                        )
                elif not in_scope:
                    updated = task.evolve(
                        status=TaskStatus.OUT_OF_SCOPE,
                        scope_reason=reason,
                        updated_at=now,
                        claim_token=None,
                    )
                if updated is not None:
                    self._tasks[key] = updated
                    changed.append(updated)
            return tuple(changed)

    def expire(self, now: datetime) -> tuple[CompanionTask, ...]:
        with self._mutation():
            expired = []
            for key, task in tuple(self._tasks.items()):
                if (
                    task.status in _UNFINISHED_STATUSES
                    and task.status is not TaskStatus.ADDING
                    and task.deadline_at <= now
                ):
                    updated = task.evolve(
                        status=TaskStatus.EXPIRED,
                        updated_at=now,
                        claim_token=None,
                    )
                    self._tasks[key] = updated
                    expired.append(updated)
            return tuple(expired)

    def set_refresh_result(
        self,
        last_seen_finished_at: Optional[str],
        last_refresh_error: Optional[str],
    ) -> None:
        with self._mutation():
            self._last_seen_finished_at = last_seen_finished_at
            self._last_refresh_error = (
                redact_diagnostic(last_refresh_error)
                if last_refresh_error is not None
                else None
            )

    def refresh_state(self) -> tuple[Optional[str], Optional[str]]:
        with self._lock:
            return self._last_seen_finished_at, self._last_refresh_error

    def tasks_for_page(
        self,
    ) -> tuple[tuple[CompanionTask, Optional[DownloadContextSnapshot]], ...]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda task: (task.updated_at, task.key),
                reverse=True,
            )
            return tuple(
                (task, self._snapshots.get(task.snapshot_key)) for task in tasks
            )
