"""바뀐 줄 덮임 측정용 실행기 — 표준 라이브러리 sys.settrace만 쓴다(고객 PC 추가 설치 없음).

사용: python _cov_runner.py <결과.json> <대상 파일들(os.pathsep 구분)> -- <모듈> [인자...]
예:   python _cov_runner.py out.json /w/a.py -- unittest tests/test_a.py
결과: {"files": {"<대상 절대 경로>": [실행된 줄 번호...]}, "rc": <종료 코드>}
이 파일은 고객 프로젝트의 파이썬으로 단독 실행된다(엔진 패키지를 import하지 않는다).
"""

import json
import os
import runpy
import sys
import threading


def main():
    out_path = sys.argv[1]
    targets = {os.path.realpath(p) for p in sys.argv[2].split(os.pathsep) if p}
    sep = sys.argv.index("--")
    module = sys.argv[sep + 1]
    sys.argv = [module] + sys.argv[sep + 2:]
    sys.path.insert(0, os.getcwd())
    hits = {}
    cache = {}

    def target_of(fn):
        r = cache.get(fn)
        if r is None:
            rp = os.path.realpath(fn)
            r = rp if rp in targets else ""
            cache[fn] = r
        return r

    def tracer(frame, event, arg):
        t = target_of(frame.f_code.co_filename)
        if not t:
            return None
        hits.setdefault(t, set()).add(frame.f_lineno)

        def local(frame, event, arg):
            if event == "line":
                hits[t].add(frame.f_lineno)
            return local
        return local

    rc = 0
    sys.settrace(tracer)
    threading.settrace(tracer)
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        rc = 1
    finally:
        sys.settrace(None)
        threading.settrace(None)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"files": {k: sorted(v) for k, v in hits.items()}, "rc": rc}, f)
    sys.exit(rc)


if __name__ == "__main__":
    main()
