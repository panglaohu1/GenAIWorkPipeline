"""Single-host compute admission for managed inference and engine requests.

The console and runner use different threads/event loops. Keep queue state under
a threading lock; never share an asyncio.Lock between those loops. This is a
process-local scheduler, not a lock for unrelated programs on the same Mac.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, TypeVar

from . import config

T = TypeVar("T")
_context: ContextVar[dict | None] = ContextVar("compute_context", default=None)
log = logging.getLogger(__name__)


class ComputeQueueTimeout(RuntimeError):
    pass


class LocalCompute:
    """FIFO single compute slot; resident models may coexist within the budget.

    Cancellation while queued removes the request. Cancellation during execution
    waits for the managed call to settle before releasing the slot: closing an
    HTTP connection is not proof that remote inference has stopped.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._queue: deque[dict] = deque()
        self._active: dict | None = None
        self._recent: deque[dict] = deque(maxlen=50)

    @contextmanager
    def context(self, run_id: str, stage: str, emit: Callable):
        token = _context.set({"run_id": run_id, "stage": stage, "emit": emit})
        try:
            yield
        finally:
            _context.reset(token)

    def _emit(self, ctx: dict, job: dict, state: str) -> None:
        if not ctx.get("emit"):
            return
        try:
            ctx["emit"]("compute", f"本地算力 {state}：{job['operation']}",
                        {**job, "state": state})
        except Exception:
            log.exception("Could not persist compute event")

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            active = dict(self._active) if self._active else None
            if active:
                active["elapsed_seconds"] = round(now - active["started_monotonic"], 3)
            queue = [{**j, "wait_seconds": round(now - j["queued_monotonic"], 3)} for j in self._queue]
            return {"policy": "serial", "active": active, "queued": queue, "recent": list(self._recent)}

    async def run(self, operation: str, call: Callable[[], Awaitable[T]], *,
                  queue_timeout: float | None = None) -> T:
        ctx = _context.get() or {}
        job = {"id": uuid.uuid4().hex[:12], "operation": operation,
               "run_id": ctx.get("run_id"), "stage": ctx.get("stage"),
               "queued_at": time.time(), "queued_monotonic": time.monotonic()}
        timeout = queue_timeout if queue_timeout is not None else float(config.get("compute.queue_timeout_s", 1800))
        with self._lock:
            self._queue.append(job)
        self._emit(ctx, job, "queued")
        acquired = False
        state = "failed"
        try:
            while not acquired:
                with self._lock:
                    if self._active is None and self._queue[0] is job:
                        self._queue.popleft()
                        job["started_monotonic"] = time.monotonic()
                        job["wait_seconds"] = round(job["started_monotonic"] - job["queued_monotonic"], 3)
                        self._active = job
                        acquired = True
                if not acquired:
                    if time.monotonic() - job["queued_monotonic"] >= timeout:
                        raise ComputeQueueTimeout(f"等待本地算力超时（{timeout:g}s）：{operation}")
                    await asyncio.sleep(0.05)
            self._emit(ctx, job, "running")
            task = asyncio.create_task(call())
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if task.done():
                        # Retrieve exceptions, including cancellation of the call itself.
                        result = task.result()
                        break
                    self._emit(ctx, job, "draining")
            if cancelled:
                raise asyncio.CancelledError
            state = "done"
            return result
        except asyncio.CancelledError:
            state = "cancelled"
            raise
        except BaseException as exc:
            job["error"] = str(exc)[:500]
            raise
        finally:
            now = time.monotonic()
            with self._lock:
                if acquired:
                    self._active = None
                    job["execution_seconds"] = round(now - job["started_monotonic"], 3)
                else:
                    self._queue.remove(job)
                    job["wait_seconds"] = round(now - job["queued_monotonic"], 3)
                    job["execution_seconds"] = 0.0
                job["state"] = state
                job["finished_at"] = time.time()
                self._recent.append(dict(job))
            self._emit(ctx, job, state)


compute = LocalCompute()
