"""Concurrent access to the workspace database.

The UI runs a pipeline on a worker thread while the browser polls from request
threads, so several connections write and read the same file at once. SQLite
forbids sharing one connection across threads and serialises writers, so this is
where a wrong assumption shows up as an intermittent failure rather than a
clean error.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from aicut.db.store import Store
from aicut.models import Episode, Event, EventMention, Project


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "aicut.db"
        self.store = Store(self.path)
        self.project = self.store.create_project(Project(file_path="/f.mkv", duration_sec=100))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_the_database_runs_in_wal_so_a_reader_does_not_block_the_writer(self):
        mode = self.store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_an_in_memory_store_is_not_forced_into_wal(self):
        memory = Store()
        try:
            self.assertEqual(memory.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "memory")
        finally:
            memory.close()

    def test_writers_on_separate_threads_do_not_lose_rows(self):
        """Each thread opens its own connection, as the UI does."""
        errors: list[Exception] = []
        episodes_per_thread = 12

        def worker(tag: int) -> None:
            store = Store(self.path)
            try:
                for i in range(episodes_per_thread):
                    store.save_episode(Episode(
                        project_id=self.project.project_id,
                        target_type=f"t{tag}",
                        notes=f"worker {tag} episode {i}",
                    ))
            except Exception as exc:                     # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [], f"concurrent writes failed: {errors[:2]}")
        self.assertEqual(len(self.store.episodes(self.project.project_id)), 4 * episodes_per_thread)

    def test_a_reader_sees_committed_work_while_a_writer_keeps_going(self):
        started = threading.Event()
        stop = threading.Event()
        seen: list[int] = []

        def writer() -> None:
            store = Store(self.path)
            try:
                for i in range(40):
                    event = Event(project_id=self.project.project_id, summary=f"event {i}")
                    event.mentions = [EventMention(event.event_id, i, i + 1, "result", "")]
                    store.conn.execute(
                        "INSERT INTO tb_event (event_id, project_id, summary, people, relations)"
                        " VALUES (?,?,?,'[]','[]')",
                        (event.event_id, self.project.project_id, event.summary),
                    )
                    store.conn.commit()
                    started.set()
            finally:
                store.close()
                stop.set()

        thread = threading.Thread(target=writer)
        thread.start()
        started.wait(timeout=10)
        reader = Store(self.path)
        try:
            while not stop.is_set() and len(seen) < 3:
                seen.append(len(reader.events(self.project.project_id)))
        finally:
            reader.close()
        thread.join(timeout=30)

        self.assertTrue(seen, "a reader could not read at all while a writer was running")
        self.assertEqual(len(self.store.events(self.project.project_id)), 40)

    def test_the_ui_hands_each_thread_its_own_connection(self):
        from aicut.ui.server import UiServer

        ui = UiServer(Path(self._tmp.name) / "ws")
        try:
            main_store = ui.store
            other: list[Store] = []
            thread = threading.Thread(target=lambda: other.append(ui.store))
            thread.start()
            thread.join(timeout=10)

            self.assertIsNot(main_store, other[0], "two threads shared one SQLite connection")
            self.assertIs(ui.store, main_store, "the same thread must keep its own connection")
        finally:
            ui.close()

    def test_closing_the_ui_closes_every_connection_it_opened(self):
        from aicut.ui.server import UiServer

        ui = UiServer(Path(self._tmp.name) / "ws2")
        ui.store
        thread = threading.Thread(target=lambda: ui.store)
        thread.start()
        thread.join(timeout=10)
        opened = list(ui._stores)
        self.assertEqual(len(opened), 2)

        ui.close()
        for store in opened:
            with self.assertRaises(Exception):
                store.conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
