"""작업 상태 읽기 비용(작업 수·작업 트리 변경 수에 비례해 늘지 않게) — 착수 스냅샷 사이드 파일·캐시·서명 대조."""

import os
import time

from tests.util import HarnessTestCase

from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.task import scope, state as st


class StateSpeedTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/a.py", "a = 1\n")
        self.commit()
        self.write("src/a.py", "a = 2\n")          # 착수 전부터 바뀌어 있던 파일
        self.write("big.bin", "x" * 10)

    def _task(self, tid="T-1", **kw):
        t = {"task_id": tid, "started_at": "2026-01-01T00:00:00+09:00", "base": scope.take_base(self.root)}
        t.update(kw)
        return st.save(self.root, t)

    def test_dirty_goes_to_sidecar(self):
        self._task()
        main = read_json(st.task_path(self.root, "T-1"))
        self.assertNotIn("dirty", main["base"])
        self.assertEqual(main["base"]["sidecar"], "T-1")
        side = read_json(st.task_path(self.root, "T-1", ".base"))
        self.assertIn("src/a.py", side)
        # 읽을 때 붙는다
        t = st.by_id(self.root, "T-1")
        self.assertIn("src/a.py", scope.base_dirty(self.root, t["base"]))

    def test_sidecar_not_rewritten_without_change(self):
        t = self._task()
        p = st.task_path(self.root, "T-1", ".base")
        m0 = p.stat().st_mtime_ns
        time.sleep(0.01)
        t = st.by_id(self.root, "T-1")
        scope.base_dirty(self.root, t["base"])
        t["edits"] = ["src/a.py"]
        st.save(self.root, t)
        self.assertEqual(p.stat().st_mtime_ns, m0)

    def test_absorb_rewrites_sidecar(self):
        self._task()
        self.write("src/new.py", "n = 1\n")
        t = st.by_id(self.root, "T-1")
        scope.absorb_outside_changes(self.root, t)
        st.save(self.root, t)
        self.assertIn("src/new.py", read_json(st.task_path(self.root, "T-1", ".base")))

    def test_old_format_migrates_on_read(self):
        dirty = {"src/a.py": st.file_hash(self.root / "src/a.py")}
        write_json(st.task_path(self.root, "T-old"),
                   {"task_id": "T-old", "started_at": "x", "base": {"head": None, "dirty": dirty}})
        tasks = st.all_tasks(self.root)
        self.assertEqual([t["task_id"] for t in tasks], ["T-old"])
        self.assertNotIn("dirty", read_json(st.task_path(self.root, "T-old"))["base"])
        self.assertEqual(read_json(st.task_path(self.root, "T-old", ".base")), dirty)
        # 옛 형식 서명(sha1 문자열)도 대조된다
        self.assertTrue(scope.unchanged_since_base(self.root, "src/a.py", dirty["src/a.py"]))

    def test_cache_returns_copies_and_sees_other_writers(self):
        self._task()
        a = st.by_id(self.root, "T-1")
        a["edits"] = ["zzz"]
        self.assertNotIn("edits", st.by_id(self.root, "T-1"))
        # 다른 프로세스가 쓴 것처럼 파일을 직접 바꾸면 다시 읽힌다
        rec = read_json(st.task_path(self.root, "T-1"))
        rec["note"] = "other-process"
        write_json(st.task_path(self.root, "T-1"), rec)
        self.assertEqual(st.by_id(self.root, "T-1").get("note"), "other-process")

    def test_signature_mtime_only_change_is_unchanged(self):
        sig = scope._sig(self.root, "src/a.py")
        self.assertIn("sha1", sig)
        p = self.root / "src/a.py"
        os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
        self.assertTrue(scope.unchanged_since_base(self.root, "src/a.py", sig))
        # 크기가 다른 내용으로 — 리눅스 파일 시각 단위가 굵어 같은 크기·같은 시각이 우연히 나올 수 있다(크기·시각이 같으면 그대로로 보는 것은 설계상 한계, spec 09)
        self.write("src/a.py", "a = 300\n")
        self.assertFalse(scope.unchanged_since_base(self.root, "src/a.py", sig))
        self.assertTrue(scope.unchanged_since_base(self.root, "gone.py", None))

    def test_touched_matches_changes_filtered_by_edits(self):
        t = self._task()
        self.write("src/b.py", "b = 1\n")       # 이 작업 편집
        self.write("src/c.py", "c = 1\n")       # 다른 사람
        (self.root / "src/a.py").unlink()      # 착수 전 변경 파일을 이 작업이 지움
        t["edits"] = ["src/b.py", "src/a.py", "src/never.py"]
        st.save(self.root, t)
        t = st.by_id(self.root, "T-1")
        old = [f for f in scope.changes(self.root, t["base"], edits=t["edits"]) if f in set(t["edits"])]
        self.assertEqual(scope.touched(self.root, t), old)
        self.assertEqual(scope.touched(self.root, t), ["src/a.py", "src/b.py"])


class ArchiveTest(HarnessTestCase):
    def test_archive_old_tasks(self):
        from vmc_harness.core import clock
        self.addCleanup(clock.set_fake_now, None)
        st.save(self.root, {"task_id": "T-old", "started_at": "2026-01-01T00:00:00+09:00",
                            "ended_at": "2026-01-01T01:00:00+09:00", "base": {"head": None, "dirty": {"x": None}}})
        st.save(self.root, {"task_id": "T-pend", "started_at": "2026-01-01T00:00:00+09:00",
                            "ended_at": "2026-01-01T01:00:00+09:00", "measure_pending": True})
        st.save(self.root, {"task_id": "T-new", "started_at": "2026-01-20T00:00:00+09:00",
                            "ended_at": "2026-01-20T01:00:00+09:00"})
        st.save(self.root, {"task_id": "T-open", "started_at": "2026-01-01T00:00:00+09:00"})
        clock.set_fake_now(clock.parse_iso("2026-01-21T00:00:00+09:00"))
        self.assertEqual(st.archive_old(self.root, days=7), ["T-old"])
        self.assertEqual(sorted(t["task_id"] for t in st.all_tasks(self.root)), ["T-new", "T-open", "T-pend"])
        self.assertEqual(len(st.all_tasks(self.root, archived=True)), 4)
        self.assertEqual(st.by_id(self.root, "T-old")["task_id"], "T-old")
        self.assertEqual(st.base_dirty(self.root, "T-old"), {"x": None})
