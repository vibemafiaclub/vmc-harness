"""OS 스케줄러 등록 (spec 06·07 init 4단계) — 상주 프로세스 없이 `loop.check_interval_min`마다 `vmc-harness loop tick`.

- macOS: 사용자 LaunchAgent plist(`~/Library/LaunchAgents/`) + `launchctl bootstrap gui/<uid>` — 관리자 권한 없음
- Windows: 작업 XML(`loop-task.xml`, UTF-16)을 `schtasks /Create /XML`로 등록 — 현재 사용자 InteractiveToken·최소 권한,
  배터리여도 시작·유지(DisallowStartIfOnBatteries·StopIfGoingOnBatteries=false), WakeToRun=true.
  XML 등록이 막히면 `schtasks /Create /SC MINUTE /MO <분>`(배터리면 안 도는 기본값)으로 폴백. 콘솔 창이 뜨지 않게 pythonw 래퍼(.pyw)
- 등록이 막히면(사내 PC 정책 — 미검증 12 #8) 수동 모드: `start-loop`로만 돈다. 실패 사실은 알림(must) 1건
상태는 로컬 원본 `scheduler.json`.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.proc import run
from vmc_harness.core.project import local_dir, project_id


def label(root):
    return "com.vibemafiaclub.vmc-harness.loop.%s" % (project_id(root) or "unknown")[:8]


def task_name(root):
    return "VMC Harness Loop %s" % (project_id(root) or "unknown")[:8]


def state_path(root):
    return local_dir(root) / "scheduler.json"


def state(root):
    return read_json(state_path(root), None) or {"method": None}


def engine_dir():
    import vmc_harness
    return Path(vmc_harness.__file__).resolve().parent.parent


def tick_command(root):
    """스케줄러가 부를 명령(인자 리스트). 설치된 CLI가 있으면 그것(업데이트에도 경로 유지), 없으면 python -m."""
    exe = shutil.which("vmc-harness")
    if exe:
        return [exe, "loop", "tick", "--root", str(root)]
    return [sys.executable, "-m", "vmc_harness", "loop", "tick", "--root", str(root)]


def plist_path(root):
    return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % label(root))


def plist_content(root):
    interval = int(config.get("loop.check_interval_min", root)) * 60
    log = str(local_dir(root) / "loop-scheduler.log")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(engine_dir())}
    if os.environ.get("VMC_HARNESS_HOME"):
        env["VMC_HARNESS_HOME"] = os.environ["VMC_HARNESS_HOME"]
    data = {
        "Label": label(root),
        "ProgramArguments": tick_command(root),
        "StartInterval": interval,
        "RunAtLoad": False,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    return plistlib.dumps(data)


def pyw_path(root):
    return local_dir(root) / "loop-tick.pyw"


def pyw_content(root):
    cmd = tick_command(root)
    return ("# vmc-harness 루프 확인 작업(Windows 작업 스케줄러가 부른다)\n"
            "import subprocess, sys\n"
            "sys.path.insert(0, %r)\n"
            "cmd = %r\n"
            "if cmd[0].lower().endswith(('python.exe', 'pythonw.exe', 'python')):\n"
            "    from vmc_harness.cli import main\n"
            "    sys.exit(main(cmd[3:]))\n"
            "subprocess.run(cmd, creationflags=0x08000000)\n") % (str(engine_dir()), cmd)


def _pythonw():
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def windows_create_args(root):
    """schtasks 인자 리스트(사용자 권한 — /RU·/RL 없음)."""
    minutes = int(config.get("loop.check_interval_min", root))
    tr = subprocess.list2cmdline([_pythonw(), str(pyw_path(root))])
    return ["schtasks", "/Create", "/SC", "MINUTE", "/MO", str(minutes), "/TN", task_name(root), "/TR", tr, "/F"]


def xml_path(root):
    return local_dir(root) / "loop-task.xml"


def _windows_user():
    user = os.environ.get("USERNAME")
    if not user:
        return None
    dom = os.environ.get("USERDOMAIN")
    return "%s\\%s" % (dom, user) if dom else user


def windows_task_xml(root):
    """작업 스케줄러 XML(문자열). 파일에는 UTF-16으로 써야 schtasks가 읽는다."""
    from xml.sax.saxutils import escape
    from vmc_harness.core.clock import now
    minutes = int(config.get("loop.check_interval_min", root))
    start, stop = int(config.get("loop.start_hour", root)), int(config.get("loop.stop_hour", root))
    limit_h = (stop - start) % 24 + 1  # 한 번 실행 상한 = 밤 창 + 여유 1시간(그 뒤엔 stop_hour로 이미 끝났어야 한다)
    user = _windows_user()
    user_el = "<UserId>%s</UserId>" % escape(user) if user else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo><Description>vmc-harness 퇴근 뒤 정리 루프 확인</Description></RegistrationInfo>\n'
        '  <Triggers><TimeTrigger>\n'
        '    <Repetition><Interval>PT%dM</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>\n'
        '    <StartBoundary>%s</StartBoundary><Enabled>true</Enabled>\n'
        '  </TimeTrigger></Triggers>\n'
        '  <Principals><Principal id="Author">%s<LogonType>InteractiveToken</LogonType>'
        '<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n'
        '    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
        '    <Enabled>true</Enabled>\n'
        '    <Hidden>false</Hidden>\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
        '    <WakeToRun>true</WakeToRun>\n'
        '    <ExecutionTimeLimit>PT%dH</ExecutionTimeLimit>\n'
        '  </Settings>\n'
        '  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>%s</Arguments></Exec></Actions>\n'
        '</Task>\n'
    ) % (minutes, now().strftime("%Y-%m-%dT%H:%M:%S"), user_el, limit_h,
         escape(_pythonw()), escape(subprocess.list2cmdline([str(pyw_path(root))])))


def windows_xml_args(root):
    return ["schtasks", "/Create", "/XML", str(xml_path(root)), "/TN", task_name(root), "/F"]


def windows_delete_args(root):
    return ["schtasks", "/Delete", "/TN", task_name(root), "/F"]


def _uid():
    return os.getuid() if hasattr(os, "getuid") else 0


def _save_state(root, method, ok, reason=None, variant=None):
    data = {"method": method, "ok": ok, "reason": reason, "at": now_iso()}
    if variant:
        data["variant"] = variant
    write_json(state_path(root), data)


def _manual(root, reason):
    _save_state(root, "manual", False, reason)
    try:
        from vmc_harness.core import notices
        notices.add(root, "loop_scheduler_manual", "must",
                    "퇴근 뒤 자동 정리 예약을 이 PC에 못 걸었어요. 퇴근할 때 `vmc-harness start-loop`로 돌릴 수 있어요.",
                    refs={"reason": reason})
    except Exception:
        pass
    return {"ok": False, "method": "manual", "reason": reason}


def register(root):
    """{"ok", "method": "launchd|schtasks|manual", "reason"}"""
    root = Path(root).resolve()
    if sys.platform == "darwin":
        p = plist_path(root)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(plist_content(root))
        except OSError as e:
            return _manual(root, "plist 쓰기 실패: %s" % e)
        dom = "gui/%d" % _uid()
        run(["launchctl", "bootout", "%s/%s" % (dom, label(root))])
        rc, _, err = run(["launchctl", "bootstrap", dom, str(p)])
        if rc != 0:
            rc, _, err = run(["launchctl", "load", "-w", str(p)])
        if rc != 0:
            try:
                p.unlink()
            except OSError:
                pass
            return _manual(root, "launchctl 실패: %s" % (err or "").strip()[:200])
        _save_state(root, "launchd", True)
        return {"ok": True, "method": "launchd", "reason": None}
    if sys.platform == "win32":
        try:
            pyw_path(root).write_text(pyw_content(root), encoding="utf-8")
        except OSError as e:
            return _manual(root, "래퍼 쓰기 실패: %s" % e)
        xml_err = None
        try:
            xml_path(root).write_text(windows_task_xml(root), encoding="utf-16")
            rc, _, err = run(windows_xml_args(root))
            xml_err = None if rc == 0 else "xml: %s" % (err or "").strip()[:200]
        except OSError as e:
            xml_err = "xml 쓰기 실패: %s" % e
        if xml_err is None:
            _save_state(root, "schtasks", True, variant="xml")
            return {"ok": True, "method": "schtasks", "variant": "xml", "reason": None}
        rc, _, err = run(windows_create_args(root))  # 폴백: 배터리면 안 도는 기본값이지만 등록은 된다
        if rc != 0:
            return _manual(root, "schtasks 실패: %s / %s" % (xml_err, (err or "").strip()[:200]))
        _save_state(root, "schtasks", True, reason=xml_err, variant="minute")
        return {"ok": True, "method": "schtasks", "variant": "minute", "reason": xml_err}
    return _manual(root, "지원하지 않는 OS: %s" % sys.platform)


def unregister(root):
    root = Path(root).resolve()
    done = False
    if sys.platform == "darwin":
        p = plist_path(root)
        rc, _, _ = run(["launchctl", "bootout", "gui/%d/%s" % (_uid(), label(root))])
        if rc != 0 and p.exists():
            run(["launchctl", "unload", "-w", str(p)])
        if p.exists():
            p.unlink()
        done = True
    elif sys.platform == "win32":
        run(windows_delete_args(root))
        for f in (pyw_path(root), xml_path(root)):
            try:
                f.unlink()
            except OSError:
                pass
        done = True
    try:
        state_path(root).unlink()
    except OSError:
        pass
    return done


def is_registered(root):
    root = Path(root).resolve()
    if sys.platform == "darwin":
        rc, _, _ = run(["launchctl", "print", "gui/%d/%s" % (_uid(), label(root))])
        return rc == 0
    if sys.platform == "win32":
        rc, _, _ = run(["schtasks", "/Query", "/TN", task_name(root)])
        return rc == 0
    return False
