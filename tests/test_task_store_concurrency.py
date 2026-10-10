from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from mcd_agent.daemon import TaskStore


class _NonReentrantConnection:
    """Expose the failure mode of overlapping operations on one connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._active = threading.Lock()
        self.first_execute_entered = threading.Event()
        self.release_first_execute = threading.Event()

    def execute(self, *args: object, **kwargs: object):
        if not self._active.acquire(blocking=False):
            raise sqlite3.InterfaceError("bad parameter or other API misuse")
        try:
            if not self.first_execute_entered.is_set():
                self.first_execute_entered.set()
                if not self.release_first_execute.wait(timeout=2.0):
                    raise AssertionError("test connection release timed out")
            return self._connection.execute(*args, **kwargs)
        finally:
            self._active.release()

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class TaskStoreConcurrencyTests(unittest.TestCase):
    def test_runtime_sync_serializes_state_push_and_terminal_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TaskStore(str(Path(temp_dir) / "state.db"))
            store.put_runtime_sync("race", {"value": 0})
            guarded = _NonReentrantConnection(store.conn)
            store.conn = guarded  # type: ignore[assignment]
            errors: list[BaseException] = []
            observed: list[dict[str, object] | None] = []

            def read_state() -> None:
                try:
                    observed.append(store.get_runtime_sync("race"))
                except BaseException as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            def write_state() -> None:
                try:
                    store.put_runtime_sync("race", {"value": 1})
                except BaseException as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            reader = threading.Thread(target=read_state, name="state-push-reader")
            writer = threading.Thread(target=write_state, name="terminal-callback-writer")
            reader.start()
            self.assertTrue(guarded.first_execute_entered.wait(timeout=1.0))
            writer.start()

            # Keep the first sqlite operation in flight while the second
            # thread reaches the store boundary. Without the TaskStore lock,
            # the guarded connection raises the same InterfaceError seen in
            # the live 1.5.65 stack.
            guarded.release_first_execute.set()
            reader.join(timeout=2.0)
            writer.join(timeout=2.0)

            self.assertFalse(reader.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(observed, [{"value": 0}])
            self.assertEqual(store.get_runtime_sync("race"), {"value": 1})
            store.close()


if __name__ == "__main__":
    unittest.main()
