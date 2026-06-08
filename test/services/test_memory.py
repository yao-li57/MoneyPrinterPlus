"""
M1 单元测试：MemoryStore 基础设施。
所有用例使用临时 db 文件，互不污染，也不接触真实 ./storage/memory/。
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services.memory import MemoryStore


class _MemoryStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "memory.db")
        self.store = MemoryStore(db_path=self.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class TestInitAndSchema(_MemoryStoreTestCase):
    def test_db_file_created_on_first_use(self):
        self.assertFalse(os.path.exists(self.db_path), "DB should be lazy-created, not at construction")
        self.store.stats()
        self.assertTrue(os.path.exists(self.db_path))

    def test_schema_version_recorded(self):
        self.store.stats()
        # peek directly via the underlying connection
        row = self.store._conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(row[0], "1")

    def test_stats_starts_at_zero(self):
        self.assertEqual(
            self.store.stats(), {"materials": 0, "scripts": 0, "failures": 0}
        )


class TestMaterialMemory(_MemoryStoreTestCase):
    def test_record_then_get(self):
        self.store.record_material_use(
            url="https://x/v1.mp4",
            subject="cats",
            search_terms=["kitten", "猫"],
            ranker_score=0.82,
            task_id="t1",
            in_final=True,
        )
        row = self.store.get_material_history("https://x/v1.mp4")
        self.assertIsNotNone(row)
        self.assertEqual(row["subject"], "cats")
        self.assertEqual(row["search_terms"], ["kitten", "猫"])
        self.assertEqual(row["used_count"], 1)
        self.assertEqual(row["in_final_count"], 1)
        self.assertAlmostEqual(row["ranker_score"], 0.82, places=4)
        self.assertEqual(row["task_id"], "t1")

    def test_repeated_record_increments_counts(self):
        url = "https://x/v2.mp4"
        for _ in range(3):
            self.store.record_material_use(url=url, subject="dogs", in_final=False)
        # one more time as in_final
        self.store.record_material_use(url=url, subject="dogs", in_final=True)

        row = self.store.get_material_history(url)
        self.assertEqual(row["used_count"], 4, "every call increments used_count")
        self.assertEqual(row["in_final_count"], 1, "only in_final=True increments in_final_count")

    def test_ranker_score_preserved_when_not_provided(self):
        url = "https://x/v3.mp4"
        self.store.record_material_use(url=url, subject="s", ranker_score=0.9)
        # next call without a score should not wipe the existing one
        self.store.record_material_use(url=url, subject="s", ranker_score=None)
        row = self.store.get_material_history(url)
        self.assertAlmostEqual(row["ranker_score"], 0.9, places=4)

    def test_get_returns_none_for_missing_url(self):
        self.assertIsNone(self.store.get_material_history("https://nope/x.mp4"))

    def test_search_by_subject_orders_by_in_final_then_score(self):
        # All same subject; differ on in_final_count and ranker_score.
        self.store.record_material_use(url="u1", subject="t", ranker_score=0.5, in_final=True)
        self.store.record_material_use(url="u2", subject="t", ranker_score=0.9, in_final=False)
        self.store.record_material_use(url="u3", subject="t", ranker_score=0.7, in_final=True)
        # bump u3 in_final to 2 to outrank u1
        self.store.record_material_use(url="u3", subject="t", in_final=True)

        rows = self.store.search_materials_by_subject("t", limit=10)
        urls = [r["url"] for r in rows]
        self.assertEqual(urls[0], "u3", "u3 has highest in_final_count (2)")
        self.assertEqual(urls[1], "u1", "u1 ties on score-secondary but has in_final=1")
        self.assertEqual(urls[2], "u2", "u2 has in_final=0 → last")

    def test_list_materials_orders_by_recency(self):
        # Fixed timestamps via private write would be cleaner; here we just
        # rely on insertion order being monotonic on a fast machine.
        urls = [f"https://x/r{i}.mp4" for i in range(5)]
        for u in urls:
            self.store.record_material_use(url=u, subject="r")
        listed = [r["url"] for r in self.store.list_materials(limit=10)]
        # most recent insert first
        self.assertEqual(listed[0], urls[-1])
        self.assertEqual(listed[-1], urls[0])

    def test_delete_material(self):
        self.store.record_material_use(url="https://x/del.mp4", subject="d")
        self.assertTrue(self.store.delete_material("https://x/del.mp4"))
        self.assertIsNone(self.store.get_material_history("https://x/del.mp4"))
        self.assertFalse(self.store.delete_material("https://x/del.mp4"), "second delete returns False")

    def test_legacy_string_terms_round_trip(self):
        # Some callers may pass a comma-joined string; we should not crash and
        # parsing back should still produce a list (single-element fallback).
        self.store.record_material_use(
            url="https://x/legacy.mp4",
            subject="s",
            search_terms="not-a-json-list",  # type: ignore[arg-type]
        )
        row = self.store.get_material_history("https://x/legacy.mp4")
        self.assertIsInstance(row["search_terms"], list)
        self.assertEqual(row["search_terms"], ["not-a-json-list"])


class TestScriptSamples(_MemoryStoreTestCase):
    def test_record_and_list(self):
        sid = self.store.record_script_sample(
            task_id="t1", subject="money", script="Money matters.", status="accepted"
        )
        self.assertIsInstance(sid, int)
        rows = self.store.list_script_samples()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["script"], "Money matters.")
        self.assertEqual(rows[0]["status"], "accepted")

    def test_filter_by_status(self):
        self.store.record_script_sample(None, "x", "a", "accepted")
        self.store.record_script_sample(None, "x", "b", "rejected")
        self.store.record_script_sample(None, "x", "c", "edited", edited_to="C")

        edited = self.store.list_script_samples(status="edited")
        self.assertEqual(len(edited), 1)
        self.assertEqual(edited[0]["edited_to"], "C")

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.store.record_script_sample(None, "x", "s", status="bogus")

    def test_delete_script(self):
        sid = self.store.record_script_sample(None, "x", "s", "accepted")
        self.assertTrue(self.store.delete_script_sample(sid))
        self.assertFalse(self.store.delete_script_sample(sid), "second delete returns False")
        self.assertEqual(self.store.list_script_samples(), [])


class TestFailureRecords(_MemoryStoreTestCase):
    def test_record_and_list(self):
        fid = self.store.record_failure(
            task_id="t1", stage="compose", error="ffmpeg crash", provider=None
        )
        self.assertIsInstance(fid, int)
        rows = self.store.list_failures()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "compose")
        self.assertEqual(rows[0]["error"], "ffmpeg crash")

    def test_filter_by_stage(self):
        self.store.record_failure(task_id="t1", stage="compose", error="e1")
        self.store.record_failure(task_id="t2", stage="script", error="e2")
        self.store.record_failure(task_id="t3", stage="compose", error="e3")

        compose = self.store.list_failures(stage="compose")
        self.assertEqual(len(compose), 2)
        self.assertTrue(all(r["stage"] == "compose" for r in compose))

    def test_delete_failure(self):
        fid = self.store.record_failure(task_id="t1", stage="x", error="e")
        self.assertTrue(self.store.delete_failure(fid))
        self.assertFalse(self.store.delete_failure(fid))


class TestAdmin(_MemoryStoreTestCase):
    def test_clear_all_wipes_everything(self):
        self.store.record_material_use(url="u", subject="s")
        self.store.record_script_sample(None, "s", "script", "accepted")
        self.store.record_failure(task_id="t", stage="compose", error="e")

        self.assertEqual(self.store.stats(), {"materials": 1, "scripts": 1, "failures": 1})
        self.store.clear_all()
        self.assertEqual(self.store.stats(), {"materials": 0, "scripts": 0, "failures": 0})

    def test_meta_preserved_after_clear(self):
        self.store.stats()  # init
        self.store.clear_all()
        row = self.store._conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(row[0], "1", "_meta must survive clear_all")


class TestThreadSafety(_MemoryStoreTestCase):
    def test_concurrent_writes_do_not_corrupt(self):
        """Pipeline uses ThreadPoolExecutor — writes from worker threads must serialize cleanly."""
        N = 50
        errors: list = []

        def writer(i: int):
            try:
                self.store.record_material_use(
                    url=f"https://x/c{i}.mp4", subject="concurrent", task_id=f"t{i}"
                )
                self.store.record_failure(task_id=f"t{i}", stage="x", error=f"e{i}")
            except Exception as e:  # pragma: no cover — test signal only
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        stats = self.store.stats()
        self.assertEqual(stats["materials"], N)
        self.assertEqual(stats["failures"], N)


if __name__ == "__main__":
    unittest.main()
