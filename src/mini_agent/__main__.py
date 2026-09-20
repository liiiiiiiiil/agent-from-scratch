import os
import sys
import unicodedata

# Enable terminal line editing (including reliable Backspace handling) when
# the platform provides Python's readline support.
try:
    import readline  # noqa: F401
except ImportError:
    pass

from mini_agent.agent import LLMResponseError, agent_loop
from mini_agent.context import ContextBudget, ContextManager
from mini_agent.instructions import InstructionLoader
from mini_agent.input_session import InputSession
from mini_agent.config import OUTPUT_MODE
from mini_agent.prompt import build_system_prompt
from mini_agent.state import AgentState, PlanRejected
from mini_agent.tools import create_registry, registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.output import TerminalOutput
from mini_agent.trace import TraceQueryError, build_trace, render_trace
from mini_agent.processes import ProcessManager
from mini_agent.providers.catalog import ProviderCatalog, load_provider_catalog
from mini_agent.resume import ResumeError, prepare_resume
from mini_agent.session import (
    DurableToolBoundary, SessionCommitUncertainError, SessionError, SessionStore,
)


def _single_line_notice(value, limit=240):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _plan_display_text(value):
    """Keep plan text complete while making terminal controls visible."""
    chars = []
    for char in value:
        if char == "\n":
            chars.append(char)
        elif unicodedata.category(char).startswith("C") or char in "\u2028\u2029":
            chars.append(char.encode("unicode_escape").decode("ascii"))
        else:
            chars.append(char)
    return "".join(chars)


def _render_plan_for_approval(state):
    """Render the current State revision and its CLI decisions without side effects."""
    snapshot = state.snapshot()
    revision_id = snapshot["planning_state"]["active_revision_id"]
    plan = snapshot["active_plan"]
    if not plan or plan.get("revision_id") != revision_id:
        return f"计划状态异常：待批 revision {revision_id} 的 active_plan 缺失或不匹配，无法展示审批内容。"

    def field(label, value, indent="  "):
        rendered = _plan_display_text(value).replace("\n", "\n" + indent)
        return f"{label}{rendered}"

    lines = [f"计划 revision {revision_id}"]
    if plan["parent_revision_id"] is not None:
        lines.append(f"父 revision：{plan['parent_revision_id']}")
    if plan["trigger_id"] is not None:
        lines.append(f"修订触发 ID：{plan['trigger_id']}")
    lines.extend((field("目标：", plan["goal"]), field("制订原因：", plan["reason"]), "限制："))
    lines.extend(field("- ", item) for item in plan["constraints"])
    if not plan["constraints"]:
        lines.append("- 无")
    lines.append("任务验收标准：")
    lines.extend(field("- ", item) for item in plan["success_criteria"])
    lines.append("步骤：")
    for index, step in enumerate(plan["steps"], 1):
        lines.append(field(
            f"{index}. [{step['status']}] {step['step_id']}：", step["content"], "   "))
        lines.append(f"   依赖：{', '.join(step['depends_on']) or '无'}")
        if step["replaces"]:
            lines.append(f"   替换：{', '.join(step['replaces'])}")
        lines.append("   本步骤验收：")
        lines.extend(field("   - ", item, "     ") for item in step["success_criteria"])
    lines.extend(("", f"计划 revision {revision_id} 等待决定：",
                  f"/approve {revision_id}", f"/reject {revision_id} <反馈>",
                  f"/continue {revision_id} <反馈>"))
    return "\n".join(lines)


def _render_process_wait(state):
    """Explain the non-terminal CLI handoff without exposing log bodies."""
    snapshot = state.snapshot()
    lines = ["后台进程或 stdin 写入仍未收束，任务暂不完成。"]
    lines.append(f"任务 ID：{snapshot.get('task_id') or '-'}")
    for process in snapshot.get("processes", []):
        if process.get("status") != "running" and not process.get("write_pending"):
            continue
        lines.append(
            f"process_id={process.get('process_id')} pid={process.get('pid')} "
            f"stdin={process.get('stdin_mode', 'closed')}/{process.get('stdin_state', 'disabled')} "
            f"write_pending={str(bool(process.get('write_pending', False))).lower()} "
            f"stdout_offset={process.get('stdout_offset', 0)} "
            f"stderr_offset={process.get('stderr_offset', 0)}"
        )
    plan = snapshot.get("active_plan") or {}
    unfinished = [
        step.get("content", "") for step in plan.get("steps", [])
        if step.get("status") != "completed"
    ]
    if unfinished:
        lines.append("未完成计划步骤：" + "；".join(unfinished[:5]))
    if snapshot.get("verification_required"):
        lines.append("验证义务：进程退出后必须在新 generation 中独立运行 verification。")
    if snapshot.get("repair_loop", {}).get("phase") != "idle":
        lines.append("修复义务：" + str(snapshot["repair_loop"].get("required_next_action", "继续处理")))
    lines.append("继续输入以观察任务；任务会先同步进程和 stdin 状态再恢复。")
    return "\n".join(lines)


def _render_crash_recovery(state, *, source_session_id=None, derived_session_id=None,
                           workspace_report=None):
    """Render bounded recovery facts without exposing original arguments."""
    snapshot = state.snapshot()
    lines = ["检测到未完成的 durable tool boundary，已进入崩溃恢复。"]
    if source_session_id:
        lines.append(f"源 session：{source_session_id}")
    if derived_session_id:
        lines.append(f"派生 session：{derived_session_id}")
    if workspace_report:
        lines.append("工作区变化（仅调查线索，不能证明 handler 是否执行）：")
        lines.extend(f"- {item}" for item in workspace_report[:20])
    orphaned = [item for item in snapshot.get("processes", [])
                if isinstance(item, dict) and item.get("status") == "orphaned"]
    if orphaned:
        lines.append("旧进程风险（仅记录，不可控制，也不声称已退出）：")
        lines.extend(
            f"- process_id={item.get('process_id')} pid={item.get('pid')} stdin="
            f"{item.get('stdin_mode', 'closed')}/{item.get('stdin_state', 'disabled')}"
            for item in orphaned[:16]
        )
    lines.append("调用分类：")
    for issue in snapshot.get("crash_issues", []):
        lines.append(
            f"- {issue.get('issue_id')}: {issue.get('tool')} / "
            f"{issue.get('classification')} / handler_admitted="
            f"{str(bool(issue.get('handler_admitted'))).lower()} / "
            f"status={issue.get('status')}"
        )
        if issue.get("reason"):
            lines.append(f"  原因：{_single_line_notice(issue.get('reason'), 300)}")
    unresolved = [item for item in snapshot.get("crash_issues", [])
                  if item.get("status") in ("unresolved", "investigating")]
    if unresolved:
        lines.append(
            f"请先只读调查，然后逐项使用 /resolve <issue_id> investigate|continue|block <反馈>。"
        )
    else:
        lines.append("未执行调用已自动补入明确结果；其余恢复义务已结算，下一步必须重新规划并验证。")
    return "\n".join(lines)


def main():
    global registry
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    argv = sys.argv[1:]
    input_session = InputSession()
    cli_output = TerminalOutput(OUTPUT_MODE)
    session_store = None
    session_id = None
    clean_shutdown = False
    resumed = False
    recovery_mode = "safe_point"
    source_session_id = None
    persistence_halted = False
    try:
        provider_catalog = load_provider_catalog()
        parent_binding = provider_catalog.parent_binding()
    except (ValueError, TypeError) as error:
        print(f"provider 配置错误：{_single_line_notice(error, 500)}")
        return
    if argv and argv[0] == "--resume":
        if len(argv) != 2 or not argv[1].strip():
            print("用法: python -m mini_agent --resume <session_id>")
            return
        session_id = argv[1]
        try:
            session_store = SessionStore()
            candidate = prepare_resume(
                session_store, session_id, os.getcwd(), provider_catalog=provider_catalog,
            )
            runtime = candidate.claim()
        except SessionCommitUncertainError as error:
            print(
                f"会话提交状态未确认（session_id={error.session_id}）："
                f"{_single_line_notice(error, 500)}；请检查磁盘文件和独占锁。"
            )
            return
        except (SessionError, ResumeError, ValueError) as error:
            print(f"会话恢复失败：{_single_line_notice(error, 500)}")
            return
        state = runtime.state
        context = runtime.context
        run_registry = runtime.registry
        process_manager = runtime.process_manager
        tool_executor = runtime.tool_executor
        protected_messages = runtime.protected_messages
        registry = run_registry
        source_session_id = runtime.source_session_id
        session_id = runtime.session_id
        recovery_mode = runtime.recovery_mode
        resumed = True
        first_task = None
        first_mode = "auto"
    else:
        state = AgentState()
        process_manager = ProcessManager()
        run_registry = create_registry(
            state, workspace_root=os.getcwd(), process_manager=process_manager,
            provider_catalog=provider_catalog,
        )
        registry = run_registry
        instructions = InstructionLoader(os.getcwd()).load()
        history = []
        system_prompt = build_system_prompt(project_instructions=instructions) if instructions else build_system_prompt()
        protected_messages = [{
            "role": "system",
            "content": system_prompt,
        }]
        # Keep the two-argument construction compatible with integrations that
        # replace ContextManager, then freeze the v0.36 parent binding on the
        # resulting context instance.
        context = ContextManager(state, history)
        context.budget = ContextBudget(
            window=parent_binding.profile.context_window,
            output_reserve_tokens=parent_binding.profile.max_output_tokens,
        )
        context.summarizer = lambda messages: parent_binding.complete(
            messages, include_tools=False, stream_output=False,
        ).message.get("content", "") or ""
        context.model_binding = parent_binding
        context.usage_meter = parent_binding.usage_meter
        context.protected_messages = protected_messages
        # Kept as a compatibility observer for callers using ToolExecutor.execute().
        # The agent loop's structured path suppresses this legacy callback.
        tool_executor = ToolExecutor(run_registry, on_result=state.record_tool)
    if resumed:
        # prepare_resume rebuilt the binding alongside the restored Context;
        # keep its UsageMeter so resumed summaries and parent calls share one
        # accounting ledger instead of replacing it with the startup probe.
        parent_binding = getattr(context, "model_binding", None) or parent_binding
    else:
        context.model_binding = parent_binding
    tool_executor.model_binding = parent_binding
    if not resumed and argv and argv[0] == "--plan":
        if len(argv) != 2 or not argv[1].strip():
            print('用法: python -m mini_agent --plan "<任务>"')
            return
        first_task = argv[1]
        first_mode = "plan_only"
    elif not resumed:
        first_task = argv[0] if argv else None
        first_mode = "auto"

    def cli_notice(message):
        cli_output.cli_notice(message)
        cli_output.close()

    def status_notice(message):
        cli_output.status_notice(message)
        cli_output.close()

    def get_session_store():
        nonlocal session_store
        if session_store is None:
            session_store = SessionStore()
        return session_store

    def sync_processes():
        task_id = getattr(state, "task_id", "")
        if not task_id:
            return
        if not hasattr(state, "sync_processes"):
            return
        state.sync_processes(process_manager.sync_processes(task_id))

    def cleanup_delegation_boundary() -> bool:
        """Cancel the one synchronous child before a task boundary or clean save."""
        manager = getattr(run_registry, "_delegation_manager", None)
        if manager is None or not manager.active_info().get("active"):
            return True
        task_id = getattr(state, "task_id", "") or None
        manager.cancel(task_id, "task_boundary")
        if manager.wait(2.0):
            return True
        info = manager.active_info()
        cli_notice(
            "子代理取消未在限定时间内收束；旧任务已保留，"
            f"delegation_id={info.get('delegation_id') or '-'}；"
            f"原因={_single_line_notice(info.get('cancel_reason') or 'timeout', 300)}。"
        )
        return False

    def save_session(handoff_status="active", manual=False):
        """Save only a complete safe point; failed saves leave State untouched."""
        nonlocal session_id, persistence_halted
        if persistence_halted:
            if manual:
                cli_notice("持久化已停止：请检查 session 文件和独占锁后重新启动。")
            return False
        if not getattr(state, "task", ""):
            if manual:
                cli_notice("用法: /save（当前没有活动任务）")
            return False
        sync_processes()
        try:
            envelope = get_session_store().save(
                session_id,
                state,
                context,
                workspace_root=os.getcwd(),
                handoff_status=handoff_status,
                save_kind="safe_point",
            )
        except SessionCommitUncertainError as error:
            session_id = error.session_id
            persistence_halted = True
            cli_notice(
                f"会话提交状态未确认（session_id={session_id}）："
                f"{_single_line_notice(error, 500)}；请检查磁盘文件和独占锁。"
            )
            return False
        except SessionError as error:
            if session_id is not None:
                persistence_halted = True
            cli_notice(f"会话保存失败：{_single_line_notice(error, 500)}")
            return False
        new_session = envelope["session_id"]
        first_save = session_id is None
        session_id = new_session
        if manual:
            cli_notice(("已保存会话：" if first_save else "已更新会话：") + new_session)
        return True

    if resumed:
        if recovery_mode == "crash_recovery" or state.has_unresolved_crash_recovery():
            cli_notice(_render_crash_recovery(
                state, source_session_id=source_session_id,
                derived_session_id=session_id, workspace_report=runtime.workspace_report,
            ))
        elif state.planning_state.phase == "awaiting_approval":
            cli_notice("会话已恢复；" + _render_plan_for_approval(state))
        elif state.status == "blocked":
            cli_notice(
                f"会话已恢复，任务仍处于 blocked：{_single_line_notice(state.terminal_reason)}；"
                "请使用 /resume <反馈>。"
            )
        elif state.status == "failed":
            cli_notice(
                f"会话已恢复，任务仍处于 failed：{_single_line_notice(state.terminal_reason)}；"
                "请使用 /new <任务>。"
            )
        else:
            restored_snapshot = state.snapshot()
            cli_notice(
                f"会话已恢复：任务={state.task or '-'}；"
                f"task_id={state.task_id or '-'}；状态={state.status}；"
                f"规划阶段={state.planning_state.phase}；修复阶段={state.repair_phase}；"
                f"verification_required={str(restored_snapshot['verification_required']).lower()}；等待用户输入。"
            )

    def cleanup_task_boundary():
        """Clean before State reset; an incomplete cleanup keeps the old task."""
        if persistence_halted:
            cli_notice("持久化已停止；当前任务不能安全切换或清空。")
            return False
        if not getattr(state, "task_id", ""):
            return True
        if not cleanup_delegation_boundary():
            return False
        sync_processes()
        report = process_manager.cleanup(state.task_id)
        if hasattr(state, "record_process_cleanup"):
            state.record_process_cleanup(report)
        if not report.complete:
            cli_notice(report.render())
            return False
        if session_id is not None and not save_session("clean"):
            return False
        return True

    def run_task(user_input, mode="auto", *, crash_investigation=False):
        nonlocal session_id, persistence_halted
        if persistence_halted:
            cli_notice("持久化已停止；请检查当前 session 文件和独占锁后重新启动。")
            return
        if not state.task:
            if hasattr(state, "begin_task"):
                state.begin_task(user_input, mode=mode)
            else:
                state.task = user_input
        if (hasattr(state, "has_unresolved_crash_recovery")
                and state.has_unresolved_crash_recovery() and not crash_investigation):
            cli_notice("崩溃恢复仍有未结算 issue；普通输入被拦截，请先使用 /resolve。")
            return
        if state.status == "awaiting_process":
            sync_processes()
            if state.status not in ("blocked", "failed"):
                state.resume_process_wait()
        if state.status in ("blocked", "failed"):
            reason = _single_line_notice(getattr(state, "terminal_reason", ""))
            cli_notice(
                (f"任务已阻塞：{reason}" if state.status == "blocked" else f"任务已失败：{reason}")
                + "；请使用 /new <任务>。"
            )
            return
        state.status = "running"
        context.history.append({"role": "user", "content": user_input})
        if session_id is not None:
            # The loop discovers this capability through the executor so old
            # integrations that still provide a two-argument agent_loop keep
            # working.
            tool_executor.session_boundary = DurableToolBoundary(
                get_session_store(), session_id, os.getcwd(),
            )
        else:
            tool_executor.session_boundary = None
        try:
            result = agent_loop(context, tool_executor)
        except SessionCommitUncertainError as error:
            session_id = error.session_id
            persistence_halted = True
            cli_notice(
                f"会话提交状态未确认（session_id={session_id}）："
                f"{_single_line_notice(error, 500)}；已停止后续工具和模型请求。"
            )
            return
        except SessionError as error:
            persistence_halted = True
            cli_notice(
                f"会话持久化失败：{_single_line_notice(error, 500)}；"
                "已停止后续工具和模型请求。"
            )
            return
        except LLMResponseError as error:
            state.status = "failed"
            cli_notice(f"服务商错误：{error}")
            return
        except Exception:
            state.status = "failed"
            raise
        if result == "达到最大迭代次数":
            if state.status not in ("blocked", "failed"):
                state.status = "failed"
            status_notice("达到最大迭代次数。")
        elif state.status == "blocked":
            reason = _single_line_notice(getattr(state, "terminal_reason", ""))
            status_notice(f"任务已阻塞：{reason}" if reason else "任务已阻塞：完成条件尚未满足。")
        elif state.status == "failed":
            reason = _single_line_notice(getattr(state, "terminal_reason", ""))
            status_notice(f"任务执行失败：{reason}" if reason else "任务执行失败。")
        elif state.status == "awaiting_process":
            cli_notice(_render_process_wait(state))
        elif getattr(getattr(state, "planning_state", None), "phase", None) == "awaiting_approval":
            cli_notice(_render_plan_for_approval(state))
        elif getattr(getattr(state, "planning_state", None), "phase", None) == "exploring":
            revision_id = state.planning_state.active_revision_id
            if revision_id is not None:
                status_notice(f"仍在调查 revision {revision_id}；若原计划可用，输入 /review {revision_id}。")
            else:
                status_notice("仍在只读调查阶段，请继续调查并提交计划。")
        elif (state.status == "running"
              and hasattr(state, "has_active_delegations")
              and state.has_active_delegations()):
            cli_notice("委派结果尚未提交；当前任务保持活动状态。")
        elif (state.status == "running"
              and not (hasattr(state, "has_unresolved_crash_recovery")
                       and state.has_unresolved_crash_recovery())):
            state.status = "done"
        if session_id is not None:
            save_session("active")

    try:
        # 命令行首条任务（可选）：与交互循环走同一套路径，
        # 保证 argv 分支后 history 状态完整，后续追问上下文不丢。
        if first_task is not None:
            run_task(first_task, mode=first_mode)

        while True:
            try:
                prompt = "你 › "
                user_input = input_session.read(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input or user_input.lower() in ("exit", "quit"):
                break
            cli_output.input_end()
            if user_input == "/resolve" or user_input.startswith("/resolve "):
                parts = user_input.split(maxsplit=3)
                if len(parts) != 4 or parts[2] not in ("investigate", "continue", "block"):
                    cli_notice("用法: /resolve <issue_id> investigate|continue|block <反馈>")
                    continue
                if not hasattr(state, "resolve_crash_issue"):
                    cli_notice("当前版本不支持 crash recovery issue。")
                    continue
                issue_id, decision, feedback = parts[1], parts[2], parts[3]
                try:
                    state.resolve_crash_issue(issue_id, decision, feedback)
                    if session_id is not None:
                        save_session("active")
                    if decision == "investigate":
                        run_task(
                            f"只读调查 crash recovery issue {issue_id}。用户反馈：{feedback}。"
                            "只能使用只读观察工具，不能执行副作用、verification 或重放原调用。",
                            crash_investigation=True,
                        )
                    elif decision == "continue":
                        if state.has_unresolved_crash_recovery():
                            cli_notice("该 issue 已结算；请继续处理其他未结算 issue。")
                        else:
                            run_task(
                                "所有 crash recovery issue 已由用户逐项结算。请进入只读 Explore，"
                                "提交或复核引用 crash_recovery trigger 的新计划，不得沿旧计划直接执行。"
                            )
                    else:
                        cli_notice("该任务已因用户决定 block；后续只允许 /new。")
                except (ValueError, PlanRejected) as error:
                    cli_notice(f"crash recovery 决定无效：{_single_line_notice(error, 500)}")
                continue
            if user_input == "/resume" or user_input.startswith("/resume "):
                if (hasattr(state, "has_unresolved_crash_recovery")
                        and state.has_unresolved_crash_recovery()):
                    cli_notice("崩溃恢复仍有未结算 issue；通用 /resume 被拦截，请先使用 /resolve。")
                    continue
                feedback = user_input[len("/resume"):].strip()
                if not feedback:
                    cli_notice("用法: /resume <反馈>")
                    continue
                try:
                    state.resume_blocked(feedback)
                    run_task(f"用户恢复 blocked 任务；反馈：{feedback}。请先在 Explore 中调查，并提交引用当前 trigger 的计划修订。")
                except (ValueError, PlanRejected) as error:
                    cli_notice(f"任务恢复无效：{error}")
                continue
            if user_input == "/save":
                save_session("active", manual=True)
                continue
            if user_input.startswith("/save "):
                cli_notice("用法: /save")
                continue
            if user_input.split(maxsplit=1)[0] in ("/approve", "/reject", "/continue", "/review"):
                if (hasattr(state, "has_unresolved_crash_recovery")
                        and state.has_unresolved_crash_recovery()):
                    cli_notice("崩溃恢复仍有未结算 issue；计划审批和复核被拦截，请先使用 /resolve。")
                    continue
                parts = user_input.split(maxsplit=2)
                command = parts[0]
                needs_feedback = command in ("/reject", "/continue")
                if (len(parts) < 2 or (needs_feedback and len(parts) != 3)
                        or (not needs_feedback and len(parts) != 2)):
                    cli_notice(f"用法: {command} <revision_id>" + (" <反馈>" if needs_feedback else ""))
                    continue
                try:
                    revision_id = int(parts[1])
                    if command == "/review":
                        state.review_current_plan(revision_id)
                        cli_notice(_render_plan_for_approval(state))
                        if session_id is not None:
                            save_session("active")
                    else:
                        decision = {
                            "/approve": "approved", "/reject": "rejected",
                            "/continue": "continue_exploring",
                        }[command]
                        state.decide_plan(decision, revision_id,
                                          parts[2] if needs_feedback else None)
                        if decision == "approved":
                            run_task(f"用户已批准当前计划 revision {revision_id}，请继续执行。")
                        elif decision == "continue_exploring":
                            run_task(f"用户要求继续只读调查 revision {revision_id}；反馈：{parts[2]}。必要时提交引用当前 trigger 的修订；若原计划仍合适，请说明，用户可用 /review 重新交付审批。")
                        else:
                            run_task(f"用户驳回计划 revision {revision_id}；反馈：{parts[2]}。请先只读调查，再提交引用当前 trigger 的修订计划。")
                except (ValueError, PlanRejected) as error:
                    cli_notice(f"计划决定无效：{error}")
                continue
            if (getattr(getattr(state, "planning_state", None), "phase", None) == "awaiting_approval"
                    and user_input not in ("/reset",) and not user_input.startswith(("/new ", "/trace"))):
                cli_notice("当前计划等待用户决定，请使用 /approve、/reject 或 /continue。")
                continue
            if user_input == "/trace" or user_input.startswith("/trace "):
                if not state.task:
                    cli_notice("当前没有活动任务，无法回放。")
                    continue
                parts = user_input.split()
                requested_generation = None
                requested_revision = None
                if len(parts) == 3 and parts[1] == "revision":
                    try:
                        requested_revision = int(parts[2])
                    except ValueError:
                        cli_notice("用法: /trace revision <revision_id>，revision_id 必须是正整数。")
                        continue
                elif len(parts) > 2:
                    cli_notice("用法: /trace [generation_id] 或 /trace revision <revision_id>")
                    continue
                elif len(parts) == 2:
                    try:
                        requested_generation = int(parts[1])
                    except ValueError:
                        cli_notice("用法: /trace [generation_id]，generation_id 必须是非负整数。")
                        continue
                try:
                    report = build_trace(
                        state.snapshot(), requested_generation,
                        revision_id=requested_revision,
                    )
                except TraceQueryError as error:
                    cli_notice(f"Trace 查询失败：{error}")
                    continue
                cli_notice(render_trace(report))
                continue
            if (hasattr(state, "has_unresolved_crash_recovery")
                    and state.has_unresolved_crash_recovery()
                    and not (user_input == "/new" or user_input.startswith("/new "))):
                if state.status in ("blocked", "failed", "done"):
                    cli_notice("任务已终止且仍有 crash recovery issue；只能使用 /new <任务>。")
                    continue
                cli_notice("崩溃恢复仍有未结算 issue；普通输入被拦截，请先使用 /resolve。")
                continue
            if user_input == "/reset":
                if not cleanup_task_boundary():
                    continue
                context.reset_task()
                if hasattr(state, "reset_task"):
                    state.reset_task()
                else:
                    state.task = ""
                    state.status = "idle"
                session_id = None
                cli_notice("当前任务已清空。输入任务开始，或使用 /new <任务>。")
                continue
            if user_input == "/new" or user_input.startswith("/new "):
                task = user_input[4:].strip()
                if not task:
                    cli_notice("用法: /new <任务>")
                    continue
                if not cleanup_task_boundary():
                    continue
                context.reset_task()
                if hasattr(state, "begin_task"):
                    state.begin_task(task)
                else:
                    state.task = task
                    state.status = "running"
                session_id = None
                cli_notice("已开始新任务。")
                run_task(task)
                continue
            run_task(user_input)
        clean_shutdown = True
    finally:
        if getattr(state, "task_id", ""):
            delegation_clean = cleanup_delegation_boundary()
            sync_processes()
            report = process_manager.cleanup(state.task_id)
            if hasattr(state, "record_process_cleanup"):
                state.record_process_cleanup(report)
            if not report.complete:
                cli_notice(report.render())
            elif delegation_clean and clean_shutdown and session_id is not None and not persistence_halted:
                save_session("clean")


if __name__ == "__main__":
    main()
