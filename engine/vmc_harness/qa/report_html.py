"""QA HTML 보고서 — results.json에서 만들기만 한다(보기 전용, 상태를 바꾸는 입력 없음 — 이중화 방지)."""

import html

from vmc_harness.core import paths
from vmc_harness.core.clock import now
from vmc_harness.qa import results, scenarios

_COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e", "HOLD": "#9a6700", "PENDING": "#57606a"}


def _e(v):
    return html.escape(str(v if v is not None else ""))


def build(root):
    data = results.load(root)["scenarios"]
    loaded = scenarios.load_all(root)
    ids = [sc.get("id") for sc, _, _ in loaded]
    for sid in data:
        if sid not in ids:
            ids.append(sid)
    titles = {sc.get("id"): sc.get("title") for sc, _, _ in loaded}
    s = results.summary(root, ids)
    human = results.human_list(root)
    reg = results.regressions(root)
    rows = []
    for sid in ids:
        r = data.get(sid) or {}
        st = r.get("status") or "PENDING"
        ev = "".join('<div><a href="%s">%s</a></div>' % (_e(_href(root, p)), _e(p)) for p in r.get("evidence") or [])
        flags = []
        if r.get("needs_human"):
            flags.append("사람")
        if r.get("needs_db_change"):
            flags.append("DB 변경")
        if (r.get("budget") or {}).get("exceeded"):
            flags.append("예산 초과")
        if sid in reg:
            flags.append("회귀")
        rows.append("<tr><td>%s</td><td>%s</td><td style='color:%s;font-weight:600'>%s</td><td>%s</td><td>%s</td>"
                    "<td>%d</td><td>%s</td></tr>" % (
                        _e(sid), _e(titles.get(sid)), _COLORS.get(st, "#000"), _e(st), _e(r.get("cause")),
                        _e(" · ".join(flags)), len(r.get("attempts") or []), ev))
    human_html = "".join("<li><b>%s</b> (%s) — %s</li>" % (_e(h["id"]), _e(", ".join(h["reasons"])), _e(h["cause"]))
                         for h in human) or "<li>없음</li>"
    body = """<!doctype html><html lang="ko"><meta charset="utf-8"><title>QA 결과</title>
<style>body{font-family:-apple-system,'Malgun Gothic',sans-serif;margin:24px;color:#1f2328}
table{border-collapse:collapse;width:100%%}td,th{border:1px solid #d0d7de;padding:6px 8px;vertical-align:top;font-size:14px}
th{background:#f6f8fa;text-align:left}.note{color:#57606a;font-size:13px}</style>
<h1>QA 결과</h1>
<p class="note">%s 생성 · 원본 .harness/qa/results.json · 이 화면은 보기 전용(상태는 <code>vmc-harness qa status set</code>로만 바뀐다)</p>
<p>전체 %d · PASS %d · FAIL %d · HOLD %d · 미실행 %d</p>
<h2>사람이 볼 것</h2><ul>%s</ul>
<h2>시나리오</h2>
<table><tr><th>id</th><th>제목</th><th>상태</th><th>원인</th><th>표시</th><th>시도</th><th>증거</th></tr>%s</table>
</html>""" % (_e(now().strftime("%Y-%m-%d %H:%M")), s["total"], s["PASS"], s["FAIL"], s["HOLD"], s["PENDING"],
              human_html, "".join(rows))
    out = paths.cache_dir(root) / "reports" / ("qa-%s.html" % now().strftime("%Y%m%d-%H%M"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return out


def _href(root, rel):
    from pathlib import Path
    p = Path(rel)
    if not p.is_absolute():
        p = Path(root) / rel
    return p.resolve().as_uri()
