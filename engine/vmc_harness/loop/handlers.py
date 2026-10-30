"""qa·loop 영역 훅 핸들러 (hooks/contract.py HANDLER_MODULES — safety=False, fail-open).

- on_user_prompt: 마지막 활동 시각 + 루프가 돌면 중단 신호(복귀 즉시 양보)
- on_session_start: 지난 밤 준비된 A·B 정리를 아침 머지(백그라운드 — 세션을 기다리게 하지 않는다). 낡은 QA 진행 표시 정리
- on_pre_tool: QA 검사(qa.hooks) + Codex는 UserPromptSubmit이 없을 수 있어 활동 시각을 여기서도(미검증 12 #2)
루프 자신이 띄운 `claude -p`(VMC_HARNESS_NESTED=1)에서는 아무것도 하지 않는다 — 루프가 자기에게 양보하지 않게.
"""


from vmc_harness.core import llm


def on_user_prompt(ctx):
    if llm.nested() or not ctx.root:
        return None
    from vmc_harness.loop import activity
    activity.touch(ctx.root)
    return None


def on_session_start(ctx):
    if llm.nested() or not ctx.root:
        return None
    from vmc_harness.loop import activity, merge
    activity.session_started(ctx.root)
    try:
        from vmc_harness.qa import runner
        a = runner.active(ctx.root)
        if a:
            runner.clear_active(ctx.root)
    except Exception:
        pass
    if activity.is_running(ctx.root) or not merge.prepared_actions(ctx.root):
        return None
    from vmc_harness.core import config
    if not config.get("loop.auto_merge", ctx.root) or not config.get("loop.enabled", ctx.root):
        return None
    from vmc_harness.core.proc import spawn_detached
    from vmc_harness.loop import scheduler
    cmd = scheduler.tick_command(ctx.root)
    cmd = cmd[:cmd.index("loop")] + ["loop", "merge", "--root", str(ctx.root)]
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scheduler.engine_dir()) + os.pathsep + env.get("PYTHONPATH", "")
    spawn_detached(cmd, cwd=str(ctx.root), env=env)
    return None


def on_pre_tool(ctx):
    if llm.nested() or not ctx.root:
        return None
    if ctx.agent == "codex":
        from vmc_harness.loop import activity
        activity.touch(ctx.root)
    from vmc_harness.qa import hooks
    return hooks.on_pre_tool(ctx)
