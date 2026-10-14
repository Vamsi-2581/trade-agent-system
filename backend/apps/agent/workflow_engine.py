"""Workflow execution engine extracted from SupervisorAgent.

Pure orchestration of multi-step workflows: step sequencing, goal-driven
loops, retry, condition evaluation, and persistence. Dependencies
(``LLMClient``, a ``route_to_agent`` callable) are injected so the engine
remains testable in isolation from the Supervisor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Awaitable

from .base import AgentMessage, AgentResult
from .llm_client import is_fallback

logger = logging.getLogger(__name__)
_wf_logger = logging.getLogger(f"{__name__}.workflow")

PAUSE_TTL = 300  # 5 minutes

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def _looks_like_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _wf_log(level: str, wf_id: str, msg: str, **kw):
    """Structured workflow log with workflow_id prefix."""
    prefix = f"[WF:{wf_id}]"
    extra = " | ".join(f"{k}={v}" for k, v in kw.items())
    full = f"{prefix} {msg}" + (f" | {extra}" if extra else "")
    # ``logging.Logger.warn`` is deprecated; translate to ``warning``.
    if level == "warn":
        level = "warning"
    getattr(_wf_logger, level, _wf_logger.info)(full)


# ------------------------------------------------------------------ #
#  工作流类型定义                                                       #
# ------------------------------------------------------------------ #


class WorkflowStep(dict):
    """工作流步骤定义

    普通步骤字段:
        agent: str — 目标 Agent 名称
        message: str — 发送给该 Agent 的消息
        condition: str | None — 条件表达式
        on_failure: str | None — 失败策略: "retry" / "skip" / "abort"
        max_retries: int — 最大重试次数（默认 0）
        retry_delay: float — 重试间隔秒数（默认 1.0）
        timeout_seconds: int — 步骤超时秒数（默认 300）
        interrupt: bool — 是否需要人工确认（默认 False）

    循环块步骤字段 (type="loop"):
        type: "loop" — 标记为循环块
        goal: str — 自然语言目标描述（必填）
        goal_condition: str | None — 可选确定性条件表达式
        max_iterations: int — 最大迭代次数（默认 3，硬上限 10）
        steps: list[WorkflowStep] — 循环体内部步骤（不支持 interrupt / 嵌套 loop）
    """


class WorkflowContext(dict):
    """工作流执行上下文

    字段:
        workflow_id: str — 唯一工作流 ID
        summary: str — 工作流摘要
        variables: dict — 用户/步骤定义的变量槽
        step_results: dict[int, dict] — 步骤执行结果 {step_idx: result}
        metadata: dict — 执行元数据（创建时间、耗时等）
    """

    @classmethod
    def create(cls, summary: str, variables: dict | None = None) -> "WorkflowContext":
        return cls(
            {
                "workflow_id": str(uuid.uuid4())[:8],
                "summary": summary,
                "variables": variables or {},
                "step_results": {},
                "metadata": {
                    "created_at": time.time(),
                    "total_steps": 0,
                    "completed_steps": 0,
                },
            }
        )


# Type alias for the route_to_agent callback used by the engine.
RouteToAgentFn = Callable[[str, AgentMessage, Any], Awaitable[AgentResult]]


class WorkflowEngine:
    """Multi-step workflow execution engine.

    Dependencies are injected via the constructor so the engine can be
    unit-tested without a real SupervisorAgent.

    Args:
        llm_client: ``LLMClient`` instance used for goal evaluation.
        route_to_agent: async callable ``(agent_name, message, on_tool_result) -> AgentResult``.
        agent_name: name used for log prefixing (typically "supervisor").
        get_session_manager: callable returning the session manager. Injected
            so tests can patch it at the Supervisor module boundary.
    """

    _LOOP_MAX_ITERATIONS_HARD = 10
    _LOOP_JUDGE_MAX_FAILURES = 2

    def __init__(
        self,
        llm_client,
        route_to_agent: RouteToAgentFn,
        agent_name: str = "supervisor",
        get_session_manager=None,
    ):
        self._llm = llm_client
        self._route_to_agent = route_to_agent
        self._agent_name = agent_name
        # Lazy default — resolved on first call so tests that patch the
        # supervisor module's ``get_session_manager`` keep working.
        self._get_session_manager = get_session_manager

    def _resolve_session_manager(self):
        """Resolve the session manager, defaulting to the global getter."""
        if self._get_session_manager is None:
            from apps.agent import supervisor as _supervisor

            self._get_session_manager = _supervisor.get_session_manager
        return self._get_session_manager()

    # ------------------------------------------------------------------ #
    #  Public entry point                                                  #
    # ------------------------------------------------------------------ #

    async def execute_workflow(
        self,
        workflow_plan: dict,
        message: AgentMessage,
        on_tool_result=None,
        on_workflow_complete=None,
    ) -> AgentResult:
        """Execute a multi-step workflow with conditions, retries, and timeouts.

        Args:
            workflow_plan: {"summary": str, "steps": [WorkflowStep], "variables": dict}
            message: Original AgentMessage (for task_id, user_id)
            on_tool_result: Optional callback for tool results
            on_workflow_complete: Optional callback(ctx, step_results, message, status, error)
                                  for persistence/notification. Called on completion, failure, or abort.

        Returns:
            AgentResult with aggregated step results
        """
        from apps.agent.session_manager import SessionState

        steps = workflow_plan.get("steps", [])
        if not steps:
            return AgentResult(
                task_id=message.task_id,
                success=False,
                error="工作流计划为空",
            )

        ctx = WorkflowContext.create(
            summary=workflow_plan.get("summary", ""),
            variables=workflow_plan.get("variables"),
        )
        ctx["metadata"]["total_steps"] = len(steps)

        session_mgr = self._resolve_session_manager()
        if message.user_id:
            await session_mgr.set_session_context(
                message.user_id,
                SessionState.WORKFLOW_RUNNING,
                active_agent="workflow",
                task_id=ctx["workflow_id"],
                ttl=workflow_plan.get("ttl", 3600),
            )

        _wf_log(
            "info",
            ctx["workflow_id"],
            "START",
            summary=ctx["summary"],
            steps=len(steps),
            user=message.user_id,
        )

        step_results: list[dict] = []

        try:
            failed_result = await self._execute_steps(
                steps,
                ctx,
                message,
                on_tool_result,
                step_results,
            )
            if failed_result is not None:
                if on_workflow_complete:
                    await on_workflow_complete(ctx, step_results, message, "failed", "")
                return failed_result

            result = self._workflow_completed(ctx, step_results, steps, message)
            elapsed = round(time.time() - ctx["metadata"]["created_at"], 2)
            _wf_log(
                "info",
                ctx["workflow_id"],
                "COMPLETED",
                steps=len(steps),
                elapsed=elapsed,
            )
            if on_workflow_complete:
                await on_workflow_complete(ctx, step_results, message, "completed", "")
            return result

        except Exception as e:
            elapsed = round(time.time() - ctx["metadata"]["created_at"], 2)
            _wf_log(
                "error",
                ctx["workflow_id"],
                "ABORTED",
                completed_steps=len(step_results),
                total=len(steps),
                error=str(e),
                elapsed=elapsed,
            )
            result = AgentResult(
                task_id=message.task_id,
                success=False,
                error=f"工作流异常: {str(e)}",
                data={
                    "workflow_id": ctx["workflow_id"],
                    "completed_steps": step_results,
                },
            )
            if on_workflow_complete:
                await on_workflow_complete(
                    ctx, step_results, message, "aborted", str(e)
                )
            return result
        finally:
            if message.user_id:
                current = await session_mgr.get_session_context(message.user_id)
                if (
                    current
                    and current.get("state") == SessionState.WORKFLOW_RUNNING.value
                ):
                    await session_mgr.clear_session_context(message.user_id)

    # ------------------------------------------------------------------ #
    #  Step sequencing                                                     #
    # ------------------------------------------------------------------ #

    async def _execute_steps(
        self,
        steps: list[dict],
        ctx: WorkflowContext,
        message: AgentMessage,
        on_tool_result,
        step_results: list[dict],
    ) -> AgentResult | None:
        """Execute a sequence of workflow steps.

        Returns None if all steps succeeded; AgentResult if a step failed with
        abort policy.
        """
        from apps.agent.task_tracker import tracker_context

        session_mgr = self._resolve_session_manager()
        base_idx = ctx["metadata"].get("completed_steps", 0)

        for i, step in enumerate(steps):
            idx = base_idx + i

            # 检查取消信号
            tracker = tracker_context.get(None)
            if tracker and tracker.is_cancelled():
                _wf_log(
                    "info",
                    ctx["workflow_id"],
                    "workflow cancelled by user at step %d",
                    idx + 1,
                )
                return AgentResult(
                    task_id=message.task_id,
                    success=False,
                    error="工作流已被用户取消",
                    data={
                        "workflow_id": ctx["workflow_id"],
                        "completed_steps": step_results,
                        "cancelled": True,
                    },
                )

            # Dispatch loop block
            if step.get("type") == "loop":
                inner = step.get("steps", [])
                for s in inner:
                    if s.get("type") == "loop":
                        return AgentResult(
                            task_id=message.task_id,
                            success=False,
                            error="不支持嵌套 goal 循环",
                        )
                loop_result = await self._execute_goal_loop(
                    step,
                    ctx,
                    message,
                    on_tool_result,
                )
                step_results.append(
                    {
                        "type": "loop",
                        "step": idx + 1,
                        **loop_result,
                    }
                )
                ctx["metadata"]["completed_steps"] = idx + 1
                if "error" in loop_result:
                    return AgentResult(
                        task_id=message.task_id,
                        success=False,
                        error=loop_result["error"],
                        data={
                            "workflow_id": ctx["workflow_id"],
                            "completed_steps": step_results,
                        },
                    )
                continue

            agent_name = step.get("agent", "")
            step_message = step.get("message", "")
            condition = step.get("condition")
            on_failure = step.get("on_failure", "abort")
            max_retries = step.get("max_retries", 0)
            retry_delay = step.get("retry_delay", 1.0)
            interrupt = step.get("interrupt", False)

            if not agent_name:
                return AgentResult(
                    task_id=message.task_id,
                    success=False,
                    error=f"步骤 {idx + 1} 缺少 agent 字段",
                )

            # Evaluate condition
            if condition and not self.evaluate_condition(ctx, condition):
                _wf_log(
                    "info",
                    ctx["workflow_id"],
                    "STEP_SKIPPED",
                    step=idx + 1,
                    agent=agent_name,
                    reason=condition,
                )
                step_results.append(
                    {
                        "agent": agent_name,
                        "data": "(条件不满足，跳过)",
                        "step": idx + 1,
                        "skipped": True,
                    }
                )
                continue

            # Human interrupt point
            if interrupt:
                _wf_log(
                    "info",
                    ctx["workflow_id"],
                    "STEP_PAUSE",
                    step=idx + 1,
                    agent=agent_name,
                    message=step_message[:100]
                    + ("..." if len(step_message) > 100 else ""),
                )
                await session_mgr.pause_session(
                    message.user_id,
                    active_agent="workflow",
                    pause_ttl=PAUSE_TTL,
                    pause_context=f"工作流 {ctx['workflow_id']} 步骤 {idx + 1} 等待确认: {step_message}",
                )
                return AgentResult(
                    task_id=message.task_id,
                    success=True,
                    data={
                        "workflow_paused": True,
                        "workflow_id": ctx["workflow_id"],
                        "pending_step": idx + 1,
                        "message": step_message,
                    },
                )

            # Execute with retry
            _wf_log(
                "info",
                ctx["workflow_id"],
                "STEP_START",
                step=idx + 1,
                total=len(steps),
                agent=agent_name,
                message=step_message[:100] + ("..." if len(step_message) > 100 else ""),
                retries=max_retries,
            )
            step_start = time.time()
            result = await self._execute_step_with_retry(
                agent_name,
                step_message,
                ctx,
                message,
                on_tool_result,
                max_retries,
                retry_delay,
            )
            step_elapsed = round(time.time() - step_start, 2)

            if result.success:
                content = self._extract_content(result.data)
                _wf_log(
                    "info",
                    ctx["workflow_id"],
                    "STEP_OK",
                    step=idx + 1,
                    agent=agent_name,
                    elapsed=step_elapsed,
                )
                step_results.append(
                    {
                        "agent": agent_name,
                        "data": content,
                        "step": idx + 1,
                    }
                )
                ctx["step_results"][idx] = {"agent": agent_name, "data": content}
                ctx["variables"][f"step_{idx + 1}_result"] = content
                ctx["metadata"]["completed_steps"] = idx + 1
                # Extract metrics dict into variables for goal_condition
                if isinstance(result.data, dict) and "metrics" in result.data:
                    for mk, mv in result.data["metrics"].items():
                        ctx["variables"][mk] = mv
            else:
                if on_failure == "skip":
                    _wf_log(
                        "warn",
                        ctx["workflow_id"],
                        "STEP_FAIL",
                        step=idx + 1,
                        agent=agent_name,
                        error=result.error,
                        action="skip",
                        elapsed=step_elapsed,
                    )
                    step_results.append(
                        {
                            "agent": agent_name,
                            "data": f"(失败，跳过: {result.error})",
                            "step": idx + 1,
                            "failed": True,
                        }
                    )
                else:
                    return self._workflow_failed(
                        ctx,
                        idx,
                        agent_name,
                        result,
                        step_results,
                        message,
                    )

        return None

    # ------------------------------------------------------------------ #
    #  Goal Loop                                                           #
    # ------------------------------------------------------------------ #

    async def _execute_goal_loop(
        self,
        loop_step: dict,
        ctx: WorkflowContext,
        message: AgentMessage,
        on_tool_result,
    ) -> dict:
        """Execute a goal-driven loop block.

        Returns dict with keys: goal, achieved, iterations, best_iteration.
        """
        goal = loop_step.get("goal", "")
        max_iter = min(
            loop_step.get("max_iterations", 3),
            self._LOOP_MAX_ITERATIONS_HARD,
        )
        inner_steps = loop_step.get("steps", [])
        ctx.setdefault("iterations", [])

        _wf_log("info", ctx["workflow_id"], "LOOP_START", goal=goal, max_iter=max_iter)

        best_score: float | None = None
        best_iter = 0
        judge_fail_count = 0
        iterations_log: list[dict] = []
        achieved = False

        for iteration in range(1, max_iter + 1):
            ctx["variables"]["loop_iteration"] = iteration
            ctx["variables"]["goal"] = goal
            if iteration == 1:
                ctx["variables"]["last_feedback"] = ""

            # Render inner step messages with placeholders
            rendered_steps = []
            for s in inner_steps:
                rendered = dict(s)
                rendered["message"] = self.render_loop_message(
                    s.get("message", ""),
                    ctx["variables"],
                )
                rendered_steps.append(rendered)

            iter_results: list[dict] = []
            failed = await self._execute_steps(
                rendered_steps,
                ctx,
                message,
                on_tool_result,
                iter_results,
            )

            if failed is not None:
                _wf_log(
                    "error",
                    ctx["workflow_id"],
                    "LOOP_ABORT",
                    iter=iteration,
                    error=failed.error,
                )
                iterations_log.append(
                    {
                        "iter": iteration,
                        "steps": iter_results,
                        "aborted": True,
                        "error": failed.error,
                    }
                )
                ctx["iterations"] = iterations_log
                return {
                    "goal": goal,
                    "achieved": False,
                    "iterations": iterations_log,
                    "best_iteration": best_iter,
                    "error": failed.error,
                }

            eval_result = await self._evaluate_goal(loop_step, ctx, iter_results)
            score = eval_result.get("score")
            feedback = eval_result.get("feedback", "")
            judge_ok = eval_result.get("judge_ok", True)

            if not judge_ok:
                judge_fail_count += 1
                if judge_fail_count >= self._LOOP_JUDGE_MAX_FAILURES:
                    _wf_log(
                        "warn",
                        ctx["workflow_id"],
                        "LOOP_JUDGE_FAILED",
                        iter=iteration,
                        consecutive=judge_fail_count,
                    )
                    iterations_log.append(
                        {
                            "iter": iteration,
                            "steps": iter_results,
                            "judge_failed": True,
                        }
                    )
                    ctx["iterations"] = iterations_log
                    return {
                        "goal": goal,
                        "achieved": False,
                        "iterations": iterations_log,
                        "best_iteration": best_iter,
                        "error": "目标判定连续失败，终止循环",
                    }
            else:
                judge_fail_count = 0

            if score is not None and (best_score is None or score > best_score):
                best_score = score
                best_iter = iteration
                ctx["variables"]["best_score"] = best_score
                ctx["variables"]["best_iteration"] = best_iter

            ctx["variables"]["last_feedback"] = feedback
            if score is not None:
                ctx["variables"]["last_score"] = score

            iterations_log.append(
                {
                    "iter": iteration,
                    "steps": iter_results,
                    "achieved": eval_result.get("achieved", False),
                    "score": score,
                    "feedback": feedback,
                }
            )

            _wf_log(
                "info",
                ctx["workflow_id"],
                "LOOP_ITER",
                iter=iteration,
                max=max_iter,
                achieved=eval_result.get("achieved"),
                score=score,
            )

            # Push progress notification via TaskTracker
            from apps.agent.task_tracker import TaskTracker

            tracker = TaskTracker.get_current()
            if tracker:
                score_text = f"，得分 {score}" if score is not None else ""
                status_text = (
                    "达成目标 ✅" if eval_result.get("achieved") else "未达标，继续迭代"
                )
                tracker.milestone(
                    f"循环迭代 {iteration}/{max_iter}{score_text}：{status_text}",
                    progress=iteration / max_iter,
                )

            if eval_result.get("achieved"):
                achieved = True
                break

        ctx["iterations"] = iterations_log
        _wf_log(
            "info",
            ctx["workflow_id"],
            "LOOP_DONE",
            achieved=achieved,
            iterations=len(iterations_log),
            best=best_iter,
        )

        return {
            "goal": goal,
            "achieved": achieved,
            "iterations": iterations_log,
            "best_iteration": best_iter,
        }

    async def _evaluate_goal(
        self,
        loop_step: dict,
        ctx: WorkflowContext,
        iter_results: list[dict],
    ) -> dict:
        """Evaluate whether the loop goal is achieved.

        Hybrid strategy:
        1. Deterministic: goal_condition expression (preferred)
        2. LLM judge: natural language goal + step outputs → JSON verdict

        Returns:
            {"achieved": bool, "score": float|None, "feedback": str, "judge_ok": bool}
        """
        goal_condition = loop_step.get("goal_condition")

        # Strategy 1: deterministic condition
        if goal_condition:
            try:
                cond_result = self.evaluate_condition(ctx, goal_condition)
                return {
                    "achieved": bool(cond_result),
                    "score": None,
                    "feedback": "",
                    "judge_ok": True,
                }
            except Exception:
                pass  # fall through to LLM judge

        # Strategy 2: LLM judge
        goal = loop_step.get("goal", "")
        step_summaries = "\n".join(
            f"[{r.get('agent', 'step')}] {str(r.get('data', ''))[:500]}"
            for r in iter_results
        )
        judge_prompt = (
            f"Goal: {goal}\n\n"
            f"Current iteration results:\n{step_summaries}\n\n"
            "Evaluate whether the goal is achieved. Reply with JSON only:\n"
            '{"achieved": true/false, "score": 0.0-1.0, "feedback": "improvement suggestion"}\n'
            "If the goal is clearly achieved, set achieved=true. "
            "Otherwise provide specific feedback on what to improve."
        )

        try:
            response = await self._llm.chat(
                system="You are a goal evaluation judge. Be strict and objective.",
                user=judge_prompt,
                max_tokens=256,
                temperature=0.1,
            )
            if is_fallback(response):
                return {
                    "achieved": False,
                    "score": None,
                    "feedback": "",
                    "judge_ok": False,
                }

            text = response.strip()
            fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if fence:
                text = fence.group(1).strip()
            else:
                obj = re.search(r"\{[\s\S]*\}", text)
                if obj:
                    text = obj.group(0).strip()
            verdict = json.loads(text)
            return {
                "achieved": bool(verdict.get("achieved", False)),
                "score": float(verdict.get("score", 0)),
                "feedback": str(verdict.get("feedback", "")),
                "judge_ok": True,
            }
        except Exception as e:
            logger.warning("[%s] Goal judge parse failed: %s", self._agent_name, e)
            return {"achieved": False, "score": None, "feedback": "", "judge_ok": False}

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def render_loop_message(self, template: str, variables: dict) -> str:
        """Render step message with loop variable placeholders.

        Missing keys are left as-is (no KeyError).
        """

        class _SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"

        try:
            return template.format_map(_SafeDict(variables))
        except Exception:
            return template

    async def _execute_step_with_retry(
        self,
        agent_name,
        step_message,
        ctx,
        original_message,
        on_tool_result,
        max_retries,
        retry_delay,
    ) -> AgentResult:
        """Execute a single workflow step with retry logic."""
        last_error = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                _wf_log(
                    "warn",
                    ctx["workflow_id"],
                    "STEP_RETRY",
                    step=agent_name,
                    attempt=attempt,
                    max=max_retries,
                )
                await asyncio.sleep(retry_delay)

            step_msg = AgentMessage(
                task_id=original_message.task_id,
                sender="supervisor",
                recipient=agent_name,
                payload={"text": step_message, "scheduled": True},
                user_id=original_message.user_id,
            )

            # Inject workflow context
            step_msg.payload["workflow_id"] = ctx["workflow_id"]
            step_msg.payload["workflow_variables"] = ctx["variables"]
            step_msg.payload["workflow_step_results"] = ctx["step_results"]

            # Inject previous step's result (backward compatible)
            if ctx["step_results"]:
                last_idx = max(ctx["step_results"].keys())
                prev = ctx["step_results"][last_idx]
                step_msg.payload["previous_agent_response"] = prev.get("data", "")
                step_msg.payload["previous_agent_name"] = prev.get("agent", "")

            result = await self._route_to_agent(
                agent_name,
                step_msg,
                on_tool_result=on_tool_result,
            )
            if result.success:
                return result
            last_error = result

        return last_error or AgentResult(
            task_id=original_message.task_id,
            success=False,
            error=f"步骤 {agent_name} 执行失败",
        )

    def evaluate_condition(self, ctx: WorkflowContext, condition: str) -> bool:
        """Evaluate a condition string against the workflow context."""
        safe_globals = {"__builtins__": {}}
        safe_locals = {
            "step_results": ctx["step_results"],
            "variables": ctx["variables"],
            "len": len,
            "str": str,
            "int": int,
            "float": float,
        }
        try:
            return bool(eval(condition, safe_globals, safe_locals))
        except Exception as e:
            logger.warning(
                "[%s] Condition eval failed '%s': %s",
                self._agent_name,
                condition,
                e,
            )
            return True

    def _extract_content(self, data: Any) -> str:
        """Extract string content from step result data."""
        if isinstance(data, dict):
            return data.get("content", str(data))
        return str(data)

    def _workflow_failed(
        self,
        ctx,
        failed_idx,
        agent_name,
        result,
        step_results,
        message,
    ):
        """Handle workflow failure."""
        elapsed = round(time.time() - ctx["metadata"]["created_at"], 2)
        _wf_log(
            "error",
            ctx["workflow_id"],
            "FAILED",
            step=failed_idx + 1,
            agent=agent_name,
            error=result.error,
            elapsed=elapsed,
        )
        return AgentResult(
            task_id=message.task_id,
            success=False,
            error=f"工作流步骤 {failed_idx + 1} ({agent_name}) 失败: {result.error}",
            data={"workflow_id": ctx["workflow_id"], "completed_steps": step_results},
        )

    def _workflow_completed(self, ctx, step_results, steps, message):
        """Handle workflow completion."""
        ctx["metadata"]["completed_at"] = time.time()
        elapsed = ctx["metadata"]["completed_at"] - ctx["metadata"]["created_at"]
        ctx["metadata"]["elapsed_seconds"] = elapsed

        _wf_log(
            "info",
            ctx["workflow_id"],
            "ALL_STEPS_DONE",
            completed=len(step_results),
            total=len(steps),
            elapsed=round(elapsed, 2),
        )
        logger.info(
            "[%s] Workflow %s completed in %.1fs (%d/%d steps)",
            self._agent_name,
            ctx["workflow_id"],
            elapsed,
            len(step_results),
            len(steps),
        )

        summary_lines = [
            f"工作流完成 ({len(step_results)}/{len(steps)} 步, {elapsed:.1f}s)",
        ]
        for sr in step_results:
            if sr.get("type") == "loop":
                achieved_text = "✅ 达成" if sr.get("achieved") else "❌ 未达标"
                iterations = sr.get("iterations", [])
                summary_lines.append(
                    f"\n--- 步骤 {sr['step']}: [循环] {sr.get('goal', '')} {achieved_text} ---\n"
                    f"迭代 {len(iterations)} 轮"
                )
            else:
                status = (
                    "(跳过)"
                    if sr.get("skipped")
                    else "(失败)"
                    if sr.get("failed")
                    else ""
                )
                data_str = str(sr.get("data", ""))[:500]
                summary_lines.append(
                    f"\n--- 步骤 {sr['step']}: {sr['agent']} {status} ---\n{data_str}"
                )

        return AgentResult(
            task_id=message.task_id,
            success=True,
            data={
                "workflow_id": ctx["workflow_id"],
                "workflow_summary": "\n".join(summary_lines),
                "step_results": step_results,
                "elapsed_seconds": elapsed,
            },
        )
