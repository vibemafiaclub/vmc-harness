"""카드 저장소 (spec 04 §브레인).

- 위치 `~/.vmc-harness/brain/` = git 레포(없으면 git init, 로컬 커밋까지). 원격은 선택(sync.link)
- 카드 1장 = 파일 1개 `cards/<id>.md` (원격과 합칠 때 충돌 없음)
- frontmatter: id·title·author·created_at·source(JSON 한 줄)·status·origin. **사용 횟수는 쓰지 않는다** — 집계(metrics)로
- 본문: 제목, 상황, 판단, 이럴 때 꺼낸다(신호), 적용하지 않는 경우, 원문 인용
- 충돌 기록은 `meta/conflicts/<card_id>/<uuid>.json`(한 건 = 파일 하나 — 팀원끼리 합쳐도 충돌 없음)
- 마피아MCP 카드 본문은 넣지 않는다(이용약관). 시드는 고객 세션 정정에서만
"""

import json
import os
import re
from pathlib import Path

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.ids import _b32, new_uuid
from vmc_harness.core.mask import mask
from vmc_harness.core.proc import run

STATUSES = ("active", "disabled", "reverted", "deleted")
ORIGINS = ("explicit_request", "learning")
SECTIONS = (("situation", "상황"), ("judgment", "판단"), ("signals", "이럴 때 꺼낸다"),
            ("not_when", "적용하지 않는 경우"), ("quote", "원문 인용"))
_FM_KEYS = ("id", "title", "author", "created_at", "source", "status", "origin", "kernel_version")


def write_lf(path, text):
    """줄바꿈을 LF로 고정해 쓴다(Windows에서도 카드 파일이 같게 — Path.write_text(newline=)은 3.10부터)."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def brain_dir():
    return paths.brain_dir()


def cards_dir():
    return brain_dir() / "cards"


def git_b(*args, timeout=30):
    return run(["git"] + list(args), cwd=str(brain_dir()), timeout=timeout)


def ensure_repo():
    d = paths.ensure(brain_dir())
    paths.ensure(d / "cards")
    if not (d / ".git").exists():
        git_b("init", "-q")
        readme = d / "README.md"
        if not readme.exists():
            readme.write_text("# 브레인\n\nVMC 하네스 판단 카드. 카드 1장 = `cards/<id>.md` 파일 1개. 기계가 쓴다.\n",
                              encoding="utf-8")
        git_b("add", "-A")
        _commit("brain: 시작")
    return d


def _ident_args():
    """커밋 작성자 — 사용자 git 설정이 없으면 하네스 이름으로(커밋 실패 방지)."""
    rc, out, _ = git_b("config", "user.name")
    rc2, out2, _ = git_b("config", "user.email")
    args = []
    if rc != 0 or not out.strip():
        args += ["-c", "user.name=vmc-harness"]
    if rc2 != 0 or not out2.strip():
        args += ["-c", "user.email=vmc-harness@localhost"]
    return args


def _commit(msg):
    rc, out, err = run(["git"] + _ident_args() + ["commit", "-q", "--no-verify", "-m", msg],
                       cwd=str(brain_dir()), timeout=30)
    return rc == 0


def current_author(root=None):
    """이 PC 사람 이름(작성자 표시·viewer 비교용) — core.project.author_name과 같은 값."""
    from vmc_harness.core.project import author_name
    return author_name(root)


# ---- 직렬화 ----

def _fm_value(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v if v is not None else "").replace("\n", " ")


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [l.strip(" -\t") for l in str(v).splitlines() if l.strip(" -\t")]


def render(card):
    lines = ["---"]
    for k in _FM_KEYS:
        lines.append("%s: %s" % (k, _fm_value(card.get(k))))
    lines += ["---", "", "# %s" % card.get("title", ""), ""]
    for key, label in SECTIONS:
        lines.append("## %s" % label)
        lines.append("")
        val = card.get(key)
        if key == "signals":
            lines += ["- %s" % s for s in _as_list(val)]
        elif key == "quote":
            lines += ["> %s" % l for l in str(val or "").splitlines()] or [">"]
        else:
            lines.append(str(val or "").strip())
        lines.append("")
    return "\n".join(lines)


def parse(text, path=None):
    card = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text.replace("\r\n", "\n"), re.S)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "source":
                try:
                    v = json.loads(v) if v else {}
                except ValueError:
                    v = {"raw": v}
            card[k] = v
        body = m.group(2)
    labels = {label: key for key, label in SECTIONS}
    cur, buf = None, []

    def flush():
        if cur is None:
            return
        txt = "\n".join(buf).strip()
        if cur == "signals":
            card[cur] = [l.strip()[2:].strip() for l in txt.splitlines() if l.strip().startswith("- ")]
        elif cur == "quote":
            card[cur] = "\n".join(l[1:].lstrip() if l.startswith(">") else l for l in txt.splitlines()).strip()
        else:
            card[cur] = txt
    for line in body.splitlines():
        if line.startswith("# ") and not card.get("title"):
            card["title"] = line[2:].strip()
            continue
        if line.startswith("## "):
            flush()
            cur, buf = labels.get(line[3:].strip()), []
            continue
        buf.append(line)
    flush()
    if path and not card.get("id"):
        card["id"] = Path(path).stem
    return card


# ---- 읽기 ----

def card_path(card_id):
    if not re.match(r"^[\w.-]+$", card_id or ""):
        raise KeyError(card_id)
    return cards_dir() / ("%s.md" % card_id)


def get_card(card_id):
    p = card_path(card_id)
    if not p.is_file():
        return None
    return parse(p.read_text(encoding="utf-8"), p)


def list_cards(include_inactive=False):
    d = cards_dir()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        try:
            c = parse(f.read_text(encoding="utf-8"), f)
        except (OSError, UnicodeDecodeError):
            continue
        if include_inactive or c.get("status", "active") == "active":
            out.append(c)
    return out


# ---- 쓰기 ----

def new_card_id():
    from vmc_harness.core.clock import now
    return "K-%s-%s" % (now().strftime("%y%m%d"), _b32())


def add_card(title, situation, judgment, signals, not_when, quote, author, source, origin, status="active"):
    """카드 1장 = 파일 1개 + 로컬 커밋. 반환: 파일 경로."""
    if origin not in ORIGINS:
        raise ValueError("카드 origin 오류: %s" % origin)
    if not (title or "").strip() or not (judgment or "").strip():
        raise ValueError("카드에 제목·판단 없음")
    ensure_repo()
    cid = new_card_id()
    card = {"id": cid, "title": mask(title.strip()), "author": author or current_author(),
            "created_at": now_iso(), "source": source or {}, "status": status, "origin": origin,
            "situation": mask(situation or ""), "judgment": mask(judgment), "signals": _as_list(signals),
            "not_when": mask(not_when or ""), "quote": mask(quote or ""), "kernel_version": KERNEL_VERSION}
    p = card_path(cid)
    write_lf(p, render(card))
    git_b("add", "--", str(p.relative_to(brain_dir())).replace("\\", "/"))
    _commit("brain: 카드 추가 %s" % cid)
    return p


def set_status(card_id, status, reason=None):
    if status not in STATUSES:
        raise ValueError(status)
    c = get_card(card_id)
    if c is None:
        return False
    if status == "deleted":
        p = card_path(card_id)
        rel = str(p.relative_to(brain_dir())).replace("\\", "/")
        rc, _, _ = git_b("rm", "-q", "--", rel)
        if rc != 0 and p.exists():
            p.unlink()
        _commit("brain: 카드 삭제 %s%s" % (card_id, (" — " + reason) if reason else ""))
        return True
    if c.get("status") == status:
        return True
    c["status"] = status
    p = card_path(card_id)
    write_lf(p, render(c))
    git_b("add", "--", str(p.relative_to(brain_dir())).replace("\\", "/"))
    _commit("brain: 카드 %s → %s%s" % (card_id, status, (" — " + reason) if reason else ""))
    return True


# ---- 충돌 기록 ----

def add_conflict(card_id, note=None, pc=None):
    ensure_repo()
    d = paths.ensure(brain_dir() / "meta" / "conflicts" / card_id)
    rec = {"card": card_id, "at": now_iso(), "pc_id": pc, "note": mask(note or "")[:200]}
    f = d / ("%s.json" % new_uuid())
    write_lf(f, json.dumps(rec, ensure_ascii=False) + "\n")
    git_b("add", "--", str(f.relative_to(brain_dir())).replace("\\", "/"))
    _commit("brain: 충돌 기록 %s" % card_id)
    return rec


def conflict_count(card_id):
    d = brain_dir() / "meta" / "conflicts" / card_id
    return len(list(d.glob("*.json"))) if d.is_dir() else 0


# ---- 브리핑용 (작성자 표시 — 12 결정: 본인 화면엔 이름, 다른 팀원 화면에선 뺀다) ----

def view(card, viewer=None, full=False):
    viewer = viewer if viewer is not None else current_author()
    keys = ("id", "title", "judgment", "signals", "not_when", "status", "origin", "created_at")
    if full:
        keys += ("situation", "quote", "source")
    out = {k: card.get(k) for k in keys}
    if card.get("author") and card.get("author") == viewer:
        out["author"] = card["author"]
    return out


def learned(since=None, viewer=None):
    """이번 주 배운 판단(브리핑 '이번 주 배운 것'). since='YYYY-MM-DD…' 이후 만든 활성 카드."""
    out = []
    viewer = viewer if viewer is not None else current_author()
    for c in list_cards():
        if since and (c.get("created_at") or "") < since:
            continue
        v = view(c, viewer)
        v["source"] = {k: (c.get("source") or {}).get(k) for k in ("project", "date") if (c.get("source") or {}).get(k)}
        out.append(v)
    out.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return out
