import json
import logging
import threading
from uuid import uuid4

from .diagnostics import exception_diagnostic
from .domain import TaskStatus
from .episodes import candidate_episodes
from .scope import ScopePolicy


logger = logging.getLogger(__name__)

_IN_FLIGHT_STATUSES = frozenset({TaskStatus.MATCHING, TaskStatus.ADDING})
_MAX_ERROR_LENGTH = 1000


class CompanionService:
    def __init__(self, store, gateway, config_provider, clock):
        self._store = store
        self._gateway = gateway
        self._config_provider = config_provider
        self._clock = clock
        self._scan_lock = threading.Lock()
        self._pending_recoveries = {}
        self._pending_outcomes = {}

    def ingest(
        self,
        snapshot,
        episodes,
        source_context,
        source_hash=None,
        source_title=None,
    ):
        config = self._config_provider()
        decision = ScopePolicy(config).decide(snapshot)
        if not decision.in_scope:
            return ()
        if self._gateway.source_matches_target(source_context):
            return ()

        tasks = self._store.upsert(
            snapshot,
            episodes,
            self._clock(),
            config.watch_days,
            decision.reason,
            source_hash,
            source_title,
        )
        if tasks:
            self.scan("download_added")
        return tasks

    def scan(self, reason):
        with self._scan_lock:
            now = self._clock()
            config = self._config_provider()
            self._flush_pending(now)
            self._store.reconcile_scope(ScopePolicy(config), now)
            self._store.expire(now)
            actionable = self._store.actionable(now)
            if not actionable:
                return True

            try:
                cache = self._gateway.load_cache()
            except Exception as exc:
                logger.warning(
                    "SubscribeMultiVersion cache read failed: error_type=%s",
                    type(exc).__name__,
                )
                return False

            groups = {}
            for task in actionable:
                groups.setdefault((task.snapshot_key, task.season), []).append(task)

            for (snapshot_key, season), tasks in groups.items():
                task_keys = tuple(task.key for task in tasks)
                snapshot = self._store.snapshot(snapshot_key)
                if snapshot is None:
                    self._record_waiting_error(
                        task_keys,
                        now,
                        ValueError("Task snapshot is unavailable"),
                        "missing_snapshot",
                    )
                    continue

                pending = {task.episode for task in tasks}
                try:
                    candidates = self._gateway.ranked_candidates(snapshot, cache)
                    usable = []
                    covered_episodes = set()
                    for candidate in candidates:
                        context = candidate.context
                        coverage = candidate_episodes(context, season, pending)
                        if not coverage:
                            continue
                        context.allowed_episodes = coverage
                        usable.append((candidate, coverage))
                        covered_episodes.update(coverage)
                except Exception as exc:
                    self._record_waiting_error(task_keys, now, exc, "candidate_filter")
                    continue

                if not usable:
                    continue

                covered_keys = tuple(
                    task.key for task in tasks if task.episode in covered_episodes
                )
                claim_token = uuid4().hex
                try:
                    claimed = self._transition(
                        covered_keys,
                        TaskStatus.MATCHING,
                        now,
                        reason="candidate_claim",
                        last_error=None,
                        claim_token=claim_token,
                        expected_statuses={TaskStatus.WAITING},
                        expected_claim_token=None,
                    )
                except Exception:
                    self._recover_before_add_failure(
                        covered_keys, claim_token, now, "claim_persistence_failure"
                    )
                    raise
                if not claimed:
                    continue

                claimed_keys = tuple(task.key for task in claimed)
                claimed_episodes = {task.episode for task in claimed}
                try:
                    metadata_plan = self._metadata_plan(
                        usable, claimed, claimed_episodes
                    )
                except Exception as exc:
                    self._recover_owned(
                        claimed_keys,
                        claim_token,
                        now,
                        reason="candidate_fingerprint",
                        last_error=self._error_text(exc),
                        expected_statuses={TaskStatus.MATCHING},
                    )
                    self._log_group_failure(claimed_keys, "candidate_fingerprint", exc)
                    continue

                adding_by_key = {}
                try:
                    for (
                        keys,
                        fingerprint,
                        candidate_title,
                        classification,
                    ) in metadata_plan:
                        changed = self._transition(
                            keys,
                            TaskStatus.ADDING,
                            now,
                            reason="download_claim",
                            candidate_fingerprint=fingerprint,
                            candidate_title=candidate_title,
                            candidate_profile=classification.profile,
                            candidate_layer=classification.layer,
                            candidate_source=classification.source,
                            candidate_rank=classification.rank,
                            candidate_evidence=classification.evidence,
                            expected_statuses={TaskStatus.MATCHING},
                            expected_claim_token=claim_token,
                        )
                        adding_by_key.update({task.key: task for task in changed})

                    unadvanced_keys = tuple(
                        key for key in claimed_keys if key not in adding_by_key
                    )
                    if unadvanced_keys:
                        self._recover_owned(
                            unadvanced_keys,
                            claim_token,
                            now,
                            reason="claim_not_advanced",
                            last_error=None,
                            expected_statuses={TaskStatus.MATCHING},
                        )
                except Exception:
                    self._recover_before_add_failure(
                        claimed_keys,
                        claim_token,
                        now,
                        "adding_persistence_failure",
                    )
                    raise

                adding = tuple(
                    current
                    for task in claimed
                    if (current := self._store.get(task.key)) is not None
                    and current.status is TaskStatus.ADDING
                    and current.claim_token == claim_token
                )
                if not adding:
                    continue

                adding_episodes = {task.episode for task in adding}
                final_contexts = self._contexts_for_episodes(usable, adding_episodes)
                if not final_contexts:
                    self._recover_owned(
                        (task.key for task in adding),
                        claim_token,
                        now,
                        reason="empty_add_coverage",
                        last_error=None,
                        expected_statuses={TaskStatus.ADDING},
                    )
                    continue

                source = "SubscribeMultiVersion|" + json.dumps(
                    {
                        "snapshot_key": snapshot.snapshot_key,
                        "episodes": sorted(adding_episodes),
                    },
                    ensure_ascii=False,
                )
                try:
                    added_episodes = self._validated_added_episodes(
                        self._gateway.add(
                            snapshot,
                            final_contexts,
                            adding_episodes,
                            source,
                        ),
                        adding_episodes,
                    )
                except Exception as exc:
                    adding_keys = tuple(task.key for task in adding)
                    self._recover_owned(
                        adding_keys,
                        claim_token,
                        now,
                        reason="candidate_add",
                        last_error=self._error_text(exc),
                        expected_statuses={TaskStatus.ADDING},
                    )
                    self._log_group_failure(adding_keys, "candidate_add", exc)
                    continue

                added_keys = tuple(
                    task.key for task in adding if task.episode in added_episodes
                )
                waiting_keys = tuple(
                    task.key for task in adding if task.episode not in added_episodes
                )
                self._finalize_outcome(
                    added_keys,
                    claim_token,
                    TaskStatus.ADDED,
                    now,
                    "download_added",
                )
                self._finalize_outcome(
                    waiting_keys,
                    claim_token,
                    TaskStatus.WAITING,
                    now,
                    "download_no_progress",
                )

            return True

    def _metadata_plan(self, usable, claimed, claimed_episodes):
        first_candidate = {}
        for candidate, coverage in usable:
            for episode in coverage & claimed_episodes:
                first_candidate.setdefault(episode, candidate)

        keys_by_candidate = {}
        for task in claimed:
            candidate = first_candidate.get(task.episode)
            if candidate is not None:
                keys_by_candidate.setdefault(id(candidate), []).append(task.key)

        plan = []
        for candidate, _coverage in usable:
            keys = keys_by_candidate.pop(id(candidate), None)
            if not keys:
                continue
            context = candidate.context
            fingerprint, candidate_title = self._candidate_metadata(context)
            plan.append(
                (
                    tuple(keys),
                    fingerprint,
                    candidate_title,
                    candidate.classification,
                )
            )
        return plan

    def _record_waiting_error(self, task_keys, now, exc, reason):
        keys = tuple(task_keys)
        self._transition(
            keys,
            TaskStatus.WAITING,
            now,
            reason=reason,
            last_error=self._error_text(exc),
            claim_token=None,
            expected_statuses={TaskStatus.WAITING},
            expected_claim_token=None,
        )
        self._log_group_failure(keys, reason, exc)

    def _recover_before_add_failure(self, task_keys, claim_token, now, reason):
        try:
            self._recover_owned(
                task_keys,
                claim_token,
                now,
                reason=reason,
                last_error=None,
                expected_statuses=_IN_FLIGHT_STATUSES,
            )
        except Exception:
            pass

    def _recover_owned(
        self,
        task_keys,
        claim_token,
        now,
        *,
        reason,
        last_error,
        expected_statuses,
    ):
        keys = tuple(dict.fromkeys(task_keys))
        try:
            changed = self._transition(
                keys,
                TaskStatus.WAITING,
                now,
                reason=reason,
                last_error=last_error,
                claim_token=None,
                expected_statuses=expected_statuses,
                expected_claim_token=claim_token,
            )
        except Exception as exc:
            for key in keys:
                self._pending_recoveries[key] = (
                    claim_token,
                    last_error,
                    reason,
                )
            self._log_persistence_failure(keys, TaskStatus.WAITING, exc)
            raise

        for key in keys:
            pending = self._pending_recoveries.get(key)
            if pending and pending[0] == claim_token:
                self._pending_recoveries.pop(key, None)
        return changed

    def _finalize_outcome(self, task_keys, claim_token, status, now, reason):
        keys = tuple(dict.fromkeys(task_keys))
        if not keys:
            return ()
        kwargs = {
            "claim_token": None,
            "expected_statuses": {TaskStatus.ADDING},
            "expected_claim_token": claim_token,
        }
        if status is TaskStatus.WAITING:
            kwargs["last_error"] = None

        for _attempt in range(2):
            try:
                changed = self._transition(
                    keys,
                    status,
                    now,
                    reason=reason,
                    **kwargs,
                )
            except Exception as exc:
                self._log_persistence_failure(keys, status, exc)
                continue
            for key in keys:
                pending = self._pending_outcomes.get(key)
                if pending and pending[0] == claim_token:
                    self._pending_outcomes.pop(key, None)
            return changed

        for key in keys:
            self._pending_outcomes[key] = (claim_token, status, reason)
        return ()

    def _flush_pending(self, now):
        for key, (claim_token, status, reason) in tuple(self._pending_outcomes.items()):
            kwargs = {
                "claim_token": None,
                "expected_statuses": {TaskStatus.ADDING},
                "expected_claim_token": claim_token,
            }
            if status is TaskStatus.WAITING:
                kwargs["last_error"] = None
            try:
                self._transition(
                    (key,),
                    status,
                    now,
                    reason=f"pending_{reason}",
                    **kwargs,
                )
            except Exception as exc:
                self._log_persistence_failure((key,), status, exc)
                raise
            self._pending_outcomes.pop(key, None)

        for key, (claim_token, last_error, reason) in tuple(
            self._pending_recoveries.items()
        ):
            try:
                self._transition(
                    (key,),
                    TaskStatus.WAITING,
                    now,
                    reason=f"pending_{reason}",
                    last_error=last_error,
                    claim_token=None,
                    expected_statuses=_IN_FLIGHT_STATUSES,
                    expected_claim_token=claim_token,
                )
            except Exception as exc:
                self._log_persistence_failure((key,), TaskStatus.WAITING, exc)
                raise
            self._pending_recoveries.pop(key, None)

    def _transition(self, task_keys, status, now, *, reason, **kwargs):
        keys = tuple(dict.fromkeys(task_keys))
        old_statuses = {
            key: task.status
            for key in keys
            if (task := self._store.get(key)) is not None
        }
        changed = self._store.transition(keys, status, now, **kwargs)
        for task in changed:
            old_status = old_statuses.get(task.key)
            logger.debug(
                "SubscribeMultiVersion task transition: task_key=%s old_status=%s "
                "new_status=%s reason=%s",
                task.key,
                old_status.value if old_status is not None else "unknown",
                task.status.value,
                reason,
            )
        return changed

    def _candidate_metadata(self, context):
        fingerprint = self._gateway.fingerprint(context)
        title = getattr(getattr(context, "torrent_info", None), "title", None)
        return fingerprint, title

    @staticmethod
    def _contexts_for_episodes(usable, episodes):
        selected = []
        remaining = set(episodes)
        for candidate, original_coverage in usable:
            coverage = original_coverage & remaining
            if not coverage:
                continue
            context = candidate.context
            context.allowed_episodes = coverage
            selected.append(context)
            remaining.difference_update(coverage)
            if not remaining:
                break
        return selected

    @staticmethod
    def _validated_added_episodes(result, claimed_episodes):
        added = set(result)
        if any(type(episode) is not int for episode in added):
            raise ValueError("Download result contains invalid episode values")
        if not added.issubset(claimed_episodes):
            raise ValueError(
                "Download result contains episodes outside claimed episodes"
            )
        return added

    @staticmethod
    def _error_text(exc):
        return exception_diagnostic(exc, _MAX_ERROR_LENGTH)

    @staticmethod
    def _log_group_failure(task_keys, reason, exc):
        for key in dict.fromkeys(task_keys):
            logger.warning(
                "SubscribeMultiVersion group failed: task_key=%s reason=%s error_type=%s",
                key,
                reason,
                type(exc).__name__,
            )

    @staticmethod
    def _log_persistence_failure(task_keys, status, exc):
        for key in dict.fromkeys(task_keys):
            logger.warning(
                "SubscribeMultiVersion persistence deferred: task_key=%s status=%s "
                "error_type=%s",
                key,
                status.value,
                type(exc).__name__,
            )
