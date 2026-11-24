"""Bounded cooperative Shader jobs; waiting jobs own no operating-system thread.

Each step runs in a fresh context, just like the former dedicated threads. In
particular a restore's temporary admission/binding must not leak into later saves.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
import heapq
import itertools
import logging
import threading
import time

_local = threading.local()
_guard = threading.Lock()
_scheduler = None


def is_current_task(task):
    return task is threading.current_thread() or getattr(_local, "task", None) is task


class _Scheduler:
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = []
        self.sequence = itertools.count()
        self.pools = {lane: ThreadPoolExecutor(max_workers=count, thread_name_prefix="shader-shared-" + lane)
                      for lane, count in (("watch", 4), ("monitor", 2), ("apply", 1))}
        threading.Thread(target=self.run, name="shader-shared-scheduler", daemon=True).start()

    def enqueue(self, task, delay):
        with self.condition:
            if task._done.is_set():
                return
            task._generation += 1
            heapq.heappush(self.queue, (time.monotonic() + max(0.0, delay), next(self.sequence), task._generation, task))
            self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                while not self.queue:
                    self.condition.wait()
                due, _, generation, task = self.queue[0]
                delay = due - time.monotonic()
                if delay > 0:
                    self.condition.wait(delay)
                    continue
                heapq.heappop(self.queue)
                if generation != task._generation or task._done.is_set():
                    continue
                if task._inflight:
                    task._wake_pending = True
                    continue
                task._inflight = True
            self.pools[task.lane].submit(task._step)


def shared_scheduler():
    global _scheduler
    with _guard:
        if _scheduler is None:
            _scheduler = _Scheduler()
        return _scheduler


class ScheduledTask:
    """Thread-compatible lifetime handle around a yielding, non-overlapping job."""
    def __init__(self, target, *, name, lane):
        self.target, self.name, self.lane = target, name, lane
        self._done = threading.Event()
        self._started = False
        self._generation = 0
        self._inflight = self._wake_pending = False
        self._iterator = None
        self._scheduler = shared_scheduler()

    def start(self):
        if self._started:
            raise RuntimeError("Shader task already started")
        self._started = True
        self._scheduler.enqueue(self, 0)

    def is_alive(self):
        return self._started and not self._done.is_set()

    def wake(self):
        if self._started:
            self._scheduler.enqueue(self, 0)

    def join(self, timeout=None):
        if is_current_task(self):
            return
        self.wake()
        self._done.wait(timeout)

    def _step(self):
        delay, finished = 0.2, False
        def advance():
            _local.task = self
            try:
                if self._iterator is None:
                    self._iterator = iter(self.target())
                return next(self._iterator)
            finally:
                _local.task = None
        try:
            delay = Context().run(advance)
        except StopIteration:
            finished = True
        except BaseException:
            finished = True
            logging.getLogger(__name__).exception("Shader scheduled job failed: %s", self.name)
        with self._scheduler.condition:
            self._inflight = False
            if finished:
                self._done.set()
            else:
                delay = 0 if self._wake_pending else delay
                self._wake_pending = False
                self._scheduler.enqueue(self, float(delay or 0.001))
