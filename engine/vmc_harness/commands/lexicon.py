"""`vmc-harness lexicon` — 어휘집(spec 08). 독자가 "이게 뭔 말이야" → 어휘집 갱신 후보(영향은 어휘집 안에서만).

  lexicon unclear <단어>   그 말을 이 사람 프로필의 '피하는 말'로, 프로젝트 후보 횟수 +1
  lexicon show             이 PC 독자 프로필 요약 + 프로젝트 어휘집
"""

import json


def register(sub):
    p = sub.add_parser("lexicon", help="어휘집: unclear <단어> | show")
    p.add_argument("action", choices=("unclear", "show"))
    p.add_argument("word", nargs="?")
    p.set_defaults(func=_run)


def _run(a):
    from vmc_harness.core import paths
    from vmc_harness.render import lexicon, profile
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트")
        return 2
    if a.action == "unclear":
        if not a.word:
            print("단어 없음")
            return 2
        print(json.dumps(lexicon.unclear(root, a.word, source="command"), ensure_ascii=False))
        return 0
    prof = profile.load(root) or {}
    lx = lexicon.load(root)
    print(json.dumps({"reader": {"name": prof.get("name"), "computed_at": prof.get("computed_at"),
                                 "preferred": {k: v.get("term") for k, v in (prof.get("preferred") or {}).items()},
                                 "objects": (prof.get("objects") or [])[:20], "avoid": prof.get("avoid") or []},
                      "project": {"terms": len(lx.get("terms") or {}), "groups": lx.get("groups") or [],
                                  "avoid": lx.get("avoid") or [], "ambiguous": lx.get("ambiguous") or {}}},
                     ensure_ascii=False, indent=1))
    return 0
