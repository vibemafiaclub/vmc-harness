"""회귀 세트(spec 11) — 합성 데이터만. 실제 고객 데이터셋 실행은 `vmc-harness eval --dataset ~/vmc-client-data/...`."""

import json
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.eval import acceptance, classify, diagnosis, runner


class DiagnosisScoreTest(unittest.TestCase):
    ITEMS = [
        {"id": "db", "any": [{"condition_type": "db_direct"}]},
        {"id": "prot", "any": [{"kind": "protected"}, {"all": ["보호\\s*영역"]}]},
        {"id": "lint", "any": [{"all": ["ruff|lint", "변경"]}]},
    ]

    def test_counts_items_in_top_k_only(self):
        props = [
            {"no": 1, "title": "DB 직접 접속 막기", "kind": "gate", "blocks": "-", "scope": "-", "condition": {"type": "db_direct"}},
            {"no": 2, "title": "ruff 도입", "kind": "structure", "blocks": "-", "scope": "전체 파일"},
            {"no": 3, "title": "감사 로그를 보호 영역으로", "kind": "protected", "blocks": "-", "scope": "-"},
        ]
        s = diagnosis.score(props, self.ITEMS, top_k=2)
        self.assertEqual(s["hits"], 1)        # 3번은 상위 2개 밖, 2번은 '변경'이 없어 린터 권고 아님
        self.assertEqual(s["matched"], {"db": [1]})
        self.assertEqual(sorted(s["missed"]), ["lint", "prot"])

    def test_evidence_text_not_matched(self):
        props = [{"no": 1, "title": "x", "kind": "structure", "blocks": "-", "scope": "-",
                  "evidence": "\"보호 영역 지켜\"라고 했다"}]
        self.assertEqual(diagnosis.score(props, self.ITEMS, top_k=5)["hits"], 0)


class DiagnosisRunRestoresTest(HarnessTestCase):
    def test_history_and_diagnosis_restored(self):
        from vmc_harness.bootstrap import diagnose
        from vmc_harness.core.jsonio import read_json, write_json
        write_json(diagnose.history_path(self.root), {"shown": [{"title": "old"}]})
        write_json(diagnose.diagnosis_path(self.root), {"proposals": [{"title": "old"}], "status": "ok"})
        ds = Path(tempfile.mkdtemp(prefix="vmh-ds-"))
        self.addCleanup(shutil.rmtree, str(ds), True)
        (ds / "diagnosis_gold.json").write_text(json.dumps({"project": str(self.root), "items": [
            {"id": "a", "any": [{"all": ["new"]}]}]}), encoding="utf-8")
        seen = {}

        def fake_run(root, analysis_dir=None, progress=None):
            seen["hist"] = read_json(diagnose.history_path(root))
            d = {"status": "ok", "proposals": [{"no": 1, "title": "new gate", "kind": "gate", "blocks": "", "scope": ""}]}
            write_json(diagnose.diagnosis_path(root), d)
            return d
        with mock.patch("vmc_harness.bootstrap.diagnose.run", side_effect=fake_run):
            r = diagnosis.run(ds, runs=1, use_existing=True)
        self.assertEqual(seen["hist"]["shown"], [])                    # 지난 제안 기록을 비운 채 돌렸다
        self.assertEqual(read_json(diagnose.history_path(self.root))["shown"], [{"title": "old"}])  # 원래대로
        self.assertEqual(read_json(diagnose.diagnosis_path(self.root))["proposals"][0]["title"], "old")
        self.assertEqual([x["hits"] for x in r["runs"]], [0, 1])       # existing(old) 0, 새 실행 1
        self.assertEqual(r["metrics"]["hits_mean"], 0.5)


class ClassifyTest(unittest.TestCase):
    TAG = ("| session | agent | 날짜 | H | A | B | C | D | 한줄 |\n|---|---|---|---|---|---|---|---|---|\n"
           "| aaaaaaaa | CODEX(gpt-x) | 07-01 | 2 | 0 | 0 | 1 | 1 | x |\n"
           "| bbbbbbbb | CURSOR | 07-01 | 1 | 0 | 0 | 1 | 0 | y |\n"
           "| cccccccc | CODEX | 07-01 | 5 | 0 | 0 | 5 | 0 | 발화 수 불일치 |\n"
           "| **합계** | | | **8** | | | | | |\n"
           "| aaaaaaaa H2 (애매) | D | A | 이유 |\n")

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="vmh-cls-"))
        self.addCleanup(shutil.rmtree, str(self.d), True)
        (self.d / "tag_1.md").write_text(self.TAG, encoding="utf-8")
        rows = [{"kind": "main", "sid": "aaaaaaaa-1", "i": 1, "text": "안되는데?"},
                {"kind": "main", "sid": "aaaaaaaa-1", "i": 0, "text": "기능 만들어줘"},
                {"kind": "spawn", "sid": "aaaaaaaa-1", "i": 2, "text": "서브에이전트"},
                {"kind": "main", "sid": "bbbbbbbb-2", "i": 0, "text": "커밋해"},
                {"kind": "main", "sid": "cccccccc-3", "i": 0, "text": "x"}]
        (self.d / "prompts.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    def test_parse_join(self):
        tags = classify.parse_tags([self.d / "tag_1.md"])
        self.assertEqual(set(tags), {"aaaaaaaa", "bbbbbbbb", "cccccccc"})
        self.assertEqual(tags["aaaaaaaa"]["D"], 1)
        sessions, dropped = classify.join(tags, classify.load_prompts(self.d / "prompts.jsonl"))
        self.assertEqual(dropped, 1)
        a = [s for s in sessions if s[0].startswith("aaaa")][0]
        self.assertEqual(a[2], ["기능 만들어줘", "안되는데?"])   # i 순서, spawn 제외

    def test_sample_alternates_and_caps(self):
        ss = [("p%d" % i, 1, ["x"] * 3) for i in range(5)] + [("n%d" % i, 0, ["x"] * 3) for i in range(5)]
        pk = classify.sample(ss, 7, 30)
        self.assertEqual(len(pk), 3)
        self.assertEqual([s[1] > 0 for s in pk][:2], [True, False])
        self.assertEqual(classify.sample([("big", 1, ["x"] * 40)], 100, 30), [])

    def test_metrics_count_matching(self):
        m = classify.metrics([{"pred": 2, "gold": 1, "prompts": 3}, {"pred": 0, "gold": 2, "prompts": 4},
                              {"pred": 1, "gold": 0, "prompts": 2}])
        self.assertEqual((m["tp"], m["pred"], m["gold"]), (1, 3, 3))
        self.assertEqual(m["precision"], 0.333)
        self.assertEqual(m["neg_sessions_flagged"], 1)

    def test_classify_session_uses_selfcheck(self):
        out = {"corrections": 1, "learnings": [
            {"gist": "g", "judgment": "j", "title": "t", "prompt_index": 1, "quote": "안되는데"},
            {"gist": "g", "judgment": "j", "title": "t", "prompt_index": 0, "quote": "지어낸 인용"}]}  # 기계 검사로 빠짐
        with mock.patch("vmc_harness.ledger.selfcheck.extract", return_value=(out, {"tokens": 50})):
            r = classify.classify_session(str(self.d), ["기능 만들어줘", "안되는데?"])
        self.assertEqual((r["pred"], r["indices"], r["tokens"]), (1, [1], 50))


class CompareTest(unittest.TestCase):
    def _sets(self, **m):
        return {"render": {"status": "ok", "metrics": {"facts_ok_rate": m.get("f", 1.0), "unknown_total": m.get("u", 0)}},
                "overhead": {"status": "ok", "metrics": {"small_sec": m.get("s", 1.0), "mid_sec": 2.0,
                                                         "small_tokens": 100, "mid_tokens": 200}},
                "acceptance": {"status": "no_data"}}

    def test_same_or_better_passes(self):
        rows, ok = runner.compare(self._sets(u=0, s=1.1), {"sets": self._sets(u=1, s=1.0)})
        self.assertTrue(ok)   # 시간은 잡음 허용(0.2) 안
        self.assertIn("건너뜀", [r for r in rows if r["set"] == "acceptance"][0]["note"])

    def test_worse_fails(self):
        _, ok = runner.compare(self._sets(u=2), {"sets": self._sets(u=1)})
        self.assertFalse(ok)
        _, ok = runner.compare(self._sets(s=1.5), {"sets": self._sets(s=1.0)})
        self.assertFalse(ok)

    def test_summary_md_renders(self):
        res = {"at": "t", "engine_version": "0", "git_commit": "x", "git_dirty": False, "eval_model": None,
               "sets": self._sets()}
        rows, ok = runner.compare(res["sets"], None)
        res["compare"] = {"rows": rows, "passed": None, "baseline_at": None}
        self.assertIn("기준선 없음", runner.summary_md(res))


class AcceptanceTest(HarnessTestCase):
    def test_no_data_without_choices(self):
        from vmc_harness.bootstrap import diagnose
        from vmc_harness.core.jsonio import write_json
        write_json(diagnose.history_path(self.root), {"shown": [{"title": "a"}, {"title": "b"}], "accepted": []})
        ds = Path(tempfile.mkdtemp(prefix="vmh-acc-"))
        self.addCleanup(shutil.rmtree, str(ds), True)
        (ds / "acceptance.json").write_text(json.dumps({"projects": [{"project": str(self.root)}]}), encoding="utf-8")
        self.assertEqual(acceptance.run(ds)["status"], "no_data")
        from vmc_harness.core import ledger
        ledger.create_action(self.root, {"type": "explicit_request", "quote": "a", "ref": "diagnose"}, "structure",
                             "h", human_requested=True, status="prepared", target={"structure": "x"})
        r = acceptance.run(ds)
        self.assertEqual(r["metrics"], {"rate": 0.5, "accepted": 1, "shown": 2})


if __name__ == "__main__":
    unittest.main()
