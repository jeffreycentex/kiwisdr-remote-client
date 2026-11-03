# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, synchronous command submission to a single application owner."""
from concurrent.futures import Future
from functools import wraps
import queue
import threading


class CommandQueue:
    def __init__(self):
        self._queue = queue.Queue(maxsize=64)
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="kiwi-commands", daemon=True)
        self._thread.start()

    def call(self, fn, *args, **kwargs):
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("service has shut down")
            try:
                self._queue.put_nowait((future, fn, args, kwargs))
            except queue.Full:
                raise RuntimeError("control command queue is full")
        return future.result()

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        if threading.current_thread() is not self._thread:
            self._thread.join()


def command(fn):
    @wraps(fn)
    def submit(self, *args, **kwargs):
        return self._commands.call(fn, self, *args, **kwargs)
    return submit
