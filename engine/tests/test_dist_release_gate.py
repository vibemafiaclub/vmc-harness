"""출고 기준(scripts/release-gate.json) — spec 11 §출고 기준."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_dist_shell import load_release
from vmc_harness.core import release_info


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.rel = load_release()
        self.tmp = Path(tempfile.mkdtemp())

    def gate(self, tests, items):
        p = self.tmp / "gate.json"
        p.write_text(json.dumps({"scenario_tests": tests, "unverified": items}), encoding="utf-8")
        return p

    def test_missing_scenario_test_blocks(self):
        g = self.gate(["engine/tests/없는_시나리오.py"], [])
        with self.assertRaises(SystemExit) as c:
            self.rel.check_gate("", g)
        self.assertIn("시나리오 테스트 없음", str(c.exception))

    def test_open_item_must_be_accepted_by_name(self):
        g = self.gate([], [{"id": "win-week", "desc": "윈도 일주일", "status": "open"},
                           {"id": "done", "desc": "확인됨", "status": "verified"}])
        with self.assertRaises(SystemExit) as c:
            self.rel.check_gate("", g)
        self.assertIn("--accept-open win-week", str(c.exception))
        with self.assertRaises(SystemExit):
            self.rel.check_gate("win-week,오타", g)
        self.assertEqual(self.rel.check_gate("win-week", g), [{"id": "win-week", "desc": "윈도 일주일"}])

    def test_real_gate_file_is_valid(self):
        gate = json.loads(self.rel.GATE_FILE.read_text(encoding="utf-8"))
        ids = [i["id"] for i in gate["unverified"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.get("desc") and i.get("status") in ("open", "verified") for i in gate["unverified"]))

    def test_build_ships_unverified_to_customer(self):
        b = self.rel.build(self.tmp / "out", eval_passed=True, unverified=[{"id": "x", "desc": "시험 중인 기능"}])
        with zipfile.ZipFile(str(b["path"])) as zf:
            man = json.loads(zf.read("manifest.json"))
            info = zf.read("engine/vmc_harness/release_info.json")
        self.assertEqual(man["unverified"], [{"id": "x", "desc": "시험 중인 기능"}])
        p = self.tmp / "release_info.json"
        p.write_bytes(info)
        self.assertEqual(release_info.notice_lines(p)[1], "- 시험 중인 기능")
        self.assertEqual(release_info.notice_lines(self.tmp / "없음.json"), [])


if __name__ == "__main__":
    unittest.main()
