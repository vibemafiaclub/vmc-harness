"""진단 리포트 HTML + PDF (spec 07 §진단 리포트).

담는 것: 1쪽 경영진 요약(숫자 3개·결론·관찰·권고·제안 목록) → (두 번째 진단부터) 지난 진단 이후(하네스 전 / 지난 진단 / 지금)
→ 제안 상세 카드(근거·막는 것·"이 장치가 있었다면" 대표 장면·[추가]·[제외]) → 이 PC의 사용자 단위 지침·훅(이름·요지·숫자, 제안만)
→ 위험이 큰 곳 → 기준선(2차에 "이후" 칸) → 부록(하네스 구성 그림 1~3, 전체 위험 지도, 기계 사실).
문구: 결함의 주어는 도구·과정, 실명 없음. 위치 `.harness/cache/reports/diagnosis-<date>.html` + PDF(파생물, 커밋 안 함).
PDF: macOS는 설치된 Chrome/Edge headless, Windows는 Edge headless. 없으면 HTML만. 메일·메신저로 보내지 않는다.
"""

import html
import os
import re
import shutil
import sys
import webbrowser
from pathlib import Path, PurePosixPath

from vmc_harness.bootstrap import backtest, util
from vmc_harness.core import config, ledger, paths
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import read_json
from vmc_harness.core.proc import run

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_FAIL = re.compile(r"(Traceback|Exception|Error[:\s]|에러|오류|안\s?돼|안\s?됨|작동\s?안|동작\s?안|깨져|깨짐)", re.I)
TEST_MOD = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)")
CATEGORY_LABEL = {"audit": "감사 기록", "approval": "결재 상태 전이", "auth": "인증·세션", "signature": "전자서명",
                  "clock": "시계", "credential": "자격 증명", "generated": "생성 파일", "generated_source": "생성 원본",
                  "db_write": "운영 DB 쓰기", "seed": "시드 데이터", "migration": "스키마 이력",
                  "prod_config": "운영 접속 설정"}


def e(s):
    return html.escape(str(s if s is not None else ""))


def reports_dir(root):
    return paths.ensure(paths.cache_dir(root) / "reports")


# ---------------------------------------------------------------- ① 위험 지도 데이터

def risk_map(root, map_data, facts, sessions, protected_globs):
    """모듈별 신호: 헤맨 곳(여러 세션이 다시 고침)·결함 났던 곳·안전망 없는 곳·보호 영역."""
    mods = [m for m in map_data.get("modules", [])]
    by_id = {m["id"]: m for m in mods}
    ids = sorted(by_id, key=len, reverse=True)

    def mod_of(p):
        p = p.replace("\\", "/")
        d = PurePosixPath(p).parent.as_posix()
        while True:
            if d in by_id:
                return d
            if d in ("", "."):
                return "." if "." in by_id else None
            up = PurePosixPath(d).parent.as_posix()
            if up == d:  # 절대경로('/'·'C:/')는 루트에서 더 못 올라간다 — 프로젝트 밖 파일
                return None
            d = up

    sig = {m["id"]: {"edit_sessions": set(), "edits": 0, "fail_after": 0, "fix_commits": 0, "baseline_fail_tests": 0}
           for m in mods}
    for s in sessions:
        fail_prompt = any(_FAIL.search(x.get("text") or "") for x in s.get("human_prompts") or [])
        touched = set()
        for t in s.get("tool_calls") or []:
            if not t.get("write"):
                continue
            for p in t.get("paths") or []:
                mid = mod_of(p)
                if mid:
                    sig[mid]["edits"] += 1
                    sig[mid]["edit_sessions"].add(s.get("session_id"))
                    touched.add(mid)
        if fail_prompt:
            for mid in touched:
                sig[mid]["fail_after"] += 1
    # git: 고침·되돌림 커밋
    for c in backtest._commits(root, config.get("backtest.max_commits", root)):
        if backtest.FIX_MSG.search(c["subject"]):
            for _, p in c["files"]:
                mid = mod_of(p)
                if mid:
                    sig[mid]["fix_commits"] += 1
    # 테스트가 가리키는 모듈(테스트 모듈의 의존) / 기준선 실패 테스트가 가리키는 모듈
    tested = set()
    fails = set()
    for r in (facts.get("test_baseline") or {}).get("runs") or []:
        pre = "" if r.get("dir") in (None, ".") else r["dir"] + "/"
        for x in (r.get("failed") or []) + (r.get("errors") or []) + [c["path"] for c in r.get("collection_errors") or []]:
            fails.add(pre + x.split("::")[0])
    fail_mods = {}
    for f in fails:
        tm = mod_of(f)
        if tm:
            fail_mods[tm] = fail_mods.get(tm, 0) + 1
    for m in mods:
        if TEST_MOD.search(m["id"]) or any(PurePosixPath(f).name.startswith("test_") or ".test." in f for f in m.get("file_list", [])):
            for d in m.get("deps", []):
                tested.add(d)
                if m["id"] in fail_mods:
                    sig[d]["baseline_fail_tests"] += fail_mods[m["id"]]
    rows = []
    for m in mods:
        if TEST_MOD.search(m["id"]):
            continue
        s = sig[m["id"]]
        prot = [g for g in protected_globs if util.glob_match(m["id"] + "/x", g) or util.glob_match(m["id"], g.rstrip("/*"))
                or any(util.glob_match(f, g) for f in m.get("file_list", [])[:200])]
        code = any(l in ("python", "ts", "js") for l in m.get("lang", []))
        row = {"id": m["id"], "lines": m.get("lines", 0), "files": m.get("files", 0),
               "responsibility": m.get("responsibility", ""),
               "edit_sessions": len(s["edit_sessions"]), "edits": s["edits"], "fail_after": s["fail_after"],
               "fix_commits": s["fix_commits"], "baseline_fail_tests": s["baseline_fail_tests"],
               "no_tests": code and m["id"] not in tested, "protected": prot,
               "largest_file": m.get("largest_file")}
        row["defects"] = s["fail_after"] + s["fix_commits"] + s["baseline_fail_tests"]
        rows.append(row)
    mx = lambda k: max([r[k] for r in rows] + [1])
    for r in rows:
        r["score"] = round(r["edit_sessions"] / float(mx("edit_sessions")) + r["defects"] / float(mx("defects"))
                           + (0.5 if r["no_tests"] else 0) + (0.5 if r["protected"] else 0)
                           + 0.3 * r["lines"] / float(mx("lines")), 3)
    rows.sort(key=lambda r: -r["score"])
    return rows


# ---------------------------------------------------------------- HTML 조각
# 골격(2026-10-23, 레퍼런스: Trail of Bits 진단 보고서·Cure53 경영진 요약·Lighthouse):
# 1쪽 경영진 요약(숫자 3개 → 결론·관찰·권고 → 제안 목록 표) → 제안 상세 카드(근거·막는 것·대표 장면) →
# 위험이 큰 곳 → 기준선 → 부록(하네스 구성·전체 위험 지도·기계 사실). 세부는 뒤로, 원문은 대표만.

CSS = """
:root{--ink:#16191d;--muted:#6b7280;--line:#e5e7eb;--soft:#f5f6f8;--navy:#1B2A4A;--accent:#b4472a;--ok:#2f7d5b}
*{box-sizing:border-box}
body{margin:0;background:#eef0f3;color:var(--ink);font-family:"Pretendard","Apple SD Gothic Neo","Malgun Gothic",-apple-system,sans-serif;line-height:1.65;font-size:14px;word-break:keep-all}
main{max-width:900px;margin:24px auto;background:#fff;padding:56px 64px 72px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.doc-head{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid var(--navy);padding-bottom:16px}
.eyebrow{font-size:11.5px;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;font-weight:600}
h1{font-size:26px;margin:6px 0 0;color:var(--navy);letter-spacing:-.01em}
.meta{font-size:12.5px;color:var(--muted);text-align:right;line-height:1.5}
h2{font-size:19px;margin:44px 0 4px;color:var(--navy);padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:0 0 8px}
h4{font-size:13px;margin:18px 0 4px;color:var(--accent);font-weight:700}
.sub{color:var(--muted);font-size:12.8px;margin:6px 0 14px}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:22px 0 4px}
.kpi{border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:30px;font-weight:800;color:var(--navy);font-variant-numeric:tabular-nums;line-height:1.2}
.kpi .v small{font-size:14px;font-weight:600;color:var(--muted);margin-left:4px}
.kpi .k{font-size:13px;font-weight:600;margin-top:4px}
.kpi .d{font-size:12px;color:var(--muted);margin-top:2px}
.lead{font-size:16px;font-weight:700;line-height:1.55;margin:4px 0 0}
ul.obs{margin:4px 0 0;padding-left:18px}
ul.obs li{margin:3px 0}
table{border-collapse:collapse;width:100%;font-size:13px;table-layout:auto}
th,td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{font-weight:600;color:#374151;background:var(--soft);font-size:12.5px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.no{width:28px;color:var(--muted);font-variant-numeric:tabular-nums}
td.kind{white-space:nowrap;width:1%}
td code,li code,p code,dd code{font-size:12px;background:var(--soft);padding:1px 4px;border-radius:4px;word-break:break-all}
.pill{display:inline-block;font-size:11.5px;border-radius:999px;padding:1px 8px;background:var(--soft);color:#374151;white-space:nowrap}
.pill.gate{background:#fbe7df;color:#8a2f17}.pill.structure{background:#e3ecf7;color:#1B2A4A}.pill.protected{background:#ece6f6;color:#4b2a7a}.pill.rule{background:#e6f2ec;color:#235c43}
.finding{border:1px solid var(--line);border-radius:10px;margin:18px 0}
.finding .fh,.finding .grid,.scenes li{break-inside:avoid}
.finding .fh{padding:12px 16px;border-bottom:1px solid var(--line)}
.finding .fh h3{margin:0}
.finding .grid{display:grid;grid-template-columns:1fr 1fr;font-size:12.8px}
.finding .grid div{padding:7px 16px;border-bottom:1px solid var(--line)}
.finding .grid div:nth-child(odd){border-right:1px solid var(--line)}
.finding .grid b{color:var(--muted);font-weight:600;margin-right:8px}
.finding .grid .hit{background:#fdf3ef}
.finding .fb{padding:4px 16px 14px}
.finding .fb p{margin:4px 0}
.ref{color:var(--muted);font-size:11.5px}
.scenes{margin:6px 0 0;padding:0;list-style:none}
.scenes li{font-size:12px;border-left:3px solid var(--accent);background:var(--soft);padding:6px 10px;margin:6px 0;border-radius:0 6px 6px 0}
.scenes .when{color:var(--muted);font-size:11.5px;display:block}
.scenes code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;word-break:break-all;white-space:pre-wrap}
.btns{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
.btn{border:1px solid var(--navy);background:#fff;color:var(--navy);border-radius:7px;padding:5px 12px;font-size:12.5px;cursor:pointer;font-family:inherit}
.btn.add{background:var(--navy);color:#fff}
.copied{font-size:12px;color:var(--ok)}
ol.notes{font-size:11.8px;color:var(--muted);padding-left:18px;margin:8px 0 0}
.areas{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.area{border:1px solid var(--line);border-radius:10px;padding:10px}
.area h4{margin:0 0 6px;font-size:12.5px;color:var(--navy)}
.mods{display:flex;flex-wrap:wrap;gap:5px}
.mod{border-radius:6px;padding:5px 7px;font-size:11px;min-width:84px;border:1.5px solid transparent;line-height:1.35}
.mod b{display:block;font-size:11.5px}
.mod.p{border-color:var(--navy)}
.tag{display:inline-block;font-size:10px;border-radius:4px;padding:0 4px;margin:2px 2px 0 0;background:rgba(0,0,0,.06)}
.tag.d{background:#f6d5c8;color:#7a2c12}.tag.t{background:#fff1c2;color:#6b5200}.tag.pr{background:#dde3f0;color:#1B2A4A}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:8px 0 10px;align-items:center}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:4px}
.fig{border:1px solid var(--line);border-radius:10px;padding:14px;margin:10px 0;break-inside:avoid}
.cap{font-size:12px;color:#666;margin-top:6px}
.muted{color:var(--muted);font-size:12.5px}
footer{margin-top:48px;font-size:11.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:10px}
@page{size:A4;margin:14mm 14mm 16mm}
@media print{body{background:#fff;font-size:11.8px;line-height:1.6}main{margin:0;padding:0;max-width:none;box-shadow:none}
  .btns{display:none}h2,h4{break-after:avoid}.area,.fig,.kpi,tr{break-inside:avoid}.pb{break-before:page}
  .kpis{margin:14px 0 0}.kpi{padding:10px 14px}.kpi .v{font-size:26px}h4{margin:13px 0 3px}.lead{font-size:14px}th,td{padding:6px 9px}}
"""

JS = """
function cp(b){var c=b.getAttribute('data-cmd');function done(){var s=b.parentNode.querySelector('.copied');if(s){s.textContent='복사됨: '+c;}}
if(navigator.clipboard){navigator.clipboard.writeText(c).then(done,done);}else{done();}}
"""

KIND_LABEL = {"gate": "막는 장치", "protected": "보호 영역", "structure": "구조 개선", "rule": "규칙"}
# 근거 문장 속 세션·커밋 식별자(예: 01a06b39 H12, chunk2) — 본문에서 흐리게 보인다
_REF = re.compile(r"\b((?:[0-9a-f]{8}(?:\s?H\d+(?:·H\d+)*)?)|chunk\d+)\b")


_TICK = re.compile(r"`([^`]+)`")


def _md(text):
    """모델 출력의 `코드` 표기를 <code>로. 나머지는 이스케이프."""
    return _TICK.sub(lambda m: "<code>%s</code>" % m.group(1), e(text))


def _evidence(text):
    return _REF.sub(lambda m: "<span class='ref'>%s</span>" % m.group(1), _md(text))


def _heat(score, mx):
    t = min(1.0, score / mx) if mx else 0
    # 낮음 #eef1f6 → 높음 #e9967a (한 색상 계열 단계)
    a = (238, 241, 246)
    b = (233, 150, 122)
    c = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % c


def _area_of(mid):
    parts = PurePosixPath(mid).parts
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if parts else mid)


def _hits(p):
    bt = p.get("backtest") or {}
    return bt.get("would_block") if isinstance(bt.get("would_block"), int) else None


# ---------------------------------------------------------------- 1쪽 경영진 요약

def _period_text(b):
    """기준선 dict → '기간, 대화 N개, 지시 M건'."""
    period = b.get("period") or ["", ""]
    return "%s ~ %s 동안의 AI 대화(세션) %s개, 사람이 친 지시 %s건" % (
        e((period[0] or "")[:10]), e((period[1] or "")[:10]), e((b.get("sessions") or {}).get("main_with_prompts", "-")),
        e(format(b["human_prompts"], ",") if isinstance(b.get("human_prompts"), int) else "-"))


def _scope(bl, n_modules):
    """머리말 — 재진단이면 설치 전 기준선 기간과 이번에 다시 읽은 기간을 나란히(D5)."""
    b, lt = bl.get("before") or {}, bl.get("latest") or {}
    if lt.get("period") and lt.get("period") != b.get("period"):
        return "이번 진단은 %s(설치 전 기준선: %s), 소스 모듈 %d개, 커밋 기록" % (_period_text(lt), _period_text(b), n_modules)
    return "%s, 소스 모듈 %d개, 커밋 기록" % (_period_text(b), n_modules)


def _correction_note(bl):
    """옛 기준선 정정 문구(D4) — 이전 리포트의 개입 비교가 틀렸다는 것을 숫자로."""
    c = bl.get("guardian_correction") or {}
    if c.get("status") != "corrected":
        return ""
    return ("<p class='sub'><b>기준선 정정</b> — 처음 기준선은 Codex 자동 승인 검토 요청 %s건을 사람이 친 지시로 함께 셌습니다"
            "(지시 %s건 → %s건, AI 대화당 사람 개입 중간값 %s → %s). 이 리포트의 \"하네스 전\"은 사람이 직접 친 지시만 다시 잰 값입니다. "
            "이전 리포트의 \"사람 개입 감소\" 비교는 이 오류가 섞여 있어 크게 잡혀 있었습니다.</p>" % (
                e(format(c.get("guardian_requests") or 0, ",")),
                e(format(c["human_prompts_before"], ",") if isinstance(c.get("human_prompts_before"), int) else "-"),
                e(format(c["human_prompts_after"], ",") if isinstance(c.get("human_prompts_after"), int) else "-"),
                e(c.get("interventions_median_before", "-")), e(c.get("interventions_median_after", "-"))))


def section_summary(diag, bl, facts, map_data):
    b = bl.get("before") or {}
    rc = b.get("recheck_after_done_ratio") or {}
    props = (diag or {}).get("proposals") or []
    hits = [h for h in (_hits(p) for p in props) if h]
    hit_nos = [p["no"] for p in props if _hits(p)]
    runs = (facts.get("test_baseline") or {}).get("runs") or []
    ce = sum(len(r.get("collection_errors") or []) for r in runs)
    tf = sum(len(r.get("failed") or []) for r in runs)
    kpis = []
    if rc.get("value") is not None:
        kpis.append(("%.1f%%" % (100 * rc["value"]), "%s/%s" % (format(rc.get("numerator", 0), ","), format(rc.get("denominator", 0), ",")),
                     "완료 보고 뒤 사람이 다시 확인", "AI가 끝났다고 한 뒤 사람이 직접 확인하고 다시 고치라고 한 비율입니다"))
    if hits:
        kpis.append(("최소 %s건" % format(sum(hits), ","), "", "하네스가 있었다면 막았을 장면",
                     "아래 제안 %s번을 지난 기록에 적용해 본 결과입니다" % ", ".join(str(n) for n in hit_nos)))
    if runs:
        kpis.append(("%d / %s" % (ce, format(tf, ",")), "", "테스트 수집 오류 / 실패",
                     "지금 전체 테스트를 돌렸을 때의 결과입니다"))
    kpi_html = "".join("<div class='kpi'><div class='v'>%s<small>%s</small></div><div class='k'>%s</div><div class='d'>%s</div></div>" % (
        e(v), e(sm), e(k), e(d)) for v, sm, k, d in kpis[:3])
    out = ["<p class='sub'>%s를 코드와 함께 살펴봤습니다. 문제는 사람이 아니라 도구와 작업 과정의 빈틈으로 적었습니다.</p>" % _scope(
               bl, len(map_data.get("modules", []))),
           _correction_note(bl),
           "<div class='kpis'>%s</div>" % kpi_html if kpi_html else ""]
    sm = (diag or {}).get("summary")
    if isinstance(sm, dict):
        if sm.get("headline"):
            out.append("<h4>핵심</h4><p class='lead'>%s</p>" % _md(sm["headline"]))
        if sm.get("observations"):
            out.append("<h4>반복되는 문제</h4><ul class='obs'>%s</ul>" % "".join("<li>%s</li>" % _evidence(x) for x in sm["observations"]))
        if sm.get("recommendation"):
            out.append("<h4>제안드리는 순서</h4><p>%s</p>" % _md(sm["recommendation"]))
    elif isinstance(sm, str) and sm.strip():
        out.append("<h4>반복되는 문제</h4><p>%s</p>" % _evidence(sm.strip()))
    return "\n".join(out)


def section_findings_table(diag):
    props = (diag or {}).get("proposals") or []
    if not props:
        if (diag or {}).get("status") == "no_proposals" and not (diag or {}).get("checks", {}).get("format_errors"):
            return "<p class='muted'>새 제안이 없습니다 — 이미 보여 드렸거나 빼신 제안은 다시 내지 않습니다.</p>"
        why = (diag or {}).get("reason") or "; ".join((diag or {}).get("checks", {}).get("format_errors") or []) or "진단을 돌리지 않았습니다"
        return "<p class='muted'>제안이 없습니다 — %s</p>" % e(why)
    out = ["<table><tr><th>#</th><th>제안</th><th>종류</th><th class='num'>지난 기록에서 막았을 장면</th></tr>"]
    for p in props:
        h = _hits(p)
        out.append("<tr><td class='no'>%d</td><td>%s</td><td class='kind'><span class='pill %s'>%s</span></td><td class='num'>%s</td></tr>" % (
            p["no"], _md(p["title"]), e(p["kind"]), e(KIND_LABEL.get(p["kind"], p["kind"])),
            ("최소 %d건" % h) if h else ("0건" if h == 0 else "<span class='muted'>—</span>")))
    out.append("</table>")
    return "\n".join(out)


# ---------------------------------------------------------------- 제안 상세 카드

def section_findings(diag, root):
    props = (diag or {}).get("proposals") or []
    per = config.get("report.scenes_per_card", root)
    out = []
    for p in props:
        bt = p.get("backtest") or {}
        h = _hits(p)
        if h:
            hit = "<div class='hit'><b>지난 기록</b>최소 %d건을 막았을 것 (AI 대화 %d개)</div>" % (h, bt.get("sessions", 0))
        elif h == 0:
            hit = "<div><b>지난 기록</b>아직 없음 — 다시 생기지 않게 막아 둡니다</div>"
        else:
            hit = "<div><b>지난 기록</b>테스트·구조를 보강하는 제안이라 해당 없음</div>"
        scenes = ""
        if bt.get("scenes"):
            items = []
            for sc in bt["scenes"][:per]:
                where = "AI 대화" if sc["source"] == "session" else "커밋 %s" % sc["ref"]
                items.append("<li><span class='when'>%s · %s</span><code>%s</code></li>" % (
                    e((sc.get("at") or "")[:10]), e(where), e(util.scrub_user(sc.get("match") or "")[:180])))
            more = " 외 %d건" % (h - per) if h and h > per else ""
            scenes = "<h4>이 장치가 있었다면 막았을 장면 (대표 %d건%s)</h4><ul class='scenes'>%s</ul>" % (min(per, len(bt["scenes"])), more, "".join(items))
        extra = ""
        if p.get("rule_text"):  # [추가]하면 AGENTS.md 규칙 블록에 이 문장이 그대로 들어간다
            extra += "<h4>넣을 규칙 문장</h4><p><code>%s</code></p>" % e(p["rule_text"])
        if p.get("requires"):
            from vmc_harness.bootstrap import requires as breq
            extra += "<h4>켜는 조건</h4><p>%s — 조건이 맞을 때까지 꺼 둔 채로 넣고, 맞으면 하네스가 켭니다.</p>" % e(
                ", ".join(breq.describe(x) for x in p["requires"]))
        if p.get("converted_from") == "gate":
            extra += "<p class='muted'>무엇을 실행하게 하는 제안이라 막는 장치가 아니라 구조 개선으로 넣습니다.</p>"
        out.append("""<div class='finding'><div class='fh'><div class='eyebrow'>제안 %d</div><h3>%s</h3></div>
<div class='grid'><div><b>종류</b><span class='pill %s'>%s</span></div>%s<div><b>적용 범위</b>%s</div><div><b>부담</b>%s</div></div>
<div class='fb'><h4>근거</h4><p>%s</p><h4>막아 주는 것</h4><p>%s</p>%s%s
<div class='btns'><button class='btn add' data-cmd='vmc-harness diagnose accept %d' onclick='cp(this)'>적용 — vmc-harness diagnose accept %d</button>
<button class='btn' data-cmd='vmc-harness diagnose exclude %d' onclick='cp(this)'>빼기</button><span class='copied'></span></div></div></div>""" % (
            p["no"], _md(p["title"]), e(p["kind"]), e(KIND_LABEL.get(p["kind"], p["kind"])), hit, _evidence(p["scope"]), _md(p["cost"]),
            _evidence(p["evidence"]), _md(p["blocks"]), extra, scenes, p["no"], p["no"], p["no"]))
    if props:
        out.append("<p class='sub'>[적용]을 누르면 명령이 복사됩니다. AI 대화창에 \"1, 3번 넣어줘\"라고 하셔도 같습니다. 아무것도 고르지 않으시면 그대로 두고, 다시 묻지 않습니다.</p>")
    return "\n".join(out)


# ---------------------------------------------------------------- 위험이 큰 곳

def section_risk_table(rows, root):
    top = rows[:config.get("report.risk_table_rows", root)]
    if not top:
        return "<p class='muted'>소스 모듈을 찾지 못했습니다.</p>"
    out = ["<table><tr><th>모듈</th><th>맡은 일</th><th class='num'>코드 줄</th><th class='num'>고친 대화</th>"
           "<th class='num'>오류 신호</th><th>테스트</th></tr>"]
    for r in top:
        lf = r.get("largest_file") or {}
        big = "<br><span class='ref'>가장 큰 파일 %s줄</span>" % format(lf.get("lines", 0), ",") if lf.get("lines", 0) >= 3000 else ""
        out.append("<tr><td><code>%s</code>%s</td><td>%s</td><td class='num'>%s%s</td><td class='num'>%d</td><td class='num'>%d</td><td>%s</td></tr>" % (
            e(r["id"]).replace("/", "/<wbr>"), " <span class='pill'>보호 후보</span>" if r["protected"] else "", e(r["responsibility"] or "-"),
            format(r["lines"], ","), big, r["edit_sessions"], r["defects"], "<b>없음</b>" if r["no_tests"] else "있음"))
    out.append("</table><ol class='notes'><li>고친 대화: 이 모듈을 수정한 AI 대화 수입니다. 여러 대화에서 다시 고칠수록 AI가 헤맨 곳입니다.</li>"
               "<li>오류 신호: 오류가 난 뒤 다시 고친 횟수 + 수정·되돌림 커밋 + 지금 실패하는 테스트를 더한 값입니다.</li>"
               "<li>테스트 없음: 이 모듈을 확인하는 테스트가 없다는 뜻입니다.</li></ol>")
    return "\n".join(out)


def section_risk_map(rows, root):
    top = rows[:config.get("report.risk_top_modules", root)]
    if not top:
        return ""
    mx = max(r["score"] for r in top) or 1
    areas = {}
    for r in top:
        areas.setdefault(_area_of(r["id"]), []).append(r)
    out = ["<div class='legend'><span><span class='sw' style='background:#eef1f6'></span>위험 낮음</span>"
           "<span><span class='sw' style='background:#e9967a'></span>위험 높음</span>"
           "<span><span class='sw' style='border:1.5px solid #1B2A4A'></span>보호 영역 후보</span>"
           "<span><span class='tag d'>오류</span>오류 신호</span>"
           "<span><span class='tag t'>테스트 없음</span>부르는 테스트 없음</span></div><div class='areas'>"]
    for area, rs in sorted(areas.items(), key=lambda kv: -max(r["score"] for r in kv[1])):
        out.append("<div class='area'><h4>%s</h4><div class='mods'>" % e(area))
        for r in rs:
            name = PurePosixPath(r["id"]).name if r["id"] != area else PurePosixPath(r["id"]).name + "/"
            tags = []
            if r["edit_sessions"]:
                tags.append("<span class='tag'>%d대화</span>" % r["edit_sessions"])
            if r["defects"]:
                tags.append("<span class='tag d'>오류 %d</span>" % r["defects"])
            if r["no_tests"]:
                tags.append("<span class='tag t'>테스트 없음</span>")
            out.append("<div class='mod%s' style='background:%s' title='%s'><b>%s</b>%s줄 %s</div>" % (
                " p" if r["protected"] else "", _heat(r["score"], mx), e(r["id"] + " — " + (r["responsibility"] or "")),
                e(name), format(r["lines"], ","), "".join(tags)))
        out.append("</div></div>")
    out.append("</div>")
    return "\n".join(out)


# ---------------------------------------------------------------- 기준선

# 기준선 정의를 독자 말로(원래 정의는 baseline.json definitions — 측정 방법의 원본)
PLAIN_DEF = {
    "recheck_after_done_ratio": "AI가 완료를 보고한 뒤 사람이 결과를 직접 확인하고 다시 고치라고 한 지시 ÷ 전체 지시입니다.",
    "interventions_per_session": "사람이 친 지시 + AI가 사람에게 물은 질문입니다. 사람이 직접 연 대화만 셉니다.",
    "interventions_per_10_tool_calls": "AI 동작(파일 읽기·수정·명령 실행) 10번마다 사람이 끼어든 횟수입니다. 작업이 커져서 늘어난 개입과 구분하려고 보는 참고 지표입니다.",
    "repeated_instructions": "앞선 다른 대화에서 거의 같은 문장으로 이미 했던 지시입니다. 규칙이 대화를 넘어 이어지지 않는다는 신호입니다.",
    "qa_human_prompts_per_qa_session": "QA 대화(브라우저 자동화·스크린샷을 썼거나 QA를 언급한 대화)에서 사람이 친 지시 수입니다.",
    "ai_questions_per_session": "AI가 사람에게 선택이나 확인을 물은 횟수입니다.",
    "tool_calls_per_session": "AI가 파일 읽기·수정·명령 실행을 한 횟수입니다.",
}


def _fmt_stat(st, key="median"):
    if not isinstance(st, dict) or st.get(key) is None:
        return "-"
    return "%s" % st[key]


def section_baseline(bl):
    if not bl or not bl.get("before"):
        return "<p class='muted'>AI 대화 기록이 없어 기준선은 만들지 않았습니다(신규 프로젝트 — 코드에서 출발합니다).</p>"
    b = bl["before"]
    a = bl.get("after") or {}
    d = bl.get("definitions") or {}
    rc = b.get("recheck_after_done_ratio") or {}
    rows = [
        ("완료 보고 뒤 사람이 다시 확인한 비율", "%.1f%% (%s/%s)" % (100 * rc["value"], rc["numerator"], rc["denominator"]) if rc.get("value") is not None else "-",
         a and (a.get("recheck_after_done_ratio") or {}).get("value") is not None and "%.1f%%" % (100 * a["recheck_after_done_ratio"]["value"]),
         PLAIN_DEF.get("recheck_after_done_ratio") if d.get("recheck_after_done_ratio") else None),
        ("AI 대화 1개당 사람이 끼어든 횟수 (중간값 / 평균)", "%s / %s" % (_fmt_stat(b.get("interventions_per_session")), _fmt_stat(b.get("interventions_per_session"), "mean")),
         a and "%s / %s" % (_fmt_stat(a.get("interventions_per_session")), _fmt_stat(a.get("interventions_per_session"), "mean")), PLAIN_DEF.get("interventions_per_session") if d.get("interventions_per_session") else None),
        ("AI 동작 10번당 사람이 끼어든 횟수 (중간값)", _fmt_stat(b.get("interventions_per_10_tool_calls")), a and _fmt_stat(a.get("interventions_per_10_tool_calls")), PLAIN_DEF.get("interventions_per_10_tool_calls") if d.get("interventions_per_10_tool_calls") else None),
        ("같은 지시를 다른 대화에서 다시 한 횟수", b.get("repeated_instructions"), a and a.get("repeated_instructions"), PLAIN_DEF.get("repeated_instructions") if d.get("repeated_instructions") else None),
        ("QA 한 번에 사람이 친 지시 수 (중간값)", _fmt_stat(b.get("qa_human_prompts_per_qa_session")), a and _fmt_stat(a.get("qa_human_prompts_per_qa_session")), PLAIN_DEF.get("qa_human_prompts_per_qa_session") if d.get("qa_human_prompts_per_qa_session") else None),
        ("AI 대화 1개당 AI가 사람에게 물은 횟수 (평균)", _fmt_stat(b.get("ai_questions_per_session"), "mean"), a and _fmt_stat(a.get("ai_questions_per_session"), "mean"), PLAIN_DEF.get("ai_questions_per_session") if d.get("ai_questions_per_session") else None),
        ("AI 대화 1개당 AI 동작 수 (중간값)", _fmt_stat(b.get("tool_calls_per_session")), a and _fmt_stat(a.get("tool_calls_per_session")), PLAIN_DEF.get("tool_calls_per_session") if d.get("tool_calls_per_session") else None),
    ]
    out = ["<table><tr><th>지표</th><th class='num'>지금</th><th class='num'>하네스 적용 후</th></tr>"]
    notes = []
    for name, v, after, df in rows:
        if df:
            notes.append(df)
        out.append("<tr><td>%s%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>" % (
            e(name), "<sup class='ref'> %d</sup>" % len(notes) if df else "", e(v),
            e(after) if after else "<span class='muted'>2차 방문 때</span>"))
    out.append("</table>")
    t = b.get("tagged")
    if t:
        out.append("<p class='sub'>사람 지시를 전부 나눠 보면 기획 보완 %d · 개발 결정 %d · 절차·도구 조작 %d · <b>AI 오류 바로잡기 %d</b>건입니다. 실질적인 지시(기획+결정+오류) 중 오류 바로잡기가 %.1f%%입니다.</p>" % (
            t["A"], t["B"], t["C"], t["D"], 100 * t.get("D_share_of_substantive", 0)))
    if notes:
        out.append("<ol class='notes'>%s</ol>" % "".join("<li>%s</li>" % e(n) for n in notes))
    return "\n".join(out)


# ---------------------------------------------------------------- 지난 진단 이후 (두 번째 진단부터 — remeasure)

def _metric_cell(m, key, field="median", pct=False):
    v = ((m or {}).get(key) or {}).get(field)
    if v is None:
        return "-"
    return "%.1f%%" % (100 * v) if pct else "%s" % v


def section_since(diag):
    m = (diag or {}).get("since_init")
    if not m:
        return ""
    sm = (diag or {}).get("summary")
    mt = m.get("metrics") or {}
    b, pv, nw = mt.get("before"), mt.get("previous"), mt.get("now")
    out = ["<h2>지난 진단 이후</h2><p class='sub'>하네스를 설치한 %s 이후 쌓인 기록을 같은 방법으로 다시 쟀습니다. 지난 진단은 %s입니다.</p>" % (
        e((m.get("since") or "-")[:10]), e((m.get("previous_diagnosis_at") or "-")[:10]))]
    if isinstance(sm, dict) and sm.get("since_last"):
        out.append("<p class='lead'>%s</p>" % _md(sm["since_last"]))
    rows = [
        ("AI 대화 수", (b or {}).get("sessions"), (pv or {}).get("sessions"), (nw or {}).get("sessions")),
        ("AI 대화 1개당 사람이 끼어든 횟수 (중간값 / 평균)",
         *("%s / %s" % (_metric_cell(x, "interventions_per_session"), _metric_cell(x, "interventions_per_session", "mean")) if x else None
           for x in (b, pv, nw))),
        ("AI 동작 10번당 사람이 끼어든 횟수 (중간값)", *(_metric_cell(x, "interventions_per_10_tool_calls") if x else None for x in (b, pv, nw))),
        ("완료 보고 뒤 다시 확인한 비율 (오류 신호 단어 기준)",
         *(_metric_cell(x, "recheck_after_done_ratio_keyword", "value", pct=True) if x else None for x in (b, pv, nw))),
        ("AI 대화 1개당 AI가 사람에게 물은 횟수 (평균)", *(_metric_cell(x, "ai_questions_per_session", "mean") if x else None for x in (b, pv, nw))),
    ]
    out.append("<table><tr><th>지표</th><th class='num'>하네스 전</th><th class='num'>지난 진단</th><th class='num'>지금</th></tr>")
    for name, x, y, z in rows:
        out.append("<tr><td>%s</td>%s</tr>" % (e(name), "".join("<td class='num'>%s</td>" % (e(v) if v not in (None, "") else "<span class='muted'>—</span>") for v in (x, y, z))))
    out.append("</table>")
    if not nw:
        out.append("<p class='muted'>설치 이후 AI 대화 기록이 아직 없어 \"지금\" 칸은 비어 있습니다.</p>")
    a = m.get("actions") or {}
    g = m.get("gates") or {}
    t = m.get("tasks") or {}
    p = m.get("proposals") or {}
    li = ["하네스가 넣은 장치 %d개 — 유지 %d · 시험 중 %d · 되돌림 %d" % (a.get("total", 0), a.get("kept", 0), a.get("trial", 0), a.get("reverted", 0))]
    if g:
        li.append("장치가 막은 장면 %d건 (사람이 \"그냥 진행\"으로 넘긴 것 %d건) · 장치가 돈 시간 합 %s초" % (
            g.get("blocked", 0), g.get("false_block", 0), g.get("sec", 0)))
    if t.get("measured"):
        li.append("작업 시간 예측이 맞은 비율 %d%% (시간을 잰 작업 %d건 중 %d건)" % (round(100 * (t.get("in_range_rate") or 0)), t["measured"], t.get("in_range", 0)))
    elif t.get("total"):
        li.append("시작한 작업 %d건 (시간을 잰 작업은 아직 없음)" % t["total"])
    li.append("지금까지 보인 제안 %d개 중 적용 %d개 · 뺀 것 %d개" % (p.get("shown", 0), p.get("added", 0), p.get("excluded", 0)))
    out.append("<ul class='obs'>%s</ul>" % "".join("<li>%s</li>" % e(x) for x in li))
    items = [x for x in g.get("items") or [] if x.get("fired")]
    if items:
        out.append("<table style='margin-top:10px'><tr><th>장치</th><th class='num'>발동</th><th class='num'>막음</th><th class='num'>그냥 진행</th><th class='num'>시간(초)</th></tr>")
        for x in sorted(items, key=lambda r: -r.get("blocked", 0)):
            out.append("<tr><td>%s</td><td class='num'>%d</td><td class='num'>%d</td><td class='num'>%d</td><td class='num'>%s</td></tr>" % (
                _md(x.get("name") or x.get("gate_id")), x.get("fired", 0), x.get("blocked", 0), x.get("false_block", 0), x.get("sec", 0)))
        out.append("</table>")
    out.append("<ol class='notes'><li>\"하네스 전\"은 설치 전 AI 대화로 잰 기준선, \"지금\"은 설치 이후 AI 대화만 같은 정의로 다시 잰 값입니다. 완료 뒤 재확인 비율은 같은 방법(오류 신호 단어)끼리만 비교합니다.</li>"
               "<li>\"그냥 진행\"은 장치가 막았는데 사람이 넘긴 경우입니다. 이런 일이 쌓이면 하네스가 그 장치를 스스로 풉니다.</li></ol>")
    return "\n".join(out)


# ---------------------------------------------------------------- 이 PC의 사용자 단위 지침·훅 (이름·요지·숫자만)

_ISSUE_LABEL = {"과한 검증": "과한 검증", "의도와 다른 지침": "의도와 다른 지침", "느린 게이트": "작업을 느리게 하는 장치"}


def _used_skill(s):
    u = s.get("used") or {}
    return ("%d회 (대화 %d개)" % (u["calls"], u["sessions"])) if u.get("calls") else "<span class='muted'>안 쓰임</span>"


def _used_hook(h):
    u = h.get("used") or {}
    if not u.get("runs"):
        return "<span class='muted'>기록 없음</span>"
    s = "%d회 · 합 %s초 · 보통 %sms" % (u["runs"], u.get("total_sec") if u.get("total_sec") is not None else "-", u.get("median_ms") if u.get("median_ms") is not None else "-")
    if u.get("errors"):
        s += " · 오류 %d" % u["errors"]
    if u.get("blocked"):
        s += " · 막음 %d" % u["blocked"]
    return e(s)


def section_user_setup(diag):
    us = (diag or {}).get("user_setup")
    ul = (diag or {}).get("user_level") or []
    if not isinstance(us, dict) or us.get("error"):
        return ""
    c, x = us.get("claude") or {}, us.get("codex") or {}
    plugins = [p for p in c.get("plugins") or [] if p.get("enabled")]
    skills = (c.get("skills") or []) + (x.get("skills") or []) + [s for p in plugins for s in p.get("skills") or []]
    hooks = (c.get("hooks") or []) + (x.get("hooks") or []) + [h for p in plugins for h in p.get("hooks") or []]
    docs = [("~/.claude/CLAUDE.md", c.get("claude_md")), ("~/.codex/AGENTS.md", x.get("agents_md")),
            ("~/.codex/AGENTS.override.md", x.get("agents_override_md"))]
    docs = [(n, d) for n, d in docs if isinstance(d, dict) and d.get("exists")]
    if not (skills or hooks or plugins or docs or ul):
        return ""
    out = ["<h2 class='pb'>이 PC에 걸려 있는 사용자 단위 지침·훅</h2>"
           "<p class='sub'>프로젝트 밖, 이 PC 사용자 설정(<code>~/.claude</code>·<code>~/.codex</code>)에 있어 AI가 이 프로젝트에서도 따르는 것들입니다. "
           "개인 설정이라 원문은 싣지 않고 이름과 요지, 지난 AI 대화에서 쓰인 횟수만 적었습니다. <b>하네스는 이 설정을 고치지 않습니다</b> — 아래 제안을 보고 바꿀지는 PC 주인이 정합니다.</p>"]
    if ul:
        out.append("<h4>점검 결과 — 제안만</h4>")
        for i, u in enumerate(ul, 1):
            out.append("<div class='finding'><div class='fh'><div class='eyebrow'>사용자 설정 %d · %s</div><h3>%s</h3></div>"
                       "<div class='fb'><p><b>대상</b> %s</p><p><b>근거</b> %s</p><p><b>바꾼다면</b> %s</p></div></div>" % (
                           i, e(_ISSUE_LABEL.get(u.get("issue"), u.get("issue") or "점검")), _md(u["title"]), _md(u["setting"]),
                           _evidence(u["evidence"]), _md(u["suggestion"])))
    if docs:
        out.append("<h4>지침 파일</h4><table><tr><th>파일</th><th class='num'>줄</th><th>제목</th></tr>")
        for n, d in docs:
            hs = d.get("headings") or []
            out.append("<tr><td><code>%s</code></td><td class='num'>%s</td><td>%s</td></tr>" % (
                e(n), format(d.get("lines", 0), ","), e(" · ".join(hs[:6]) + (" 외 %d개" % (len(hs) - 6) if len(hs) > 6 else "")) or "-"))
        out.append("</table>")
    if hooks:
        out.append("<h4>훅 — AI 동작마다 자동으로 도는 명령</h4><table><tr><th>언제</th><th>이름</th><th>어디서</th><th>지난 대화에서</th></tr>")
        for h in sorted(hooks, key=lambda h: -(((h.get("used") or {}).get("total_sec")) or 0)):
            when = (h.get("event") or "-") + ((" · " + h["matcher"]) if h.get("matcher") else "")
            out.append("<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
                e(when), e(h.get("label")), e(h.get("source")), _used_hook(h)))
        out.append("</table>")
    other = us.get("other_hook_runs") or []
    if other:
        out.append("<p class='muted'>그 밖에 프로젝트 설정 등에서 돈 훅: %s</p>" % e(" · ".join(
            "%s %d회·%s초" % (o.get("label"), o.get("runs", 0), o.get("total_sec") if o.get("total_sec") is not None else "-") for o in other[:8])))
    if plugins:
        out.append("<h4>켜져 있는 플러그인</h4><table><tr><th>이름</th><th class='num'>스킬</th><th>훅</th></tr>")
        for p in plugins:
            out.append("<tr><td><code>%s</code></td><td class='num'>%d</td><td>%s</td></tr>" % (
                e(p["name"]), len(p.get("skills") or []), e(", ".join(sorted({h["event"] for h in p.get("hooks") or []})) or "-")))
        out.append("</table>")
    if skills:
        used = [s for s in skills if (s.get("used") or {}).get("calls")]
        unused = [s for s in skills if not (s.get("used") or {}).get("calls")]
        out.append("<h4>스킬 — AI가 상황에 맞으면 꺼내 쓰는 지침</h4><table><tr><th>이름</th><th>어디서</th><th>요지</th><th>지난 대화에서</th></tr>")
        for s in sorted(used, key=lambda s: -(s["used"]["calls"])) + [s for s in unused if s.get("source") in ("user", "codex")]:
            out.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                e(s["name"]), e(s.get("source")), e(s.get("description") or "-"), _used_skill(s)))
        out.append("</table>")
        rest = [s for s in unused if s.get("source") not in ("user", "codex")]
        if rest:
            out.append("<p class='muted'>플러그인이 싣는 스킬 중 지난 대화에서 안 쓰인 것 %d개: %s</p>" % (
                len(rest), e(", ".join(s["name"] for s in rest[:20]) + (" 외" if len(rest) > 20 else ""))))
    out.append("<ol class='notes'><li>훅 시간은 Claude Code 대화 기록에 남은 실행 시간입니다. 스킬이 쓴 시간과 Codex 훅 시간은 기록에 없어 세지 않았습니다.</li>"
               "<li>지난 대화 %s개를 읽었습니다.</li></ol>" % e(us.get("sessions_read", "-")))
    return "\n".join(out)


# ---------------------------------------------------------------- 부록: 기계 사실

def section_facts(facts, claude_import_added=False):
    out = ["<table><tr><th>사실</th><th>값</th></tr>"]
    c = facts.get("commands") or {}
    out.append("<tr><td>소스 루트</td><td>%s</td></tr>" % e(", ".join(facts.get("source_roots") or [])))
    out.append("<tr><td>빌드·테스트·린트</td><td>%s</td></tr>" % (
        "<br>".join("%s: <code>%s</code>" % (e(k), e(v["command"])) for k, v in c.items()) or "-"))
    for r in (facts.get("test_baseline") or {}).get("runs") or []:
        if r.get("runner") == "pytest" and r.get("status") not in ("not_run", "timeout"):
            ce = r.get("collection_errors") or []
            val = "수집 %s개 · 수집 오류 %d건 · 실패 %d건 · 통과 %s" % (r.get("collected"), len(ce), len(r.get("failed") or []),
                                                          (r.get("counts") or {}).get("passed", "-"))
            if ce:
                val += "<br>" + "<br>".join("<code>%s</code> — %s" % (e(x["path"]), e(x["reason"][:140])) for x in ce[:5])
        else:
            val = e("%s %s" % (r.get("status"), r.get("reason", "")))
        out.append("<tr><td>테스트 기준선 (%s)</td><td>%s</td></tr>" % (e(r.get("dir")), val))
    db = facts.get("db") or {}
    out.append("<tr><td>DB</td><td>%s</td></tr>" % e(db.get("kind") or "없음"))
    sp = facts.get("sql_patch_dir") or {}
    if sp:
        v = "%s%s" % (sp.get("suggested") or "-", " (코드가 가리키지만 아직 없는 폴더)" if sp.get("suggested") in (sp.get("referenced_missing") or []) else "")
        out.append("<tr><td>SQL 패치 폴더</td><td>%s</td></tr>" % e(v))
    conv = facts.get("conventions") or []
    out.append("<tr><td>기존 관례</td><td>%s</td></tr>" % ("<br>".join(e(x["text"]) for x in conv) or "-"))
    am = facts.get("agents_md") or {}
    note = ("<br>CLAUDE.md가 AGENTS.md를 불러오지 않아 Claude Code가 하네스 규칙을 읽지 못하는 구조였습니다 — "
            "CLAUDE.md 끝에 <code>@AGENTS.md</code> 한 줄을 덧붙였습니다(기존 내용·줄끝은 그대로)") if claude_import_added else ""
    out.append("<tr><td>에이전트 지침 파일</td><td>%s%s</td></tr>" % (
        e(", ".join("%s %s" % (k, "있음" if v.get("exists") else "없음") for k, v in am.items())), note))
    out.append("</table>")
    pc = facts.get("protected_candidates") or []
    if pc:
        out.append("<h3 style='margin-top:20px'>보호 영역 후보 %d곳</h3><table><tr><th>경로</th><th>분류</th><th>이유</th><th class='num'>파일</th></tr>" % len(pc))
        for x in pc:
            out.append("<tr><td><code>%s</code></td><td style='white-space:nowrap'>%s</td><td>%s</td><td class='num'>%d</td></tr>" % (
                e(x["glob"]), e(CATEGORY_LABEL.get(x["category"], x["category"])), e(x["reason"]), x["file_count"]))
        out.append("</table>")
    return "\n".join(out)


def _diagrams():
    p = TEMPLATE_DIR / "diagrams.html"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def build_html(root, project_name=None):
    from vmc_harness.bootstrap import diagnose as dg, facts as bfacts, structmap
    root = Path(root)
    facts = bfacts.load(root)
    map_data = structmap.load(root) or {"modules": []}
    diag = dg.load(root) or {}
    bl = ledger.read_baseline(root) or {}
    sessions = dg.read_sessions_index(root)
    pglobs = [c["glob"] for c in facts.get("protected_candidates") or []]
    rows = risk_map(root, map_data, facts, sessions, pglobs)
    name = project_name or root.name
    date = now().strftime("%Y-%m-%d")
    n_props = len((diag or {}).get("proposals") or [])
    head = """<div class='doc-head'><div><div class='eyebrow'>코드베이스 진단 리포트</div><h1>%s</h1></div>
<div class='meta'>%s<br>VIBE MAFIA CLUB · VMC 하네스</div></div>""" % (e(name), e(date))
    body = [
        head,
        section_summary(diag, bl, facts, map_data),
        "<h4>제안 %d개</h4>" % n_props if n_props else "",
        section_findings_table(diag),
        section_since(diag),
        "<h2 class='pb'>제안 상세</h2><p class='sub'>제안마다 근거, 막아 주는 것, 적용 범위, 부담을 적었습니다. \"지난 기록\"은 제안을 지난 커밋과 AI 대화에 그대로 적용해 보고 확실히 걸리는 장면만 센 값이라, 실제보다 적게 잡혀 있습니다.</p>",
        section_findings(diag, root),
        section_user_setup(diag),
        "<h2 class='pb'>위험이 큰 곳 — 상위 %d개 모듈</h2><p class='sub'>AI가 여러 번 다시 고친 곳, 오류가 났던 곳, 테스트가 없는 곳, 보호가 필요한 곳을 합쳐 순서를 매겼습니다. 전체 지도는 부록 B에 있습니다.</p>" % config.get("report.risk_table_rows", root),
        section_risk_table(rows, root),
        "<h2 class='pb'>기준선</h2><p class='sub'>지난 작업 기록에서 잰 지금 수치입니다. 2차 방문 때 같은 방법으로 다시 재서 \"하네스 적용 후\" 칸을 채워 비교해 드리겠습니다.</p>",
        section_baseline(bl),
        "<h2 class='pb'>부록 A. 하네스가 하는 일</h2>",
        _diagrams(),
        "<h2 class='pb'>부록 B. 위험 지도 — 상위 %d개 모듈</h2>" % config.get("report.risk_top_modules", root),
        section_risk_map(rows, root),
        "<h2 class='pb'>부록 C. 코드에서 확인한 사실</h2><p class='sub'>코드와 설정에서 그대로 읽은 값입니다.</p>",
        section_facts(facts, bool((read_json(paths.harness_dir(root) / "config.json", {}) or {}).get("claude_import_added_at"))),
        "<footer>이 문서는 하네스가 프로젝트 안의 기록만으로 만들었습니다. 코드와 기록은 회사 PC 밖으로 나가지 않았고, 하네스가 이 문서를 메일이나 메신저로 보내지도 않습니다.</footer>",
    ]
    return "<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>%s 진단 리포트 %s</title><style>%s</style><script>%s</script></head><body><main>%s</main></body></html>" % (
        e(name), e(date), CSS, JS, "\n".join(body))


# ---------------------------------------------------------------- PDF·열기

def find_browser():
    cands = []
    if sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    elif sys.platform == "win32":
        pf = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA") if os.environ.get(k)]
        for base in pf:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    else:
        cands = [shutil.which(x) for x in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge") if shutil.which(x)]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def to_pdf(root, html_path, pdf_path):
    exe = find_browser()
    if not exe:
        return None, "Chrome/Edge 없음 — HTML만"
    import tempfile
    prof = tempfile.mkdtemp(prefix="vmh-pdf-")
    try:
        rc, out, err = run([exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                            "--user-data-dir=%s" % prof, "--no-pdf-header-footer",
                            "--print-to-pdf=%s" % str(pdf_path), Path(html_path).resolve().as_uri()],
                           timeout=config.get("report.pdf_timeout_s", root))
    finally:
        shutil.rmtree(prof, ignore_errors=True)
    if Path(pdf_path).is_file() and Path(pdf_path).stat().st_size > 0:
        return str(pdf_path), None
    return None, "PDF 만들기 실패(rc=%s)" % rc


def build(root, open_browser=True, out_dir=None, project_name=None):
    """리포트 HTML(+PDF). {"html","pdf","pdf_error"}"""
    root = Path(root)
    d = reports_dir(root)
    stamp = now().strftime("%Y-%m-%d")
    hp = d / ("diagnosis-%s.html" % stamp)
    hp.write_text(build_html(root, project_name), encoding="utf-8")
    pp = d / ("diagnosis-%s.pdf" % stamp)
    pdf, err = to_pdf(root, hp, pp)
    res = {"html": str(hp), "pdf": pdf, "pdf_error": err}
    if out_dir:
        od = paths.ensure(out_dir)
        shutil.copy2(str(hp), str(od / hp.name))
        res["html_copy"] = str(od / hp.name)
        if pdf:
            shutil.copy2(pdf, str(od / pp.name))
            res["pdf_copy"] = str(od / pp.name)
    if open_browser:
        try:
            webbrowser.open(hp.resolve().as_uri())
        except Exception:
            pass
    return res
