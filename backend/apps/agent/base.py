from __future__ import annotations
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
import uuid


# 延迟导入避免循环依赖
def _get_tool_registry():
    from apps.agent.tools.base import ToolRegistry

    return ToolRegistry


logger = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sender: str = ""  # agent type or 'user'
    recipient: str = ""  # target agent type
    intent: str = ""
    payload: dict = field(default_factory=dict)
    timeout_ms: int = 30000
    user_id: str = "anonymous"
    origin: str = (
        ""  # 下达命令的 gateway: web | api | lark | telegram | tui | scheduler | ""
    )


@dataclass
class AgentResult:
    task_id: str = ""
    success: bool = True
    data: Any = None
    error: str = ""
    token_used: int = 0
    duration_ms: int = 0
    need_reroute: bool = False
    reroute_reason: str = ""
    reroute_suggestion: str = ""


class BaseAgent(ABC):
    """所有Agent的抽象基类（自研，无第三方框架依赖）"""

    name: str = "base"
    # 子类可覆盖：声明该 Agent 可用的工具名称列表
    _agent_tools: list[str] = []
    # 工具调用最大循环轮数
    _max_tool_rounds: int = 32
    # 未验证任务声明的最大纠错轮数（见 _run_tool_loop 防虚构守卫）
    _max_claim_corrections: int = 2

    # LLM 答复中出现这些特征，说明它在声称“任务已提交 / 结果已产出”，
    # 这类声明必须有真实工具调用（submit_backtest / get_task_result 等）佐证。
    _TASK_CLAIM_PATTERNS = re.compile(
        r"(已提交|提交成功|回测(?:任务|报告)|回测已完成|已安排.{0,16}查询|"
        r"查询.{0,8}结果如下|任务\s*已)"
    )
    # 用户请求必须是任务型指令时才启用防虚构守卫，避免误伤普通问答。
    _TASK_REQUEST_KEYWORDS = re.compile(
        r"(回测|backtest|查询|提交|结果|任务|task)",
        re.IGNORECASE,
    )
    _UNVERIFIED_CLAIM_CORRECTION = (
        "【系统校验】你刚才的回答声称已提交回测任务或已产出回测结果，"
        "但这一轮没有调用任何工具。任务 ID 只能来自 submit_backtest 的实际返回值，"
        "结果只能来自 get_task_result 的实际返回值——绝不能虚构。"
        "请立即调用对应工具获取真实数据后再回答；如果工具调用失败，请如实报告错误。"
    )

    # “声称已写入”的文件声明模式（2026-09-18 虚构事故：summary 声称
    # `rsi_macd_short_strategy.py` 已写入，实际文件不存在）。
    # 两个方向：`xxx.py` 已写入 / 已写入 `xxx.py`
    _WRITTEN_FILE_CLAIMS = (
        re.compile(r"([~\w][\w./\-]*\.py)`?\s*(?:已写入|已生成|已保存|已创建|已落盘)"),
        re.compile(r"(?:已写入|已生成|已保存|已创建|已落盘)\s*`?([~\w][\w./\-]*\.py)"),
    )

    # summary 轮成功声明（比 _TASK_CLAIM_PATTERNS 更宽：09-18 事故报告的
    # “回测完成 / 策略创建成功 / 夏普比率 / task_id: xxx”均不在 in-loop 守卫模式里）。
    # 与磁盘文件验证联合使用：命中成功声明 + 声称写入的文件不存在 → 拦截。
    _SUMMARY_SUCCESS_CLAIM = re.compile(
        r"(已提交|提交成功|回测(?:任务|报告|完成|已完成)|策略(?:创建|生成).{0,6}成功|"
        r"回测结果(?:如下)?|夏普比率|胜率|task_id\s*[:：]\s*[\w\-]{6,})"
    )

    # 指标锚定硬规则（2026-09-18 事故：定时任务 agent 在 get_task_result
    # 返回真值后，输出中全部指标被虚构——26 笔 vs 真实 9 笔、-12.34% vs
    # 真实 -0.56%）。锚定块与硬规则同时注入工具结果旁与 summary prompt。
    _METRIC_ANCHOR_RULE = (
        "硬规则：报告中出现的任何数字（交易次数/夏普/胜率/收益率/回撤/盈亏比/权益等）"
        "必须逐字取自上述复算值（或由其四舍五入）；严禁编造、估算或引用与上述不一致的数字；"
        "某指标若不在上述中，写“未提供”。"
    )

    # get_task_result 返回值中的关键指标白名单（注入锚定块用）
    _TASK_METRIC_KEYS = (
        "total_trades",
        "sharpe_ratio",
        "win_rate",
        "total_return_pct",
        "max_drawdown_pct",
        # 期末权益的两个来源口径（见 _METRIC_CLAIM_PATTERNS 同名注释）：
        # Celery 归档给 final_equity，BacktestResult 给 final_capital。
        # 两个都必须在白名单里，否则锚定块不会带上它、溯源校验永远判不可核验。
        "final_equity",
        "final_capital",
        # 盈亏比的两个口径（见 _METRIC_CLAIM_PATTERNS 同名注释）：
        # `profit_factor` 是总额口径（总盈利/总亏损），`payoff_ratio` 是单笔口径
        # （平均盈利/平均亏损）。两者都叫「盈亏比」，都必须在白名单里——
        # 只收一个，另一个口径的正确报告就会因「锚定值不在正文里」被误拦。
        "profit_factor",
        "payoff_ratio",
        "result_id",
    )

    # ── 指标溯源（2026-10-19 事故）────────────────────────────────────────
    # 事故：定时任务 1c05a48b 把一份**从未调用 get_task_result** 的回测报告
    # 用 notify_user 推给了用户。库里真值 820 笔 / -89.86% / 夏普 -20.20 /
    # 胜率 13.41%，推出去的是 1,102 笔 / -3.37% / -0.37 / 46.83%。
    #
    # 为什么既有守卫没拦住：`_looks_like_unverified_task_claim` 以
    # `had_tool_results` 为闸门——只要**任何**工具执行过就放行。notify_user
    # 不是指标来源，却把闸门打开了，于是「先 notify_user 再编数字」绕过守卫；
    # 而锚定块只在 get_task_result 真执行时才注入，没执行就完全没有锚定。
    #
    # 判据因此改为「**这条工具调用是否在向用户断言指标**」，而不是「本轮是否
    # 调用过工具」：向用户断言指标的调用，其数字必须能在本轮 get_task_result
    # 的返回值里找到（允许四舍五入）。找不到就是虚构。
    _METRIC_ASSERTION_TOOLS = frozenset({"notify_user"})

    # 指标断言识别：关键词 → 锚定键元组（空元组表示「没有对应锚定键」）。
    #
    # 2026-10-20 变更：盈亏比不再是「无锚定键」的衍生指标。引擎开始算
    # `profit_factor` / `payoff_ratio` 并透出到 `get_task_result` 之后，它就有了
    # 真值可比——原先「不参与判定」的豁免变成了一条**虚构后门**：报告写
    # 「盈亏比 0.96」（事故里那个数）而返回值里没有、或对不上，守卫都会放行。
    # 现在两个口径都认（总额口径 profit_factor / 单笔口径 payoff_ratio），
    # 与期末权益认两个来源口径是同一条理由。
    _METRIC_CLAIM_PATTERNS = (
        (re.compile(r"总收益率|收益率|回报率"), ("total_return_pct",)),
        (re.compile(r"夏普"), ("sharpe_ratio",)),
        (re.compile(r"胜率"), ("win_rate",)),
        (re.compile(r"最大回撤|回撤"), ("max_drawdown_pct",)),
        (re.compile(r"交易次数|交易笔数|总交易|笔数"), ("total_trades",)),
        # 期末权益有两个来源口径，必须都认（2026-10-19 线上复验发现的误拦）：
        #   * `task_progress` 归档路径 → `final_equity`（Celery result dict）
        #   * `BacktestResult` 路径     → `final_capital`（`backtest.py:563`）
        # 只认前者会让走 BacktestResult 的报告永远匹配不上「期末权益」，
        # 于是**完全正确**的报告被误拦。
        (
            re.compile(r"期末权益|最终权益|期末资金|最终资金|期末余额"),
            ("final_equity", "final_capital"),
        ),
        # 盈亏比的两个口径，必须都认：总额口径与单笔口径在同一份报告里
        # 可能是不同的数（0.96 vs 1.4），只认一个就会把另一个口径的正确报告拦掉。
        (
            re.compile(r"盈亏比|profit_factor|profit factor|payoff_ratio"),
            ("profit_factor", "payoff_ratio"),
        ),
    )
    # 关键词之后取数窗口：够拿到紧邻的那个数字，又窄到不会把同一行的
    # 其它内容算进来。只向后取——指标值总在关键词之后（`夏普比率：-0.37`）。
    _METRIC_CLAIM_WINDOW_AFTER = 28
    _NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

    _METRIC_PROVENANCE_ERROR = (
        "Error: 指标溯源校验未通过 —— 本次通知正文包含回测指标，但本轮没有"
        "任何可核验的 get_task_result 返回值支撑这些数字（或数字与返回值不一致）。"
        "报告中的每个指标都必须逐字取自 get_task_result 的返回值（可四舍五入），"
        "缺失的指标写「未提供」。请先调用 get_task_result 拿到真实结果，"
        "再按其返回值重写通知正文；严禁编造或估算任何数字。"
    )

    # 同一工具连续失败达到该阈值后熔断：后续对该工具的调用不再真实执行，
    # 直接返回禁用通知（2026-10-14：web_search 因代理 DNS 失败被 LLM 反复
    # 重试，烧满 _max_tool_rounds=32 轮，无法给出结果）。
    _TOOL_FAILURE_DISABLE_THRESHOLD = 3
    _TOOL_DISABLED_NOTICE = (
        "【系统通知】工具 {tool} 已连续失败 {n} 次，本次调用已被系统禁用，"
        "不会执行。不要再尝试调用它。请基于已有信息给出回答；"
        "如确需该数据，请如实说明该数据源当前不可用。"
    )

    # 工具通道（LLM function calling）彻底不可用时的对外答复：
    # 必须明确告诉用户"本次未执行"，绝不能把降级文本当成正常答复转发。
    _TOOL_CHANNEL_DOWN_REPLY = (
        "⚠️ 本次请求未执行。Agent 的 LLM 工具调用通道故障（{reason}），"
        "重试全部失败，因此没有调用任何工具、也没有提交任何任务或回测。\n"
        "请稍后重试；若持续失败，请检查 LLM provider 状态。"
    )
    # 已真实执行过工具、仅收尾这一轮降级时的补充说明
    _TOOL_CHANNEL_DEGRADED_NOTE = (
        "\n\n（注：生成此回答时 LLM 工具调用通道故障（{reason}），"
        "以上内容基于已执行的工具结果，本轮未执行新的操作。）"
    )

    def __init__(self):
        self._running = False
        self._skills_loader = None
        self._current_user_id: str = ""

    # ------------------------------------------------------------------ #
    #  工具 & 技能（公共逻辑）                                              #
    # ------------------------------------------------------------------ #

    def _get_skills_loader(self):
        """懒加载 skills_loader。"""
        if self._skills_loader is None:
            from apps.skill.loader import get_skills_loader

            self._skills_loader = get_skills_loader(self.name)
        return self._skills_loader

    def _get_tools_schema(self) -> list[dict]:
        """获取当前 Agent 可用的工具 schema。

        优先使用 _agent_tools 声明列表；未声明则回退到全局所有工具。
        """
        from apps.agent.tools.base import ToolRegistry

        if self._agent_tools:
            schemas = []
            for tool_name in self._agent_tools:
                tool = ToolRegistry.get(tool_name)
                if tool:
                    schemas.append(tool.schema)
                else:
                    logger.warning(
                        "[%s] tool %s not found in registry", self.name, tool_name
                    )
            return schemas
        return ToolRegistry.get_all_schemas()

    def _build_skills_section(self, base_prompt: str) -> str:
        """注入 always 技能 + 非 always 技能摘要到 system prompt。

        子类传入基础 prompt，返回增强后的 prompt。
        """
        loader = self._get_skills_loader()
        always_skills = loader.get_always_skills()
        if always_skills:
            skills_content = loader.load_skills_content(always_skills)
            base_prompt = f"{base_prompt}\n\n### Agent Skills\n{skills_content}"

        skill_summary = loader.build_summary()
        if skill_summary:
            base_prompt = (
                f"{base_prompt}\n\n"
                f"### Skills\n"
                f"以下技能扩展了你的能力, **能用则必须要使用**。使用 load_skill 工具加载完整内容。\n"
                f"如果用户的需求与以下技能描述相关，**一定要**使用 load_skill 工具。\n"
                f"{skill_summary}"
            )

        return base_prompt

    async def _execute_tool_call(self, tc) -> str:
        """执行单个工具调用，返回结果字符串。

        特殊处理 load_skill（传 agent_name）。
        自动注入 user_id（如果工具支持）。
        """
        try:
            # Push tool call notification
            from .task_tracker import tracker_context

            tracker = tracker_context.get(None)
            if tracker is not None:
                tracker.tool_call(tc.name, tc.arguments)

            if tc.name == "load_skill":
                from apps.agent.tools.load_skill import LoadSkillTool

                skill_name = tc.arguments.get("skill_name", "")
                tool = LoadSkillTool(agent_name=self.name)
                logger.info(
                    "[%s] executing tool: %s with args: %s",
                    self.name,
                    tc.name,
                    tc.arguments,
                )
                result = await tool.execute(skill_name=skill_name)
            else:
                # 取证日志：记录收到的参数键与 content 长度（不打印内容本身），
                # 用于诊断模型漏传/错传参数（2026-09-18 write_file 事件）
                _arg_preview = {
                    k: (f"<str {len(v)} chars>" if isinstance(v, str) else v)
                    for k, v in tc.arguments.items()
                    if k != "user_id"
                }
                logger.info(
                    "[%s] executing tool: %s args=%s",
                    self.name,
                    tc.name,
                    _arg_preview,
                )
                # 注入 user_id（如果工具支持）
                if self._current_user_id and not tc.arguments.get("user_id"):
                    tc.arguments["user_id"] = self._current_user_id
                result = await self.run_tool(tc.name, **tc.arguments)

            if result.success:
                return str(result.data)
            return f"Error: {result.error}"
        except Exception as e:
            logger.warning("[%s] tool %s failed: %s", self.name, tc.name, e)
            return f"Error: {e}"

    @classmethod
    def _asserted_metric_groups(cls, text: str):
        """抽出「正文里被**断言**了的指标组」——即关键词同行内带着数字的那些。

        只看同一行、只向后取（指标值总在关键词之后）。为什么要「同行且带数字」：
        `回测完成，报告见下` 这类句子不该算指标断言，否则守卫会误伤普通叙述。

        **不**把「关键词旁的那个数字」当成该指标的值——那是原设计的致命假设。
        2026-10-19 线上复验踩到两次：
          * 表格排版 `| 最大回撤 | -89.86% |` 里，跨列取数会拿到隔壁列的值；
          * regime 归因表 `| 震荡 | 324 | 13.89% | ... | 胜率 | ...` 里，
            「胜率」后面最近的是交易数 324，于是完全正确的报告被判虚构。
        现在只回答「这个指标被提到了吗」，值对不对交给
        `_metric_claims_are_anchored` 用「锚定值是否出现在正文」来判。
        """
        if not text:
            return
        seen: set[tuple] = set()
        for pattern, keys in cls._METRIC_CLAIM_PATTERNS:
            for m in pattern.finditer(text):
                line_end = text.find("\n", m.end())
                if line_end == -1:
                    line_end = len(text)
                tail = text[m.end() : line_end][: cls._METRIC_CLAIM_WINDOW_AFTER]
                if cls._NUMBER_RE.search(tail) and keys not in seen:
                    seen.add(keys)
                    yield keys

    @classmethod
    def _text_numbers(cls, text: str) -> list[float]:
        """正文里出现的所有数字（去掉千分位，解析失败丢弃）。"""
        out: list[float] = []
        for m in cls._NUMBER_RE.finditer(text):
            v = cls._parse_number(m.group(0))
            if v is not None:
                out.append(v)
        return out

    @classmethod
    def _claims_backtest_metrics(cls, text: str) -> bool:
        """文本是否在断言回测指标（收益率/夏普/胜率/回撤/笔数/盈亏比…）。"""
        return any(True for _ in cls._asserted_metric_groups(text))

    @classmethod
    def _metric_claims_are_anchored(cls, text: str, metrics: dict | None) -> bool:
        """正文断言的指标，能否在本轮 `metrics`（get_task_result 返回值）里对上。

        判据（2026-10-19 第二次修正）：**该指标的锚定值必须出现在正文里**，
        而不是「关键词旁边那个数字等于锚定值」。

        原判据在真实报告上误拦过两次，都是因为它在猜「这个数字属于哪个指标」：
        表格跨列、regime 归因表（多行同名列）都会猜错。而「真值有没有出现在
        报告里」不需要猜——事故里那份虚构报告写的是 1,102 / -3.37 / -0.37 /
        46.83 / -10.20，真值 820 / -89.86 / -20.20 / 13.41 一个都没出现，
        照样能被抓住。

        `metrics` 为空 = 本轮根本没查过结果 → 只要断言了指标就判不可锚定
        （事故形态 A：压根没调 get_task_result 就发了报告）。

        `盈亏比`/`profit_factor`（2026-10-20 起）**参与**判定：引擎算出的
        `profit_factor` / `payoff_ratio` 已经进了 `get_task_result` 的返回值，
        它不再是无从核验的衍生指标。此前这里对空元组直接 `continue`，等于给
        报告里的盈亏比开了一条「随便写」的后门——事故里那个虚构的 0.96 正是
        这么漏过去的。现在 `_asserted_metric_groups` 不再产出空元组（每组都有
        锚定键），因此这条豁免连同它的分支一起删掉。

        「有锚定键但本轮没取到该键」仍是 `continue`（不是拦截）：例如老回测行
        的 `metrics` 里没有 `profit_factor`。这与「口径对不上」不同——那条是
        明确的虚构证据，这条只是「这次没测到」。但**没有亏损笔时引擎给 None、
        于是整键缺失**，此时报告若写了盈亏比数字，就会落在「没取到」这一支上
        被放行——这是已知且刻意的取舍：宁可漏拦一个无法核验的衍生指标，也不
        要把一份核心指标全对的报告整份拦掉（2026-10-19 已经因误拦翻过一次车）。
        """
        anchors = cls._anchor_values(metrics)
        if not anchors:
            # 没有可核验的返回值：断言即违规
            return not cls._claims_backtest_metrics(text)
        text_numbers = cls._text_numbers(text)
        for keys in cls._asserted_metric_groups(text):
            candidates = [anchors[k] for k in keys if anchors.get(k) is not None]
            if not candidates:
                continue
            if not any(
                cls._numbers_agree(t, a) for t in text_numbers for a in candidates
            ):
                return False
        return True

    @classmethod
    def _unanchored_metric_claims(cls, text: str, metrics: dict | None) -> list:
        """列出**不可溯源**的指标断言，形如 ``[("win_rate", 期望值)]``。

        纯诊断用途：拦截时写进日志，让「为什么这份报告被拦」当场可读——
        否则只能看到一句 "no verifiable return value"，分不清是没查结果、
        还是某个字段口径没对上（2026-10-19 线上复验就踩过 `final_capital`
        vs `final_equity`，以及表格跨列取数两个坑，都误拦过正确报告）。
        """
        anchors = cls._anchor_values(metrics)
        text_numbers = cls._text_numbers(text)
        out: list[tuple[str, object]] = []
        for keys in cls._asserted_metric_groups(text):
            candidates = [anchors[k] for k in keys if anchors.get(k) is not None]
            if not candidates:
                out.append((keys[0], None))
                continue
            if not any(
                cls._numbers_agree(t, a) for t in text_numbers for a in candidates
            ):
                out.append((keys[0], candidates))
        return out

    @classmethod
    def _anchor_values(cls, metrics: dict | None) -> dict:
        """把锚定指标归一成可比较的 float（不可解析的丢弃）。"""
        if not metrics:
            return {}
        out = {}
        for k, v in metrics.items():
            if isinstance(v, bool):
                continue
            parsed = cls._parse_number(str(v))
            if parsed is not None:
                out[k] = parsed
        return out

    @staticmethod
    def _parse_number(raw: str) -> float | None:
        """`-89.86` / `1,102` / `820` → float；失败返回 None。"""
        try:
            return float(raw.replace(",", "").strip())
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _numbers_agree(claimed: float, anchor: float) -> bool:
        """两个数字是否可视为同一指标值（容纳四舍五入/百分比/小数口径）。

        三种口径都认：
        * 同量级（收益率 -89.86285 → -89.86）
        * 小数 vs 百分数（胜率 0.1341 → 13.41%）
        * 绝对差极小（0.0 附近，避免除零）
        """
        if claimed == anchor:
            return True
        for a, b in ((claimed, anchor), (claimed, anchor * 100.0)):
            if b == 0:
                if abs(a) < 0.01:
                    return True
                continue
            if abs(a - b) <= max(abs(b) * 0.005, 0.01):
                return True
        return False

    def _violates_metric_provenance(
        self, tc_name: str, arguments: dict, task_metrics: dict | None
    ) -> bool:
        """该工具调用是否在向用户断言无法溯源的指标。

        只对 `_METRIC_ASSERTION_TOOLS`（当前是 notify_user）生效——这些工具的
        参数会**直接送达用户**，是虚构指标真正的出口。其它工具（read_file 等）
        的参数不构成对外断言，不做校验。
        """
        if tc_name not in self._METRIC_ASSERTION_TOOLS:
            return False
        message = arguments.get("message") or ""
        if not isinstance(message, str) or not message:
            return False
        if not self._claims_backtest_metrics(message):
            return False
        return not self._metric_claims_are_anchored(message, task_metrics)

    @staticmethod
    def _extract_task_metrics(result_text: str) -> dict | None:
        """从 get_task_result 的返回文本（str(dict) 形式）提取关键指标。

        用于「复算值锚定」：只有 status=SUCCESS 且 result 为 dict 且含
        白名单指标字段时才返回（one_time 任务分支 result 是 str → None）。
        解析失败/非 dict/非 SUCCESS → None（宁可不锚定，不可错锚定）。
        """
        import ast

        if not result_text or result_text.startswith("Error"):
            return None
        try:
            data = ast.literal_eval(result_text)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(data, dict) or data.get("status") != "SUCCESS":
            return None
        res = data.get("result")
        if not isinstance(res, dict):
            return None
        metrics = {}
        for k in BaseAgent._TASK_METRIC_KEYS:
            v = res.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float, str)):
                metrics[k] = v
        return metrics or None

    @staticmethod
    def _looks_like_unverified_task_claim(
        content: str, messages: list[dict], had_tool_results: bool
    ) -> bool:
        """判断 LLM 答复是否属于“未经工具验证的任务声明”。

        场景：用户要求回测/查询结果，LLM 却在未调用任何工具的情况下直接输出
        “回测任务已提交，task_id=xxx”或“回测报告：...”——这类答复是虚构的
        （曾导致前端回测记录里没有任何结果的假“已完成”任务）。

        仅当同时满足以下条件才判定为需要拦截：
        1) 本轮尚未执行过任何工具（否则正常总结也会包含“回测报告”）；
        2) 答复文本命中任务声明特征（已提交/回测报告/任务已…）；
        3) 用户请求本身是任务型指令（回测/查询/提交/结果等）。
        """
        if not content or not content.strip():
            return False
        if had_tool_results:
            return False
        if not BaseAgent._TASK_CLAIM_PATTERNS.search(content):
            return False
        user_text = "\n".join(
            m.get("content", "")
            for m in messages
            if m.get("role") in ("user", "system")
        )
        return bool(BaseAgent._TASK_REQUEST_KEYWORDS.search(user_text))

    @classmethod
    def _claims_unwritten_file(cls, content: str) -> bool:
        """验证声称“已写入”的文件在磁盘上是否真实存在。

        返回 True 表示存在无法在磁盘验证的文件写入声明（疑似虚构）。
        验证位置：绝对路径按原样；相对路径依次查 WORKSPACE_ROOT（~/.tradelogx）
        与当前工作目录。只读检查，不做任何写操作。
        （2026-09-18：quant summary 虚构“rsi_macd_short_strategy.py 已写入”，
        该文件实际不存在——用磁盘验证拦截此类声明。）
        """
        from pathlib import Path

        if not content:
            return False
        for pattern in cls._WRITTEN_FILE_CLAIMS:
            for m in pattern.finditer(content):
                path = m.group(1)
                if path.startswith("/"):
                    candidates = (Path(path),)
                else:
                    try:
                        from .tools.file_io import WORKSPACE_ROOT

                        candidates = (WORKSPACE_ROOT / path, Path(path))
                    except Exception:
                        candidates = (Path(path),)
                if not any(c.is_file() for c in candidates):
                    return True
        return False

    def _try_parse_json_tool_call(
        self, content: str, tools: list[dict] | None = None
    ) -> dict | None:
        """尝试从 LLM 文本回复中解析 JSON 格式的 tool call（fallback 路径）。

        当 LLM 不支持原生 function calling 时，fallback 会让 LLM 输出
        {"tool": "<name>", "args": {...}} 格式。此方法解析这种格式并返回
        标准化的 tool call dict。

        额外支持两种文本伪 tool call（LLM 偶发把 native function call 写成
        文本块返回在 content 里，而不是原生 tool_calls 字段）：

        1. XML 格式:
           <tool_call><function=NAME><arguments>{...}</arguments></function></tool_call>
        2. DeepSeek DSML 文本格式（含全角/ASCII/退化标记变体）:
           <｜DSML｜function_calls><｜DSML｜invoke name="NAME">...
           <｜DSML｜parameter name="arg" string="true">value</｜DSML｜parameter>
           ...

        识别后转为标准 dict，让 _run_tool_loop 真正执行工具而不是直接 return
        content（否则用户会收到一段原始 XML/标记文本，任务无法继续）。
        """
        if not content or not content.strip():
            return None
        text = content.strip()
        # 去掉 markdown 代码块包裹
        if text.startswith("```"):
            idx = text.find("\n")
            text = text[idx + 1 :] if idx > 0 else text[3:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        # 提取 JSON 对象
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            import re

            m = re.search(
                r'\{[^{}]*"tool"\s*:\s*"[^"]+"\s*,\s*"args"\s*:\s*\{[^{}]*\}\s*\}',
                text,
            )
            if not m:
                # XML 伪 tool call: <tool_call>...<function=NAME>...<arguments>{...}</arguments>...</function></tool_call>
                xml = self._try_parse_xml_tool_call(text)
                if xml is not None:
                    return xml
                # DeepSeek DSML 文本伪 tool call（含标记退化变体）
                return self._try_parse_dsml_tool_call(text, tools)
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        if isinstance(data, dict) and "tool" in data and "args" in data:
            return data
        return None

    def _try_parse_xml_tool_call(self, text: str) -> dict | None:
        """Parse XML-formatted pseudo tool calls from LLM content.

        Recognizes:
            <tool_call>
              <function=NAME>
              <arguments>{JSON}</arguments>
              </function>
            </tool_call>

        Returns {"tool": NAME, "args": {...}} on success, None otherwise.
        """
        import re

        m = re.search(
            r"<\s*tool_call\s*>\s*<\s*function\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*>"
            r"(.*?)</\s*function\s*>\s*</\s*tool_call\s*>",
            text,
            re.DOTALL,
        )
        if not m:
            return None
        name = m.group(1).strip()
        inner = m.group(2) or ""
        args_match = re.search(
            r"<\s*arguments\s*>(.*?)</\s*arguments\s*>",
            inner,
            re.DOTALL,
        )
        if args_match is None:
            args: dict = {}
        else:
            raw = args_match.group(1).strip()
            if not raw:
                args = {}
            else:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                if not isinstance(parsed, dict):
                    return None
                args = parsed
        return {"tool": name, "args": args}

    # ------------------------------------------------------------------ #
    #  DeepSeek DSML 文本格式 tool call（fallback 路径）                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_dsml_text(text: str) -> str | None:
        """将 DSML 标记的已知退化形态归一化为 `<|DSML|tag ...>`。

        DeepSeek 官方文档格式使用全角标记 <｜DSML｜tag>；实际输出常退化为
        ASCII 形式：
          <|DSML|tag>            （单竖线）
          <||DSML||tag>          （双竖线，HF#209）
          <| |DSML| |tag>        （竖线间带空格，用户聊天截图形态）
        归一化后统一为 <|DSML|tag ...>，便于正则解析。

        Returns: 归一化后的文本；若文本中没有任何 DSML 标记形态则返回 None。
        """
        import re

        if "DSML" not in text:
            return None
        t = text.replace("｜", "|")

        def _canon(m: re.Match) -> str:
            close = m.group(1)
            tag = m.group(2)
            attrs = m.group(3) or ""
            return f"<{close}|DSML|{tag}{attrs}>"

        # <| |DSML| |tag attrs> / <||DSML||tag> / <｜DSML｜tag> / </| |DSML| |tag>
        return re.sub(
            r"<\s*(/?)\s*\|(?:\s*\|)*\s*DSML\s*\|(?:\s*\|)*\s*"
            r"([A-Za-z_][A-Za-z0-9_]*)([^>]*)>",
            _canon,
            t,
        )

    @staticmethod
    def _parse_dsml_parameters(inner: str) -> dict:
        """解析 DSML parameter 元素为参数字典。

        <|DSML|parameter name="arg" string="true">value</|DSML|parameter>
        string="true"（或缺省）→ 原样字符串；string="false" → 尝试 JSON 字面量。
        """
        import re

        args: dict = {}
        for pm in re.finditer(
            r"<\|DSML\|parameter\s+name=\"([^\"]+)\""
            r"(?:\s+string=\"(true|false)\")?>(.*?)</\|DSML\|parameter>",
            inner,
            re.DOTALL,
        ):
            name = pm.group(1)
            is_string = pm.group(2) != "false"
            raw = pm.group(3).strip()
            if is_string:
                value = raw
            else:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw
            args[name] = value
        return args

    @staticmethod
    def _infer_dsml_tool_by_args(args: dict, tools: list[dict]) -> str | None:
        """退化形态缺失 invoke/tool 名时，按参数名匹配工具 schema 推断。

        只有唯一候选时才返回该工具名，否则返回 None（宁可保持原文输出，
        也不盲猜调用错误工具）。
        """
        if not args or not tools:
            return None
        given = set(args.keys())
        candidates: list[str] = []
        for schema in tools:
            fn = schema.get("function") or {}
            params = (fn.get("parameters") or {}).get("properties") or {}
            props = params.keys() if isinstance(params, dict) else []
            if given.issubset(set(props)):
                name = fn.get("name", "")
                if name:
                    candidates.append(name)
        return candidates[0] if len(candidates) == 1 else None

    def _try_parse_dsml_tool_call(
        self, text: str, tools: list[dict] | None = None
    ) -> dict | None:
        """解析 DeepSeek DSML 文本格式的伪 tool call。

        支持两种结构：
        1. 包装形式（官方文档）：
           <|DSML|tool_calls>...<|DSML|invoke name="TOOL">...
           <|DSML|parameter name="a" string="true">v</|DSML|parameter>
           </|DSML|invoke>...</|DSML|tool_calls>
           （root 名可能是 tool_calls / function_calls；标记全角/ASCII/退化均可）
        2. 单调用退化形式（用户截图）：
           <| |DSML| |tool_call>...</| |DSML| |parameter name="a" string="true">v
           </| |DSML| |parameter></| |DSML| |tool_call>
        3. 无标记形式（服务端剥掉 DSML 标记）：
           <function_calls><invoke name="TOOL">...</function_calls>

        Returns: {"tool": NAME, "args": {...}}；NAME 缺失且无法从 tools schema
        推断时返回 None。
        """
        import re

        if not text:
            return None
        normalized = self._normalize_dsml_text(text)

        # --- 1/2. 带 DSML 标记的形态（含退化） ---
        if normalized is not None:
            work = normalized

            # 1. 包装形式：root tool_calls/function_calls > invoke > parameter
            root_m = re.search(
                r"<\|DSML\|(?:tool_calls|function_calls)>(.*?)"
                r"</\|DSML\|(?:tool_calls|function_calls)>",
                work,
                re.DOTALL,
            )
            if root_m:
                invokes = re.findall(
                    r"<\|DSML\|invoke(?:\s+name=\"([A-Za-z_][A-Za-z0-9_]*)\")?>"
                    r"(.*?)</\|DSML\|invoke>",
                    root_m.group(1),
                    re.DOTALL,
                )
                if not invokes:
                    return None
                name, invoke_inner = invokes[0]
                name = name or ""
                args = self._parse_dsml_parameters(invoke_inner)
                if not name and tools:
                    name = self._infer_dsml_tool_by_args(args, tools) or ""
                return {"tool": name, "args": args} if name else None

            # 2. 单调用退化形式：root tool_call（可带 name 属性）> parameter
            single_m = re.search(
                r"<\|DSML\|tool_call(?:\s+name=\"([A-Za-z_][A-Za-z0-9_]*)\")?>"
                r"(.*?)</\|DSML\|tool_call>",
                work,
                re.DOTALL,
            )
            if single_m:
                name = single_m.group(1) or ""
                args = self._parse_dsml_parameters(single_m.group(2))
                if not name and tools:
                    name = self._infer_dsml_tool_by_args(args, tools) or ""
                return {"tool": name, "args": args} if name else None
            return None

        # --- 3. 无标记形式（文本中没有 DSML 标记，服务端可能剥掉了它们） ---
        work = text
        bare_m = re.search(r"<function_calls>(.*?)</function_calls>", work, re.DOTALL)
        if not bare_m:
            return None
        invokes = re.findall(
            r"<invoke\s+name=\"([A-Za-z_][A-Za-z0-9_]*)\"[^>]*>(.*?)</invoke>",
            bare_m.group(1),
            re.DOTALL,
        )
        if not invokes:
            return None
        name, invoke_inner = invokes[0]
        args: dict = {}
        for pm in re.finditer(
            r"<parameter\s+name=\"([^\"]+)\""
            r"(?:\s+string=\"(true|false)\")?>(.*?)</parameter>",
            invoke_inner,
            re.DOTALL,
        ):
            pname = pm.group(1)
            is_string = pm.group(2) != "false"
            raw = pm.group(3).strip()
            if is_string:
                value = raw
            else:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw
            args[pname] = value
        return {"tool": name, "args": args}

    async def _run_tool_loop(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 8192,
        on_tool_result=None,
    ) -> tuple[str, bool]:
        """通用工具调用循环。子类可复用。

        max_tokens 默认 8192（2026-09-18：2048 对 write_file 大参数工具
        预算过紧——6KB 策略文件 content ≈ 2000+ tokens，模型在输出预算
        压力下漏传必填 file_path；截断重试上限 32768 不受影响）。

        Returns:
            (final_content, is_fallback)
        """
        from .llm_client import LLMClient, is_fallback

        llm = LLMClient.get_instance()
        had_tool_results = False  # tracks whether any tools were executed
        claim_corrections = 0  # 防虚构守卫的纠错轮数
        # 失败感知总结（2026-09-18 虚构事故）：记录每个工具的失败次数与最近错误，
        # 供循环耗尽后的 summary 轮使用（告知 LLM 真实失败状态 + 拦截虚构总结）
        tool_failure_counts: dict[str, int] = {}
        # 同工具连续失败熔断（2026-10-14 web_search×32 事故）
        disabled_tools: set[str] = set()
        last_tool_error = ""
        # 指标锚定（2026-09-18 事故）：本轮 get_task_result 返回的复算值，
        # 同时注入工具结果旁（覆盖直接返回路径）与 summary prompt
        task_metrics: dict | None = None
        # 指标溯源（2026-10-19 事故）：本轮是否发生过「断言指标但不可溯源」的
        # 拦截。只要发生过，最终答复（不只是 notify_user 正文）同样要守住——
        # 事故里那轮任务的 result.content 也把虚构数字复述了一遍。
        metric_provenance_blocked = False

        for round_idx in range(self._max_tool_rounds):
            resp = await llm.chat_with_tools(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )
            content_len = len(resp.content) if resp.content else 0
            logger.info(
                "[%s] _run_tool_loop round %d/%d: has_tool_calls=%s, "
                "content_len=%d, reasoning=%s",
                self.name,
                round_idx + 1,
                self._max_tool_rounds,
                resp.has_tool_calls,
                content_len,
                bool(resp.reasoning_content),
            )
            if not resp.has_tool_calls:
                content = resp.content
                if is_fallback(content):
                    logger.warning(
                        "[%s] _run_tool_loop round %d: detected fallback marker",
                        self.name,
                        round_idx + 1,
                    )
                    return ("", True)

                # 本轮答复是否来自"工具通道故障后的降级路径"
                degraded = bool(getattr(resp, "degraded", False))
                degraded_reason = getattr(resp, "degradation_reason", "") or "unknown"

                # 检查是否是 fallback 路径的 JSON tool call（降级路径的补救：
                # 若模型仍输出了 JSON tool call，工具依然可以真正执行）
                json_tc = self._try_parse_json_tool_call(content, tools)
                if json_tc is None:
                    # 工具通道故障 + 本轮没有任何工具执行 → 拒绝把这段
                    # "看起来像答复"的降级文本转发给用户（否则用户会以为已执行）
                    if degraded and not had_tool_results:
                        logger.error(
                            "[%s] _run_tool_loop round %d: tool channel degraded "
                            "(%s), no tool executed this turn; refusing to relay "
                            "degraded answer (content_len=%d)",
                            self.name,
                            round_idx + 1,
                            degraded_reason,
                            content_len,
                        )
                        return (
                            self._TOOL_CHANNEL_DOWN_REPLY.format(
                                reason=degraded_reason
                            ),
                            False,
                        )
                    if content and content.strip():
                        # ── 防虚构守卫 ──
                        # LLM 声称“任务已提交 / 已出回测报告”，但本轮未执行任何工具
                        # （如没有调用 submit_backtest / get_task_result）时，该答复
                        # 属于虚构：任务 ID / 指标并非真实工具返回值，前端也不会有记录。
                        # 强制追加一轮纠错，要求其实际调用工具；纠错超限则拒绝转发。
                        if self._looks_like_unverified_task_claim(
                            content, messages, had_tool_results
                        ):
                            if claim_corrections < self._max_claim_corrections:
                                claim_corrections += 1
                                logger.warning(
                                    "[%s] _run_tool_loop round %d: LLM claimed task "
                                    "submission/result without executing any tool; "
                                    "forcing corrective round %d/%d",
                                    self.name,
                                    round_idx + 1,
                                    claim_corrections,
                                    self._max_claim_corrections,
                                )
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": self._UNVERIFIED_CLAIM_CORRECTION,
                                    }
                                )
                                continue
                            logger.error(
                                "[%s] _run_tool_loop: LLM persisted unverified task "
                                "claim after %d correction(s); refusing to relay "
                                "fabricated answer",
                                self.name,
                                self._max_claim_corrections,
                            )
                            return (
                                "系统未能确认你的任务已提交/已执行：本轮对话中没有检测到任何"
                                "实际的工具调用（例如 submit_backtest / get_task_result），"
                                "因此无法验证答复中的任务 ID 与回测结果。请重试，"
                                "或联系系统管理员检查 Agent 工具调用链路。",
                                False,
                            )
                        if degraded:
                            logger.warning(
                                "[%s] _run_tool_loop round %d: degraded round "
                                "after executed tool result(s); appending notice",
                                self.name,
                                round_idx + 1,
                            )
                            return (
                                content
                                + self._TOOL_CHANNEL_DEGRADED_NOTE.format(
                                    reason=degraded_reason
                                ),
                                False,
                            )
                        return (content, False)
                    # 空内容但之前有工具执行结果，强制 LLM 总结
                    if had_tool_results:
                        logger.warning(
                            "[%s] LLM returned empty content after tool execution, "
                            "forcing summary",
                            self.name,
                        )
                        break
                    # 既无工具也无内容，跳过本轮
                    logger.debug(
                        "[%s] _run_tool_loop round %d: empty content, no tool calls, "
                        "skipping (had_tool_results=%s)",
                        self.name,
                        round_idx + 1,
                        had_tool_results,
                    )
                    continue

                # 将 JSON tool call 转为标准 ToolCallRequest
                tc = type(
                    "ToolCallRequest",
                    (),
                    {
                        "call_id": "json_tc_0",
                        "name": json_tc["tool"],
                        "arguments": json_tc["args"],
                    },
                )()
                tool_calls = [tc]
            else:
                tool_calls = resp.tool_calls

            # 执行工具
            tool_results = []
            all_disabled = True
            for tc in tool_calls:
                if tc.name in disabled_tools:
                    # 熔断：该工具已连续失败达阈值，不再真实执行，
                    # 直接注入禁用通知（明令 LLM 停止重试）
                    result_text = self._TOOL_DISABLED_NOTICE.format(
                        tool=tc.name,
                        n=tool_failure_counts.get(tc.name, 0),
                    )
                elif self._violates_metric_provenance(
                    tc.name, tc.arguments, task_metrics
                ):
                    # 指标溯源（2026-10-19 事故）：notify_user 正文断言了回测
                    # 指标，但本轮 get_task_result 没给过可核验的返回值
                    # （或数字对不上）→ 不执行，把错误回给 LLM 让它先查真值。
                    # 关键：这条路径不设置 had_tool_results，因此「先 notify_user
                    # 再编数字」无法再借道绕过防虚构守卫。
                    logger.error(
                        "[%s] metric provenance blocked %s: unanchored=%s "
                        "task_metrics=%s",
                        self.name,
                        tc.name,
                        self._unanchored_metric_claims(
                            tc.arguments.get("message") or "", task_metrics
                        ),
                        task_metrics,
                    )
                    metric_provenance_blocked = True
                    result_text = self._METRIC_PROVENANCE_ERROR
                else:
                    all_disabled = False
                    result_text = await self._execute_tool_call(tc)
                if result_text.startswith("Error"):
                    tool_failure_counts[tc.name] = (
                        tool_failure_counts.get(tc.name, 0) + 1
                    )
                    last_tool_error = result_text[:300]
                    if (
                        tool_failure_counts[tc.name]
                        >= self._TOOL_FAILURE_DISABLE_THRESHOLD
                    ):
                        disabled_tools.add(tc.name)
                        logger.warning(
                            "[%s] circuit breaker: tool %s failed %d times, "
                            "disabled for the rest of this loop",
                            self.name,
                            tc.name,
                            tool_failure_counts[tc.name],
                        )
                elif tc.name == "get_task_result":
                    # 指标锚定（2026-09-18 事故）：get_task_result 成功且含指标时，
                    # 在工具结果旁追加复算值锚定块 + 硬规则——后续每一轮
                    # （含直接返回的最终轮）LLM 都能在原数据旁看到锚定值。
                    task_metrics = self._extract_task_metrics(result_text)
                    if task_metrics:
                        result_text = (
                            result_text
                            + "\n\n[复算值锚定 · 报告唯一事实来源]\n"
                            + "\n".join(f"{k}={v}" for k, v in task_metrics.items())
                            + "\n"
                            + self._METRIC_ANCHOR_RULE
                        )
                if on_tool_result:
                    await on_tool_result(tc.name, result_text)
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.call_id,
                        "content": result_text,
                    }
                )
                had_tool_results = True
                # Update last_alive so watchdog knows task is still making progress
                from .task_tracker import tracker_context

                tracker = tracker_context.get(None)
                if tracker is not None:
                    tracker.alive()

            assistant_msg = {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.call_id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in tool_calls
                ],
            }
            if resp.reasoning_content:
                assistant_msg["reasoning_content"] = resp.reasoning_content
            messages.append(assistant_msg)
            messages.extend(tool_results)

            # 本轮所有工具调用均已熔断禁用 → 继续循环只会空转烧轮次
            # （2026-10-14 web_search×32 事故），直接跳出并走 summary 收尾。
            if all_disabled:
                logger.warning(
                    "[%s] _run_tool_loop round %d: all tool calls hit circuit "
                    "breaker (%s); breaking out to summary",
                    self.name,
                    round_idx + 1,
                    ", ".join(sorted(disabled_tools)),
                )
                break

        # 超出轮次或 LLM 返回空内容但有工具结果，让 LLM 总结
        logger.info(
            "[%s] _run_tool_loop exiting loop: had_tool_results=%s, "
            "message_count=%d, requesting summary from LLM",
            self.name,
            had_tool_results,
            len(messages),
        )
        history_text = "\n".join(
            f"[{m.get('role', 'user')}]: {m.get('content', '')}" for m in messages
        )
        # ── 失败感知总结（2026-09-18 虚构事故）──
        # 30 次 write_file 失败撞轮次上限后，裸的“请给出最终回答” prompt 让 LLM
        # 虚构了完整成功报告（假文件已写入 + 假 task_id + 假回测指标）。
        # summary 轮必须告知：终止原因、失败工具清单、并明令禁止对失败操作宣称成功。
        summary_user = f"{history_text}\n\n请根据以上工具调用结果给出最终回答。"
        total_tool_failures = sum(tool_failure_counts.values())
        if total_tool_failures > 0:
            fail_desc = "、".join(
                f"{name} 失败 {cnt} 次"
                for name, cnt in sorted(tool_failure_counts.items())
            )
            summary_user += (
                "\n\n【系统提示 · 必须遵守】工具循环因达到轮次上限被强制终止，"
                f"期间工具执行失败：{fail_desc}。最近一次错误：{last_tool_error}。"
                "失败的工具调用意味着对应操作没有成功——文件没有写入、任务没有提交、"
                "回测没有执行。你的最终回答必须基于真实工具结果：对失败的操作如实说明"
                "失败原因与实际完成的部分，严禁声称文件已写入、任务已提交、回测已完成，"
                "严禁编造 task_id、文件路径或任何回测指标。"
            )
        # 指标锚定（2026-09-18 事故）：本轮 get_task_result 返回过复算值时，
        # summary 轮 prompt 同样注入锚定块 + 硬规则（双重覆盖 summary 路径）。
        if task_metrics:
            summary_user += (
                "\n\n[复算值锚定 · 报告唯一事实来源（get_task_result 返回值）]\n"
                + "\n".join(f"{k}={v}" for k, v in task_metrics.items())
                + "\n"
                + self._METRIC_ANCHOR_RULE
            )
        final = await llm.chat(
            system=system,
            user=summary_user,
            max_tokens=max_tokens,
        )
        if is_fallback(final):
            # 收尾总结也彻底失败：明确告知，不要把 FALLBACK_MARKER 当答复转发
            logger.error(
                "[%s] _run_tool_loop summary LLM unavailable (fallback marker, "
                "had_tool_results=%s)",
                self.name,
                had_tool_results,
            )
            tail = (
                "已执行的工具结果仍然有效（可在回测记录中查看）。"
                if had_tool_results
                else "本次请求未执行：没有调用任何工具、也没有提交任何任务或回测。"
            )
            return (
                f"⚠️ 无法生成总结：LLM 不可用（provider 故障）。{tail}\n"
                "请稍后重试；若持续失败，请检查 LLM provider 状态。",
                False,
            )
        final_len = len(final) if final else 0
        if not final or not final.strip():
            logger.warning(
                "[%s] _run_tool_loop summary returned empty content "
                "(final_len=%d, had_tool_results=%s, messages=%d)",
                self.name,
                final_len,
                had_tool_results,
                len(messages),
            )
        # ── 防虚构守卫（summary 轮）──
        # 有工具失败 + summary 仍含任务完成声明 + 声称写入的文件经磁盘验证不存在
        # → 判定虚构，拒绝转发（2026-09-18 quant write_file×30 事故：summary
        # 虚构"文件已写入 + task_id + 回测报告"，工作流照常 STEP_OK）。
        if (
            total_tool_failures > 0
            and self._SUMMARY_SUCCESS_CLAIM.search(final)
            and self._claims_unwritten_file(final)
        ):
            fail_desc = "、".join(
                f"{name} 失败 {cnt} 次"
                for name, cnt in sorted(tool_failure_counts.items())
            )
            logger.error(
                "[%s] _run_tool_loop summary after failed tool run still claims "
                "task success with unwritten file (failures=%s); refusing to relay "
                "fabricated report",
                self.name,
                tool_failure_counts,
            )
            return (
                "⚠️ 本次总结已被系统拦截：工具循环达到轮次上限被强制终止，"
                f"期间 {fail_desc}（最近错误：{last_tool_error}）。"
                "总结中包含与真实工具结果不符的成功声明"
                "（声称已写入的文件经磁盘验证不存在），为防止虚构内容作为结果送达，"
                "已拦截该总结。请排查工具失败原因后重试任务。",
                False,
            )
        # 指标溯源（2026-10-19 事故）：本轮拦截过「不可溯源的指标断言」时，
        # 收尾答复也不许复述那些数字——事故里 result.content 正是第二处出口。
        if (
            metric_provenance_blocked
            and self._claims_backtest_metrics(final)
            and not self._metric_claims_are_anchored(final, task_metrics)
        ):
            logger.error(
                "[%s] summary still asserts unanchored backtest metrics after "
                "provenance block; refusing to relay fabricated report",
                self.name,
            )
            return (
                "⚠️ 本次回测报告已被系统拦截：报告中的指标无法在 "
                "get_task_result 的返回值中核验（本轮没有取到真实结果，"
                "或数字与返回值不一致）。为防止虚构数字送达，已拦截该报告。\n"
                "请重新查询任务结果后重试；若任务仍在运行，请稍后再查。",
                False,
            )
        return (final, False)

    async def run_tool(self, tool_name: str, **kwargs) -> Any:
        """执行一个工具，优先从全局ToolRegistry查找"""
        registry = _get_tool_registry()
        tool = registry.get(tool_name)
        if tool is None:
            raise ValueError(f"Tool {tool_name!r} not found in registry")
        result = await tool.execute(**kwargs)
        logger.debug("[%s] tool=%s success=%s", self.name, tool_name, result.success)
        return result

    @abstractmethod
    async def handle(self, message: AgentMessage, on_tool_result=None) -> AgentResult:
        """处理一条消息，返回结果"""

    async def start(self) -> None:
        self._running = True
        logger.info("[%s] started", self.name)

    async def stop(self) -> None:
        self._running = False
        logger.info("[%s] stopped", self.name)
