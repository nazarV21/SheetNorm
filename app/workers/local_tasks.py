from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from flask import Flask


class LocalTaskManager:
    """In-process background executor for local/pilot installations.

    The executor keeps work independent from the browser request, so navigating
    away from the page does not interrupt the conversion. Production deployments
    should prefer Redis/RQ because in-process jobs do not survive an application
    restart.
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="sheetnorm-job",
        )
        self._lock = threading.RLock()
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._max_workers = max(1, int(max_workers))

    def submit(
        self,
        app: Flask,
        job_id: str,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        with self._lock:
            existing = self._futures.get(job_id)
            if existing and not existing.done():
                return existing

            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event

            def execute() -> Any:
                with app.app_context():
                    return function(*args, cancel_event=cancel_event, **kwargs)

            future = self._executor.submit(execute)
            self._futures[job_id] = future

            def cleanup(completed: Future[Any]) -> None:
                with self._lock:
                    if self._futures.get(job_id) is completed:
                        self._futures.pop(job_id, None)
                        self._cancel_events.pop(job_id, None)

            future.add_done_callback(cleanup)
            return future

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            cancel_event = self._cancel_events.get(job_id)
            future = self._futures.get(job_id)
            if cancel_event is not None:
                cancel_event.set()
            cancelled_before_start = bool(future and future.cancel())
            return cancel_event is not None or cancelled_before_start

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            future = self._futures.get(job_id)
            return bool(future and not future.done())

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = [job_id for job_id, future in self._futures.items() if not future.done()]
            return {
                "mode": "thread",
                "process_id": os.getpid(),
                "max_workers": self._max_workers,
                "active_jobs": active,
                "active_count": len(active),
            }


def get_local_task_manager(app: Flask, max_workers: int = 2) -> LocalTaskManager:
    manager = app.extensions.get("sheetnorm_local_tasks")
    if manager is None:
        manager = LocalTaskManager(max_workers=max_workers)
        app.extensions["sheetnorm_local_tasks"] = manager
    return manager
