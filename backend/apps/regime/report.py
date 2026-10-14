"""五段日报的生成与落库（第①段单元 8iii）+ 投递与投递看门狗（第①段单元 8iv）。

CONTEXT.md 第 173 条是这一段的全部正文，本模块是它的实现。要点按重要性排：

## 报的是「今天刚产出、明日 08:00 才生效」的那条，不是此刻生效中的那条

一天可见期的全部价值就是「异常在生效前被拦下」，而日报是用户**唯一**能看到待生效结论的
地方——只报当前生效态，等于让可见期只存在于代码里。所以第①段必须读 `effective_at`
（= `business_midnight(run_day + 1)`，也就是「自明日 08:00 起生效」那句话的字面出处），
**必须明写生效时刻**，否则「今日判定」这个词就在撒谎：此刻咬人的是另一条。

代价是第①②段读起来是未来时。这与「停自动、恢复人工」是同一种时间观：机制先把话说出来，
再动手。

## 一行一天，且只在今天有结论（或等到了截止时刻）之后才写

日报是一份**发给人的消息**，不是心跳日志：一天发两版会让人以为机制改主意了。判据与
`apps.regime.shadow` 同源——那张表已经替本模块解决了「数据晚到几分钟」这个时序问题
（业务日 D 的第一轮心跳恰好撞上 D−1 那根日线的收盘，判定会先回 `stale_candles`），
所以本模块**不重复判断**，只读 `judgement` 的返回值：有结论就写，没结论就等。

等多久是这条规则唯一需要第二个出口的地方。判定的失败分两类，出口也分两个：

- **数据类失败**（`skipped`：日线没到、预热未满）——判定任务正常返回，只是没有结论。
  等到 `config.REPORT` 的业务日界 + `watchdog_hour:watchdog_minute`，照发一份写着
  「今日判定缺失，处于保持的上一有效状态」的日报。**沉默必须能被识别为异常**，而一份
  永不出现的日报与「今天没什么事」在聊天窗口里长得一样。
- **故障类失败**（抛异常）——`apps.trading.tasks` 的这条链路直接中断，本模块根本不被
  调用。这一类的兜底是单元 8iv 的独立看门狗（「当天日报在固定时刻仍未成功投递则升级
  告警」）。**这不是漏洞，是那句「独立」的全部含义**：让日报自己报告「日报没发出去」，
  等于让一个哑掉的东西开口说话。

截止时刻与看门狗时刻**共用同一个数**（`watchdog_hour` / `watchdog_minute`）：它问的是
「到了这个点日报该到了没有」。看门狗不挂 crontab，而是固定间隔轮询 + 在函数里比这个
时刻（`check_report_delivery`），口径因此只有 `_deadline` 一处。

## 五段落成 `sections`，正文是它的渲染结果，不另存一份

`body` 不做成列：它就是 `sections` 拼起来的那段文字，两份正文迟早会漂开，而日报的正文
恰好是**给人看的那一份**。分段落库还让「第①段说了什么」可以被定向引用，不必去解析正文。

## 第②段是结构化的，其余四段是文本

第②段要按人裁剪（「停用段只含你实际受影响的策略」），裁剪意味着**从条目里挑**，而一段
拼好的文本挑不出条目来。所以第②段以结构化的 `change` 落在 `landscape` 里，渲染发生在
**裁剪之后**（`render_change`）。其余四段对所有人相同，直接是文本。

## 受众与裁剪

受众是所有 `is_active` 用户（CONTEXT.md 第 173 条）。判定是有用的公共信息，而「一个你没
在跑的策略被停了」纯属噪声——噪声会让人开始忽略日报，那时「必发」就只剩形式。所以：

- 第①②③④⑤段对所有人相同；
- 第②段只保留与**该用户活跃会话**相关的条目；筛完为空则**整节不出现**（无活跃会话者
  根本看不到这一节）。

裁定「与用户相关」用的是活跃会话（`LiveSession.ACTIVE_STATUSES`），不是策略的
`is_active`：后者是「这个策略退役了没有」的人工总开关（CONTEXT.md 第 106 条），与
「谁在跑它」是两件事。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.common.time_utils import business_tz, format_business, to_business
from apps.regime import config, halt, judgement, mechanism_switch
from apps.regime.deactivation import BLOCKED_DISPLAY, is_blanket
from apps.regime.events import describe_candidate, describe_event
from apps.regime.models import (
    CANDIDATE_DISCARD_EXPIRED,
    NO_ESCALATION_DISPLAY,
    ActorKind,
    CandidateEvent,
    CandidateStatus,
    DailyReport,
    Escalation,
    EventImpact,
    EventStatus,
    HaltTrigger,
    MajorEvent,
    MechanismKind,
    MechanismMode,
    RegimeJudgement,
    RegimeMechanismSwitch,
)
from apps.regime.quant import BaseRegime
from apps.trading.models import LiveSession, Strategy

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 段落名与标题
# --------------------------------------------------------------------------- #

SECTION_TODAY = "today"
SECTION_CHANGE = "change"
SECTION_EVENTS = "events"
SECTION_HEALTH = "health"
SECTION_DELIVERY = "delivery"

#: 段落顺序 = 五段在正文里的顺序。**这是一份契约**：`sections` 是落库的 JSON，8v 的只读
#: 工具与审计会按键取段，所以键名与顺序都不要顺手改。
SECTIONS = (
    SECTION_TODAY,
    SECTION_CHANGE,
    SECTION_EVENTS,
    SECTION_HEALTH,
    SECTION_DELIVERY,
)

_SECTION_TITLES = {
    SECTION_TODAY: "一、今日判定",
    SECTION_CHANGE: "二、相对昨日的变化",
    SECTION_EVENTS: "三、未来 {horizon} 天的高影响事件",
    SECTION_HEALTH: "四、机制健康",
    SECTION_DELIVERY: "五、昨日日报的投递结果",
}

#: 判定层 `skipped` 的展示名。**缺失也是一种结论**，要说出是哪一种缺失——「不知道」与
#: 「数据还没到」对读的人是两件事，前者要人去看，后者等一下就好。
_SKIP_DISPLAY = {
    "no_candles": "日线库为空（回填任务没跑过）",
    "stale_candles": "日线还没到签署日（数据管道比日界慢）",
    "empty_series": "日线序列为空",
    "undecidable": "量化判不出来（预热未满，或分位窗口还没攒满 250 日）",
}

#: 写库的收场。
OUTCOME_CREATED = "created"
#: 当天已有一份日报（心跳 5 分钟一轮，绝大多数 tick 走到这里是这一种）。
OUTCOME_KEPT = "kept"
#: 还没有结论，也还没到截止时刻——不是失败，是「再等等」。
OUTCOME_WAITING = "waiting"

#: 判定层 `evidence["decision"]["action"]` 的取值之一：量化判出的候选阶段被**持续期**拦下，
#: 于是沿用了当前阶段。与 `judgement._DECISION_CARRIED` 是同一个字面量——那边是私有名，
#: 这里照抄一份并把耦合写在明面上：两处必须一起改，而 `test_report.py` 钉着这个等式。
ACTION_CARRIED = "carried"


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def write_daily_report(
    judgement_result: dict,
    deactivation: dict,
    shadow: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """把这一轮的判定 / 推导 / Shadow 摘要落成当天的日报，返回一行摘要。

    三个参数依次是 `run_daily_judgement()` / `run_deactivation()` /
    `write_shadow_record()` 的返回值（任务层放在 `payload` 里）。**本模块不重新取数**：
    日报与 Shadow 记录必须说同一件事，两处各取一次就会在日界附近各说各话。

    不抛异常的那一半与 `shadow.write_shadow_record` 同一纪律：判定没有结论**不是本层的
    失败**，照写（这正是「沉默必须能被识别为异常」的落点）。数据库故障才抛，往上让任务
    标 FAILURE。
    """
    now = now or timezone.now()
    symbol = (
        judgement_result.get("symbol")
        or shadow.get("symbol")
        or deactivation.get("symbol")
        or config.CANDLES.symbol
    )
    run_day = _run_day(judgement_result, shadow, now)

    existing = DailyReport.objects.filter(symbol=symbol, run_day=run_day).first()
    if existing is not None:
        # 一天一份。心跳五轮一轮，绝大多数 tick 走到这里只确认已有那一份。
        return _summary(existing, OUTCOME_KEPT)

    if not _ready(judgement_result, run_day, now):
        return {
            "symbol": symbol,
            "run_day": run_day.isoformat(),
            "outcome": OUTCOME_WAITING,
            "written": False,
            "sections": {},
            "note": "今日判定尚无结论，且未到日报截止时刻，本轮不写日报",
        }

    # 三值所抄的那一行判定：第①段的依据数字只存在于它的 `evidence` 里，外键也指向它。
    # 只查一次，组稿与落库共用。**不向 Shadow 摘要要**——它返回的那张摘要里没有外键，
    # 而「外键非空 ⟺ 三值非空」这条不变量在两处各判一次迟早会漂开。
    record = _judgement_row(symbol, judgement_result)
    sections, landscape = _compose(
        symbol, run_day, judgement_result, deactivation, record, now=now
    )
    row, created = DailyReport.objects.get_or_create(
        symbol=symbol,
        run_day=run_day,
        defaults={
            "judgement": record,
            **_inline_values(judgement_result),
            "deactivation_skipped": deactivation.get("skipped") or "",
            "judgement_missing": not judgement_result.get("base_regime"),
            "sections": sections,
            "landscape": landscape,
            "note": _note(judgement_result, deactivation, landscape),
        },
    )
    if created:
        logger.info("[regime] 日报落库 %s", _summary(row, OUTCOME_CREATED))
    return _summary(row, OUTCOME_CREATED if created else OUTCOME_KEPT)


# --------------------------------------------------------------------------- #
# 什么时候可以写
# --------------------------------------------------------------------------- #


def _ready(judgement_result: dict, run_day: date, now: datetime) -> bool:
    """今天这份日报该不该写了。

    两条路（见模块 docstring）：判定出了结论，或者等到了截止时刻。截止时刻按**业务时区**
    解读——`watchdog_hour` 是给人看的钟点（「早九点还没收到就该响了」），不是 UTC 小时。

    截止时刻早于业务日界（北京 08:00）时，`deadline <= 日界` 恒真，于是当天第一轮心跳
    就会写一份「判定缺失」——那是配置写错了，不是时序问题。这里不拦，但 `ReportConfig`
    的 docstring 与它的默认值（9 点）已经把正常形状说清楚了。
    """
    if judgement_result.get("base_regime"):
        return True
    return now >= _deadline(run_day)


def _deadline(run_day: date) -> datetime:
    """当天日报的截止时刻（绝对时刻）。"""
    return datetime.combine(
        run_day,
        time(config.REPORT.watchdog_hour, config.REPORT.watchdog_minute),
        tzinfo=business_tz(),
    )


def _run_day(judgement_result: dict, shadow: dict, now: datetime) -> date:
    """这一份日报记在哪个业务日。

    判定层的 `run_day` 是权威（它决定 `effective_at`，也就是判定记录的唯一键），Shadow
    摘要是同一口径的二手来源。两者都缺席时（测试、数据订正）才回落到本地时刻——回落值
    可能与判定层不一致，所以缺席要留痕。
    """
    for source, key in ((judgement_result, "run_day"), (shadow, "run_day")):
        raw = source.get(key)
        if raw:
            return date.fromisoformat(raw)
    logger.warning(
        "[regime] 判定与 Shadow 摘要都没有 run_day，按本地时刻推断日报运行日"
    )
    return _business_day(now)


def _business_day(now: datetime) -> date:
    """这个绝对时刻落在哪个**业务日**（业务时区的自然日）。

    与 `business_midnight` 是同一个口径的两面：日界是业务时区的 08:00（== 日线换线），
    所以「今天的日报」按业务日算，不按进程时区的自然日算。
    """
    return to_business(now).date()


# --------------------------------------------------------------------------- #
# 组稿
# --------------------------------------------------------------------------- #


def _compose(
    symbol: str,
    run_day: date,
    judgement_result: dict,
    deactivation: dict,
    record: RegimeJudgement | None,
    *,
    now: datetime,
) -> tuple[dict, dict]:
    """把五段组出来，返回 `(sections, landscape)`。

    `record` 由调用方查好后传进来（第①段的依据数字与外键用的是同一行，查一次就够）。

    `landscape` 是 Q3 要求的结构化快照：`DeactivationDecision` 的行是**原地更新**的
    （同一策略 × 阶段只有一行，`last_confirmed_at` 天天往前走），所以「昨天的建议清单」
    只能靠当天那一份快照回答，明天再去读决策表读到的已经不是昨天的世界了。
    """
    landscape = _landscape(symbol, run_day, judgement_result, deactivation)
    sections = {
        SECTION_TODAY: _section_today(judgement_result, record, symbol),
        SECTION_CHANGE: render_change(landscape[SECTION_CHANGE]),
        SECTION_EVENTS: _section_events(now=now),
        SECTION_HEALTH: _section_health(
            symbol, judgement_result, deactivation, now=now
        ),
        SECTION_DELIVERY: _section_delivery(symbol, run_day),
    }
    return sections, landscape


def _inline_values(judgement_result: dict) -> dict:
    """三值内联副本。与 `ShadowDailyRecord` 同一取舍：日报要能**独立**回答「那天判的是
    什么」，判定记录被数据订正过之后，一份审计文件跟着改就成了改历史。"""
    return {
        "base_regime": judgement_result.get("base_regime") or "",
        "escalation": judgement_result.get("escalation") or "",
        "effective_regime": judgement_result.get("effective_regime") or "",
    }


def _judgement_row(symbol: str, judgement_result: dict) -> RegimeJudgement | None:
    """三值所抄的那一行判定。

    第①段的**依据数字**（分位、斜率、分离度）只存在于这一行的 `evidence` 里——判定层的
    返回值故意不带它们（那会让每次心跳都多背一坨只在落库时用一次的数据）。所以这是本模块
    唯一必须下钻的一次查询，查询键与 `judgement._record_once` 的唯一约束是同一对。
    """
    raw = judgement_result.get("effective_at")
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning("[regime] 判定返回值的 effective_at 解析不了：%r", raw)
        return None
    return RegimeJudgement.objects.filter(symbol=symbol, effective_at=at).first()


# --------------------------------------------------------------------------- #
# 第一段：今日新判定
# --------------------------------------------------------------------------- #


def _section_today(
    judgement_result: dict, record: RegimeJudgement | None, symbol: str
) -> str:
    """第①段。**必须明写生效时刻**（CONTEXT.md 第 173 条）。"""
    lines = [f"标的：{symbol}"]
    if not judgement_result.get("base_regime"):
        return "\n".join(
            [*lines, *_missing_judgement_lines(judgement_result, record, symbol)]
        )

    effective = _regime_display(judgement_result.get("effective_regime"))
    base = _regime_display(judgement_result.get("base_regime"))
    lines.append(f"今日判定：{effective}")

    raw_effective_at = judgement_result.get("effective_at")
    if raw_effective_at:
        lines.append(f"生效时刻：{format_business(_parse(raw_effective_at))}")
    lines.append("（此刻生效中的仍是上一有效阶段，本条要到生效时刻才咬人）")

    escalation = judgement_result.get("escalation") or ""
    if escalation:
        lines.append(
            f"基础阶段：{base}；被抬升为 {effective}"
            f"（抬升标志：{_escalation_display(escalation)}）"
        )
    elif base != effective:
        lines.append(f"基础阶段：{base}")

    decision = (record.evidence or {}).get("decision") if record else None
    decision = decision or {}
    if decision.get("action") == ACTION_CARRIED:
        days = decision.get("in_force_days")
        lines.append(
            "维持不变：量化判出的候选阶段是 "
            f"{_regime_display((record.evidence or {}).get('quant', {}).get('regime'))}，"
            f"但当前阶段只站了 {days} 个自然日（不足 "
            f"{config.JUDGEMENT_LIFECYCLE.min_dwell_days} 天），本次沿用 {effective}"
        )

    numbers = _quant_lines(record)
    if numbers:
        lines.append("依据：")
        lines.extend(numbers)
    return "\n".join(lines)


def _missing_judgement_lines(
    judgement_result: dict, record: RegimeJudgement | None, symbol: str
) -> list[str]:
    """判定缺失时的第①段正文。

    措辞照 CONTEXT.md 第 173 条的原话：「今日判定缺失，处于保持的上一有效状态」。再补一句
    原因——「不知道」和「数据还没到」对读的人是两件事。上一有效状态从库里读，不猜。

    `symbol` 必须由调用方给：`judgement.current_judgement(None)` 会去查 `symbol=NULL`，
    那永远查不到，于是「库里有生效判定」会被读成「冷启动」——一个自洽的错答案。
    """
    if record is not None and record.base_regime:
        # 走到这里说明返回值说没结论、库里那条却有——口径已经漂开，照实说，别掩盖。
        logger.warning(
            "[regime] 判定返回值无结论，但记录 %s 有三值，第①段按记录写", record.pk
        )
        return [f"今日判定：{_regime_display(record.effective_regime)}（记录口径）"]

    skipped = judgement_result.get("skipped") or ""
    reason = _SKIP_DISPLAY.get(skipped, skipped or "未说明")
    lines = [f"今日判定缺失（{reason}），处于保持的上一有效状态。"]

    current = judgement.current_judgement(symbol)
    if current is None:
        lines.append("库中还没有任何生效过的判定（冷启动）。")
    else:
        lines.append(
            f"当前生效：{_regime_display(current.effective_regime)}，"
            f"自 {format_business(current.effective_at)} 起"
        )
    latest = judgement.last_judgement(symbol)
    if latest is not None and (current is None or latest.pk != current.pk):
        # 「最近写下的」与「生效中的」不是同一条时才补这一句。它是不是已经咬了人，上面
        # 那一行已经说了（生效时刻写在那儿）；这里只补「机制上一次成功是什么时候」。
        lines.append(
            f"最近写下的一条是 {format_business(latest.created_at)} 那次"
            f"（运行日 {latest.run_day}，自 {format_business(latest.effective_at)} 起生效）"
        )
    return lines


def _quant_lines(record: RegimeJudgement | None) -> list[str]:
    """依据的具体数字（CONTEXT.md 第 173 条：分位/斜率，不是「市场波动加大」这类话）。

    全部从判定记录自己的 `evidence` 里读——那是判定当时亲手写下的原话。取不到就**不写**，
    不拿别的量凑：一句编出来的「依据」比没有依据更坏。
    """
    if record is None:
        return []
    quant = (record.evidence or {}).get("quant") or {}
    lines: list[str] = []

    atr_pct = quant.get("atr_pct")
    rank = quant.get("atr_pct_rank")
    sample = quant.get("quantile_sample")
    if atr_pct is not None:
        text = f"  ATR% {_num(atr_pct)}"
        if rank is not None:
            text += f"，{sample or config.JUDGEMENT.quantile_window_days} 日分位 {_pct(rank)}"
            if rank >= config.JUDGEMENT.high_vol_quantile:
                # 阈值是**分位**的阈值（判据就是「ATR% 分位 > 80%」），拿来比的是分位这
                # 个数，不是 ATR% 本身——两个量纲差着一个数量级，混起来会把「今天波动
                # 很大」判成常态化。比较发生在 `rank is not None` 里面，因为分位算不出来
                # 时这个判据压根没有取值。
                text += "（高于高波动阈值）"
        lines.append(text)

    slow = quant.get("ema_slow")
    if slow is not None:
        lines.append(
            f"  EMA{config.JUDGEMENT.ema_fast_period} {_num(quant.get('ema_fast'))} / "
            f"EMA{config.JUDGEMENT.ema_slow_period} {_num(slow)}，"
            f"斜率 {_num(quant.get('ema_slope'), digits=6)}"
        )

    separation = quant.get("separation")
    if separation is not None:
        lines.append(f"  分离度 (EMA快−EMA慢)/ATR = {_num(separation, digits=3)}")
    return lines


def _num(value: object, *, digits: int = 4) -> str:
    if value is None:
        return "未算"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _regime_display(value: object) -> str:
    """slug → 中文展示名。取不到展示名时原样回显 slug，**不猜**。"""
    if not value:
        return "（无）"
    try:
        return BaseRegime(value).display
    except ValueError:
        return str(value)


def _escalation_display(value: object) -> str:
    """抬升标志 slug → 中文展示名，与 `_regime_display` 同一纪律。

    展示名的出处是模型层自己给的（`NO_ESCALATION_DISPLAY` 的 docstring 明写「查询/日报要
    显示『无抬升』时用它，不要就地写中文」），所以这里不另写一份映射。**原本这里写的是
    裸 slug**：`Escalation` 是 `str` 枚举，`f"{escalation}"` 不报错、只是把「news」端到
    用户眼前——而这一段的全部意义就是回答「今天为什么更保守」。
    """
    if not value:
        return NO_ESCALATION_DISPLAY
    try:
        return Escalation(value).display
    except ValueError:
        return str(value)


# --------------------------------------------------------------------------- #
# 第二段：相对昨日的变化（结构化 + 按人裁剪）
# --------------------------------------------------------------------------- #


def _landscape(
    symbol: str,
    run_day: date,
    judgement_result: dict,
    deactivation: dict,
) -> dict:
    """今天这一份快照。第②段的比对基准就是它（Q3）。

    ## 底料是**当天推导产出的建议清单**，不是决策表

    两处都能拿到「策略 × 阶段」，选这里有两个各自独立的理由：

    1. 决策表存的是**当前状态**，不是**变更**。第③段的 gate 层确实会翻开
       `DeactivationDecision.status`（`applied` / `released`），但翻面只写那一列、不留
       「什么时候翻的」（③b Q9），而日报第②段要的正是「相对昨日的变化」——把今天这张表与
       昨天那张做差，读出来的是「这两天的状态不一样」，答不出「今天变了什么、变的是哪
       一层」。当天推导的清单则会随着池化表换代而变化，缩得下去。
    2. 这一天**到底说了什么**只有清单记得：决策行是滚动的，明天再读读到的已经不是今天的
       世界。`ShadowDailyRecord.suggestions` 与这里是同一份底料（同一次调用的同一个
       `payload`），所以日报第②段与 Shadow 记录不会在日界附近各说各话。

    ## 键是「层」

    `by_regime` 的键是建议里的 `regime`（阶段 slug），值是那一层的策略清单——CONTEXT.md
    第 173 条要的「将新停用 / 将解除〈某层〉」里的「层」正是它。`state` 一并冻结：「为什么
    这天说它不适用」在决策行被后世覆盖之后就只剩这一份了。

    被豁免的（`exempt`）照落在清单里：豁免改变的是「这条建议会不会真咬人」，不是「机制
    说过什么」。第②段按结构做差，把这两件事混起来会让「豁免到期」看起来像「新停用」。
    """
    suggestions = list(deactivation.get("suggestions") or [])
    names = _strategy_names([s.get("strategy_id") for s in suggestions])

    by_regime: dict[str, list[dict]] = {}
    for item in suggestions:
        sid = item.get("strategy_id")
        if not sid:
            continue
        by_regime.setdefault(item.get("regime") or "", []).append(
            {
                "strategy_id": str(sid),
                "name": names.get(str(sid)) or "",
                "state": item.get("state") or "",
                "reason": item.get("reason") or "",
                "source": item.get("source") or "",
                "running": bool(item.get("running")),
                "exempt": bool(item.get("exempt")),
                "created": bool(item.get("created")),
            }
        )
    for entries in by_regime.values():
        # 顺序不稳会让「今天和昨天有什么不同」多出一堆假的差异。
        entries.sort(key=lambda e: e["strategy_id"])

    return {
        "symbol": symbol,
        "run_day": run_day.isoformat(),
        "regime": judgement_result.get("effective_regime") or "",
        "regime_display": _regime_display(judgement_result.get("effective_regime"))
        if judgement_result.get("effective_regime")
        else "",
        "blocked": deactivation.get("skipped") or "",
        "generation_id": deactivation.get("generation_id"),
        "managed": deactivation.get("managed"),
        "unresolved_count": len(deactivation.get("unresolved") or []),
        "unmanaged_count": len(deactivation.get("unmanaged") or []),
        "needs_review": deactivation.get("needs_review"),
        "missing_cells": deactivation.get("missing_cells"),
        "by_regime": by_regime,
        "count": len(suggestions),
        SECTION_CHANGE: _change(
            by_regime,
            today_blocked=deactivation.get("skipped") or "",
            # 当轮判定的生效阶段，与上面 `"regime"` 是同一个取值。保命档的进出靠它
            # 认（`deactivation.is_blanket`）——**传进去而不是让 `_change` 自己查**：
            # 同一份日报的两段必须来自同一次判定结果。
            today_regime=judgement_result.get("effective_regime") or "",
            symbol=symbol,
            run_day=run_day,
        ),
    }


def _strategy_names(strategy_ids: list) -> dict[str, str]:
    """建议里只有策略 id，日报要给人看名字。一次查询，不逐条问。"""
    wanted = [sid for sid in strategy_ids if sid]
    if not wanted:
        return {}
    rows = Strategy.objects.filter(id__in=wanted).values_list("id", "name")
    return {str(pk): name for pk, name in rows}


def _change(
    by_regime: dict,
    *,
    today_blocked: str,
    today_regime: str,
    symbol: str,
    run_day: date,
) -> dict:
    """第②段的结构化内容：与**上一份日报**的 `by_regime` 做差。

    为什么读上一份日报而不是「昨天的 Shadow 记录」：`ShadowDailyRecord.suggestions` 是
    那一轮推导的原始产物，而这里的比对要的是「机制**对外承诺过**的清单」——日报是那份
    承诺。两者在正常日子里相同，在推导被阻塞的日子里不同，而正是那种日子里比对最容易
    读错（见下面 `blocked` 的处理）。

    `blocked` 的三态是这段的核心：本轮的推导没产出建议时，今天的集合是**空**的，直接做差
    会把「这轮没说话」读成「把这些全解除了」。同理，上一份日报本身是阻塞日时，它那份空
    集合不代表「那天没有建议」，拿它当基准会凭空造出一堆「将停用」。

    ## 保命档那一层要单独补，而且它不在 `diff_regimes` 的坐标系里

    `by_regime` 的键是**建议清单里的阶段**，而保命档是阶段本身的性质（「生效中的判定就是
    高波动」，`halt_sync`），它在清单里根本没有对应的条目——池化对 `high_vol` 那一格给出
    的是 `blanket`，`deactivation._verdict` 把它映成 `BLANKET`，而 `BLANKET ∉ targets`。
    于是「今天进了保命档」这件事，做差做不出来、也不会以别的方式出现。

    这不能靠「少一行」了事：抬升日上一份日报各层的策略**全部消失**（今天的清单只按新阶段
    产出），只做差会渲染成一片「将解除〈某层〉」——读起来正是「可以交易了」，而事实是
    **此刻谁都不该开新仓**。所以抬升日把那些 `release` 全部丢掉，只补一条保命档的
    `halt`；降级日反过来，补一条 `release`。

    `today_regime` 由调用方从当轮判定带进来（`_landscape` 就在它旁边），**本函数不自己
    去查判定表**：同一份日报里「今日判定」与「相对昨日的变化」必须来自同一次判定结果，
    各查一次迟早会在日界附近各说各话。上一份的则是现成的——`prev.landscape["regime"]`
    就是那天写库时的同一个字段（**取不到就当作不是保命档**，即 `is_blanket("")` 为假）。
    """
    prev = (
        DailyReport.objects.filter(symbol=symbol, run_day__lt=run_day)
        .order_by("-run_day")
        .only("run_day", "landscape")
        .first()
    )

    if today_blocked:
        return {
            "status": "blocked",
            "note": (
                f"本轮推导未产出停用建议（{BLOCKED_DISPLAY.get(today_blocked, today_blocked)}），"
                "故本日不做今昨比对——空集合不等于「全解除」"
            ),
            "baseline_run_day": prev.run_day.isoformat() if prev else None,
            "items": [],
        }
    if prev is None:
        return {
            "status": "first",
            "note": "无昨日日报可比对，这是首次",
            "baseline_run_day": None,
            "items": [],
        }

    prev_landscape = prev.landscape or {}
    prev_by_regime = prev_landscape.get("by_regime") or {}
    if prev_landscape.get("blocked"):
        return {
            "status": "blocked",
            "note": (
                f"上一份日报（{prev.run_day}）那天推导被阻塞"
                f"（{BLOCKED_DISPLAY.get(prev_landscape.get('blocked'), prev_landscape.get('blocked'))}），"
                "它那份清单是空的但不代表「那天没有建议」，故不做比对"
            ),
            "baseline_run_day": prev.run_day.isoformat(),
            "items": [],
        }

    items = diff_regimes(prev_by_regime, by_regime)

    # 保命档那一层的进出（见 docstring）。**这一段排在判空之前**：只有保命档动了的那些天，
    # `items` 做完差正好是空的，先判空就会把「今天全场停手」报成「无变化」——而这两句
    # 对读的人是天差地别的两件事。三条早退路径（本轮阻塞 / 没有上一份 / 上一份阻塞）都
    # 在前面返回了，它们说的是「今天不比」，这里绝不越过它们去比。
    prev_blanket = is_blanket(prev_landscape.get("regime") or "")
    today_blanket = is_blanket(today_regime)
    if today_blanket and not prev_blanket:
        items = [i for i in items if i["kind"] != "release"] + [_blanket_item("halt")]
    elif prev_blanket and not today_blanket:
        items = items + [_blanket_item("release")]

    if not items:
        note = f"与 {prev.run_day} 的日报相比无变化"
        if prev.run_day != run_day - timedelta(days=1):
            note = (
                f"与上一份日报（{prev.run_day}，距今 "
                f"{(run_day - prev.run_day).days} 天）相比无变化"
            )
        return {
            "status": "unchanged",
            "note": note,
            "baseline_run_day": prev.run_day.isoformat(),
            "items": [],
        }
    return {
        "status": "diff",
        "note": f"与上一份日报（{prev.run_day}）相比：{_change_headline(items)}",
        "baseline_run_day": prev.run_day.isoformat(),
        "items": items,
    }


def diff_regimes(prev_by_regime: dict, today_by_regime: dict) -> list[dict]:
    """两天的「层 → 建议集合」做差，返回预告条目（层粒度，CONTEXT.md 第 173 条）。

    - `halt`：今天有、上一份没有 → 「将停用〈层〉」
    - `release`：上一份有、今天没有 → 「将解除〈层〉」

    只在**同一层内**比对策略。跨层搬动一条策略在数据上是「解除 + 停用」两件事，报成一件
    会掩盖「它其实换了个阶段继续被停着」，而那句话正是读的人要的判断。
    """
    items: list[dict] = []
    for regime in sorted(set(prev_by_regime) | set(today_by_regime)):
        prev_ids = {e["strategy_id"] for e in prev_by_regime.get(regime, [])}
        today_entries = today_by_regime.get(regime, [])
        today_ids = {e["strategy_id"] for e in today_entries}
        added = [e for e in today_entries if e["strategy_id"] not in prev_ids]
        removed = [
            e
            for e in prev_by_regime.get(regime, [])
            if e["strategy_id"] not in today_ids
        ]
        if added:
            items.append(_change_item("halt", regime, added))
        if removed:
            items.append(_change_item("release", regime, removed))
    return items


def _change_item(kind: str, regime: str, entries: list[dict]) -> dict:
    return {
        "kind": kind,
        "regime": regime,
        "regime_display": _regime_display(regime),
        "strategies": [
            {
                "strategy_id": e["strategy_id"],
                "name": e.get("name") or "",
                "running": bool(e.get("running")),
                "exempt": bool(e.get("exempt")),
            }
            for e in sorted(entries, key=lambda e: e["strategy_id"])
        ],
    }


def _change_headline(items: list[dict]) -> str:
    parts = [
        f"{_verb_of(item)}{_layer_label(item)}"
        + ("" if item.get("blanket") else f"{len(item['strategies'])} 个")
        for item in items
    ]
    return "；".join(parts)


#: 保命档那一层的层名后缀。它与别的层不是同一种东西：别的层回答「哪个阶段里这批策略
#: 不行」，它回答「此刻谁都不该开新仓」（CONTEXT.md 第 176 条），所以层名后面缀一个
#: 「档」，与 `HaltTrigger.BLANKET` 的展示名（「保命档（高波动）」）同调。
_BLANKET_SUFFIX = "档"

#: 保命档条目的补充说明。措辞取自 `halt_sync._blanket_reason`（那边写的是「保命档不做
#: 适用性判断，与证据无关」），**不在这里另写一句中文**：同一层在日报、`query_halt` 与
#: 那条声明里必须同措辞——两处各写一遍，读的人迟早要面对「说的是不是同一件事」。
_BLANKET_NOTE = "（全市场一律，与证据无关）"


def _blanket_item(kind: str) -> dict:
    """保命档那一层的一条变化。``kind`` 与其余条目共用一套取值（``halt`` / ``release``）。

    ``strategies`` **恒为空**，而且不是「暂时填不上」：保命档做的是适用性判断**之外**的
    判断（「此刻谁都不该开新仓」，与证据无关），它答不出一份策略清单。所以渲染层必须靠
    ``blanket`` 这个标志换一套说法，不能按「条目里没有策略」去猜——那与「这层的策略正好
    全被裁掉」在结构上一模一样，混过去的表现是保命档那一行整条消失。
    """
    regime = BaseRegime.HIGH_VOL.value
    return {
        "kind": kind,
        "regime": regime,
        "regime_display": _regime_display(regime),
        "blanket": True,
        "strategies": [],
    }


def _verb_of(item: dict) -> str:
    """预告口径的动词（CONTEXT.md 第 173 条）。"""
    return "将新停用" if item["kind"] == "halt" else "将解除"


def _layer_label(item: dict) -> str:
    """层名（带书名号）。**层名只有这一处拼**：标题与正文说的必须是同一个层。"""
    suffix = _BLANKET_SUFFIX if item.get("blanket") else ""
    return f"「{item['regime_display']}」{suffix}"


def render_change(change: dict | None, *, strategy_ids: set[str] | None = None) -> str:
    """把结构化第②段渲染成文本，`strategy_ids` 非空时顺带按人裁剪。

    **裁剪发生在渲染之前**：`strategy_ids=None` 表示「不裁」（存档那一份、以及日志）；
    传一个集合表示「只留与这些策略有关的条目」（某个用户那一份）。筛完没有条目时返回
    **空串**，由调用方决定连标题一起省掉——无活跃会话的人不该看到这一节。

    保命档那条**不按策略裁**（它没有策略清单可裁，`_blanket_item`），但它不是段可见性的
    例外：`strategy_ids` 是空集时连它也一起省掉。空集的意思是「机制压根没在管这个人」，
    而「全市场一律」正是最该被那句话挡住的——给一个没有活跃会话的人推「全场将停手」，
    下一次他就会开始忽略日报。

    取的是「预告口径」而不是陈述口径（CONTEXT.md 第 173 条）：这些变化**将在明日 08:00
    生效**，所以写「将停用」，不写「已停用」。此刻被停的与这里写的不是同一批，混起来
    就正好是本机制从头到尾在防的那种「两个入口两种说法」。
    """
    if not change:
        return ""
    status = change.get("status")
    if status in ("first", "blocked", "unchanged"):
        return change.get("note") or ""

    lines: list[str] = []
    for item in change.get("items") or []:
        blanket = bool(item.get("blanket"))
        if blanket and strategy_ids is not None and not strategy_ids:
            continue
        entries = item.get("strategies") or []
        if strategy_ids is not None:
            entries = [e for e in entries if e.get("strategy_id") in strategy_ids]
        if not entries and not blanket:
            continue
        line = f"{_verb_of(item)}{_layer_label(item)}"
        if blanket:
            lines.append(line + _BLANKET_NOTE)
            continue
        names = "、".join(_entry_label(e) for e in entries)
        lines.append(f"{line}：{names}")
    if not lines:
        return ""
    lines.append("（以上自明日 08:00 起生效）")
    return "\n".join(lines)


def _entry_label(entry: dict) -> str:
    name = entry.get("name") or entry.get("strategy_id", "")[:8]
    marks = []
    if entry.get("exempt"):
        marks.append("人工豁免中")
    if entry.get("running") is False:
        marks.append("未在跑")
    return f"{name}（{'，'.join(marks)}）" if marks else name


# --------------------------------------------------------------------------- #
# 第三段：未来 N 天的高影响事件
# --------------------------------------------------------------------------- #


def _section_events(*, now: datetime) -> str:
    """第③段。查询形状与 `apps.agent.event_commands._list` **逐字同源**：两处回答的是
    同一个问题，口径不同就会出现「日报说有 2 条、追问时工具说有 5 条」。

    天数取 `config.REPORT.event_horizon_days`，**不吃参数**（CONTEXT.md 第 183 条）。
    起点是 `now` 而不是运行日：同源那一份用的是 `now`，两处口径必须逐字一致。
    """
    horizon = config.REPORT.event_horizon_days
    until = now + timedelta(days=horizon)
    lines = [f"未来 {horizon} 天（截至 {format_business(until)}）："]

    upcoming = list(
        MajorEvent.objects.filter(
            status=EventStatus.SCHEDULED.value,
            event_time__lte=until,
            resume_at__gte=now,
        ).order_by("event_time", "id")
    )
    high = [e for e in upcoming if e.impact == EventImpact.HIGH.value]
    if high:
        lines.append(f"会触发熔断的（档位「高」）：{len(high)} 条")
        lines.extend(describe_event(event, now=now) for event in high)
    else:
        lines.append("会触发熔断的（档位「高」）：没有")

    others = len(upcoming) - len(high)
    if others:
        # 不静默略过：录进来的事件在日报里查不到，人会以为系统把那条吃了。
        lines.append(
            f"另有 {others} 条档位非「高」的事件不产生熔断，用 /event list 查看。"
        )

    lines.extend(_candidate_lines(now=now))
    return "\n".join(lines)


def _candidate_lines(*, now: datetime) -> list[str]:
    """候选事件单列一节，落在第③段末尾。

    候选**不产生任何动作**，转正需人工重新给出时间/档位/作用域（CONTEXT.md 第 94 条）。
    `describe_candidate` 与 `describe_event` 结构上可分辨（候选没有「熔断窗口」那一行），
    所以这里不必再写一遍「这不是熔断」——但一行总括要说，因为日报是许多人第一次听说
    「候选」这个词的地方。

    两半**必须成对存在**：待处置的（要人动手）与到期被丢掉的（机制提过、没人处置的证据，
    CONTEXT.md 第 152 条）。少了后一半，清理一跑起来过期候选就从这一节上整个消失——
    连「它曾经存在过」都不会有人知道。
    """
    pending = list(
        CandidateEvent.objects.filter(status=CandidateStatus.PENDING.value).order_by(
            "-raised_at", "-id"
        )
    )
    if pending:
        lines = [
            f"待人工处置的候选事件：{len(pending)} 条（**不产生任何熔断**，"
            "转正需人工重新给出时间/档位/作用域）",
            *(describe_candidate(candidate, now=now) for candidate in pending),
        ]
    else:
        lines = ["待人工处置的候选事件：没有"]
    lines.extend(_dropped_candidate_lines(now=now))
    return lines


#: 「到期被丢弃」那一段回看多少天。**这是日报的一个展示窗口，不是机制参数**，所以它是本
#: 文件的常量而不进 `config.GROUPS`：那一组会被一起调整、一起审计，还会被
#: `config.full_snapshot()` 内嵌进每一条判定记录（`config_snapshot`）——把一个展示窗口
#: 塞进去，会让日后读判定记录的人以为它与判定有关。
#:
#: 也**刻意不绑** `EVENTS.candidate_expiry_days` 与 `EVENTS.coverage_decay_days`：那两个 14
#: 数的是「一条候选能活多久」与「多久无新事件入库要提醒」，`config.EVENTS` 的 docstring
#: 自己写着它们「将来也会各自漂开」。再让第三个用途绑上去，就是让三处一起漂。
_DROPPED_WINDOW_DAYS = 14


def _dropped_candidate_lines(*, now: datetime) -> list[str]:
    """到期未确认、已被自动丢弃的候选（第 152 条那条「覆盖率衰减」的证据）。

    **只在这一期真有人被丢时才印**，一行都不印不算沉默：「清理动作还活着吗」这个问题在
    体检页的「调度表（beat）」那一段有唯一的家（`beat_health.py`），在日报里再补一句
    「近 14 天没有丢弃」就是给同一个问题造第二个答案。

    逐条一行而不是 `describe_candidate` 那种多行块：这是要一眼扫过的证据清单，不是要人
    动手的待办。**「提出于」必须带上**——窗口按丢弃时刻算，于是「三个月前提出、今天才被
    丢」（清理坏了一阵子，第一次跑就是这种形状）会被印得像最近的衰减。

    行里**不印失效时刻**：它是 `提出日 + 14 天` 的算术结果，而这段文字要说的是「多久没人
    理」，不是「在哪一秒被丢」。失效期本身在总括与 `describe_candidate` 里各说了一次，
    再往每行塞一遍只会让这份清单变长。
    """
    since = now - timedelta(days=_DROPPED_WINDOW_DAYS)
    dropped = list(
        CandidateEvent.objects.filter(
            status=CandidateStatus.DISCARDED.value,
            discard_reason=CANDIDATE_DISCARD_EXPIRED,
            decided_at__gte=since,
        ).order_by("-decided_at", "-id")
    )
    if not dropped:
        return []
    return [
        f"到期未确认而自动丢弃（近 {_DROPPED_WINDOW_DAYS} 天）：{len(dropped)} 条"
        "——机制提过、没人处置",
        *(
            f"候选 #{candidate.id}「{candidate.name}」（{candidate.raised_at} 提出）"
            for candidate in dropped
        ),
    ]


# --------------------------------------------------------------------------- #
# 第四段：机制健康
# --------------------------------------------------------------------------- #


def _section_health(
    symbol: str,
    judgement_result: dict,
    deactivation: dict,
    *,
    now: datetime,
) -> str:
    """第④段：上次判定成功时间 + 三个开关 + 停止声明表此刻的实数 + 是否触发过自熔断
    + 未归类策略计数。

    「上次判定成功时间」的唯一出处是 `judgement.last_judgement()`——**最近写下的那条，
    不论生效了没有**。不能拿 `current_judgement().created_at` 顶替：那读的是「正在生效的
    那条是什么时候写的」，而它对「机制多久没出结论了」这个问题给的是错的答案（详见
    `apps.regime.judgement.last_judgement` 的 docstring）。
    """
    lines: list[str] = []

    last = judgement.last_judgement(symbol)
    if last is None:
        lines.append("上次判定成功：从未（判定任务没有写完过任何一条记录）")
    else:
        lines.append(
            f"上次判定成功：{format_business(last.created_at)}"
            f"（运行日 {last.run_day}，结论 {_regime_display(last.effective_regime)}）"
        )

    lines.extend(_switch_lines(now=now))
    lines.extend(_declaration_lines(now=now, regime=deactivation.get("regime")))

    # 「是否触发过自熔断」只能由切换流水回答：自熔断的收场是「退回 Shadow」，那条路径会在
    # 流水里留一条 to_mode=shadow 的行。这里只读，不推断。
    #
    # **必须限定 `kind`（第②段 Q4 之后）**：加这一列之前只有「机制整体」一种语义，所以
    # 「有一条退回 Shadow 的行」与「机制自熔断过」是同一句话。加列之后就不同了——人工把
    # 事件熔断那个开关关掉同样是一条 to_mode=shadow 的行，而那不是自熔断（自熔断说的是
    # 机制整体不可信）。少这一条限定，日报会把一次例行的人工关开关报成自熔断。
    #
    # **还要限定 `actor_kind`**：`/regime mech back` 让人能自己把机制退回 Shadow，而那一条
    # 也是 to_mode=shadow。不加这一层，一次人工退回会被日报报成「自熔断触发过」——比上面
    # 那条更坏，因为自熔断是「机制不可信」的结论，而它接下来会被当成重新上线的依据。
    # 人工退回不是没发生：它由 `_switch_lines` 的「机制当前档（最近一次切换 …）」那一行
    # 报出来，这里只回答「**自**熔断有没有触发过」。
    back = [
        row
        for row in RegimeMechanismSwitch.objects.filter(
            kind=MechanismKind.MECHANISM.value,
            to_mode=MechanismMode.SHADOW.value,
        ).order_by("-at", "-id")[:1]
    ]
    if back and back[0].actor_kind == ActorKind.TASK.value:
        lines.append(
            f"自熔断：触发过（最近一次 {format_business(back[0].at)}，"
            f"{back[0].actor_name}：{back[0].reason}）"
        )
    elif back:
        # 有人退回过、但不是自熔断。说「未触发过」而不说「没有退回记录」：后一句在这里
        # 是假话（流水里确实有一条退回 Shadow 的行），而日报上的假话正是这一轮要清掉的东西。
        lines.append(
            f"自熔断：未触发过（最近一次退回 Shadow 是**人工**：{back[0].actor_name}，"
            f"{format_business(back[0].at)}）"
        )
    else:
        lines.append("自熔断：未触发过（切换流水里没有一条退回 Shadow 的记录）")

    if judgement_result.get("base_regime"):
        lines.append("本轮判定：已出结论")
    else:
        skipped = judgement_result.get("skipped") or ""
        lines.append(
            f"本轮判定：未出结论（{_SKIP_DISPLAY.get(skipped, skipped or '未说明')}）"
        )
    derivation = deactivation.get("skipped") or ""
    lines.append(
        "本轮推导："
        + (BLOCKED_DISPLAY.get(derivation, derivation) or "正常，已产出建议")
    )
    note = (deactivation.get("note") or "").strip()
    if note and note != BLOCKED_DISPLAY.get(derivation, ""):
        lines.append(f"  推导备注：{note}")

    managed = deactivation.get("managed")
    if managed is not None:
        text = f"被管策略：{managed} 个（其中在跑 {deactivation.get('running')} 个）"
        unmanaged = deactivation.get("unmanaged") or []
        if unmanaged:
            # 「未归类」的幽灵策略行是回测自动建出来的，日报只给它们一个数，不给动作
            # （CONTEXT.md 第 105 条）。数要报，否则「被管集合怎么会变」无处可问。
            text += f"；未归类 {len(unmanaged)} 个（不产生任何动作）"
        unresolved = deactivation.get("unresolved") or []
        if unresolved:
            text += f"；实现类解析不到 {len(unresolved)} 个"
        lines.append(text)
        # 「结论在、动作不在」的那些也要报数：`needs_review` 判为不适用但依据方向冲突，
        # 于是它既不在建议清单里、也不该被当成「这轮没事」。不报，它就只存在于任务日志里，
        # 而日志不算被看见。
        review = deactivation.get("needs_review")
        if review:
            lines.append(
                f"  待人工复核：{review} 条（判为不适用但依据方向冲突，本轮不动手）"
            )
        missing = deactivation.get("missing_cells")
        if missing:
            lines.append(
                f"  当前代缺格：{missing} 条（策略 × 阶段在池化表里没有这一格）"
            )

    lines.append(event_library_line(now=now))
    return "\n".join(lines)


# 三个开关，按「机制整体 → 事件熔断 → 行情阶段 gate」的顺序列出。顺序固定：日报是逐日
# 对照读的，顺序一变，昨天的第 2 行与今天的第 2 行就不是同一件事。
_SWITCH_KINDS = (
    MechanismKind.MECHANISM,
    MechanismKind.EVENT_BREAKER,
    MechanismKind.REGIME_GATE,
)


def _switch_lines(*, now: datetime) -> list[str]:
    """三个开关各自的当前档 + 各自最近一次生效时间（CONTEXT.md 第 179 条）。

    「各自最近一次生效时间」在第②段之前答不出来——那张表没有「是哪一个开关」这一列，
    而当时又一行都不写，所以只能如实说「无流水可查」。第②段给
    `RegimeMechanismSwitch` 加了 `kind`（Q4），三个开关从此各查各的流水。

    每一档都走 `current(kind)` 而不是写死 Shadow：开关一旦被人打开（②f 的上线确认
    入口），这句话要跟着变——写死的话，命令落了库而日报仍报 Shadow，两边都「正常」。

    末一行说的是**保命档不在三个开关里**：它是阶段本身的性质（高波动一到就生效），
    没有哪个人点头才让它生效（CONTEXT.md 第 179 条的三个开关对应的是三条**自动行为**，
    而保命档不需要被「启用」）。不写这一行，读日报的人会以为高波动档也要等某个开关。

    「出 Shadow 到期」那一行只**在到期之后**出现（`mechanism_switch.expiry_notice`，
    第 159 条）：到期前天天报一句「还没到期」是二十天的噪声，会训练人跳过第④段，而这条
    兜底要防的恰恰是「没人管的库让 Shadow 无限期挂着、挂着的样子与正常跑着一样」。
    位置紧跟在「机制当前档」下面：它说的是**那一档**的事，离了半个屏幕就会被读成别的东西。
    """
    lines: list[str] = []

    master = RegimeMechanismSwitch.latest(MechanismKind.MECHANISM)
    lines.append(
        f"机制当前档：{RegimeMechanismSwitch.current(MechanismKind.MECHANISM).display}"
        + (
            f"（最近一次切换 {format_business(master.at)}）"
            if master
            else "（无切换流水）"
        )
    )
    notice = mechanism_switch.expiry_notice(now=now)
    if notice is not None:
        lines.append(notice)

    lines.append("三个开关（各自独立、各自人工确认）：")
    for kind in _SWITCH_KINDS:
        row = RegimeMechanismSwitch.latest(kind)
        lines.append(
            f"  {kind.display}：{RegimeMechanismSwitch.current(kind).display}"
            + (
                f"（最近一次生效 {format_business(row.at)}）"
                if row
                else "（无切换流水）"
            )
        )
    lines.append(
        "  保命档（高波动）：不在这三个开关里——高波动一到就生效，不需要人工确认。"
    )
    return lines


def _declaration_lines(*, now: datetime, regime: object) -> list[str]:
    """停止声明表**此刻**的实数（第③段 W4）。

    **这一行说的是「此刻」，不是今天那条判定的效果**：声明表吃的是**生效中**的判定
    （`judgement.current_judgement`），而今天 08:00 刚产出、明日 08:00 才生效的那一条此刻
    还没咬人。本段上面那行「上次判定成功（结论 …）」报的正是**待生效**的那条——两个阶段名
    会不一样，所以这一行必须把「按生效中的阶段」写出来，否则读的人只能在两句里挑一句信。

    `blocking_declarations` 而不是只读表：它是**开关感知**的，也就是「此刻真的在拦什么」
    在本仓的唯一答案（`query_halt` 与下单通路都从它出发）。少了这一层过滤，Shadow 期会把
    「表里躺着、开开关就咬人」的行报成已经拦住下单。两个数的差额**只在不为零时**说出来
    ——正常一轮它们相等，而每天印一句「另有 0 条」会让那一句变成背景噪声。

    **只报层名与条数，不报作用域**（行级明细在即时消息与 `/regime gate` 里，那里用的是
    `HaltLayer.text`）：第④段要回答的是「机制此刻在不在拦、拦在哪儿」，一屏作用域会把
    这个要点埋掉；至于「停的是不是我的策略」，那是第②段按人裁剪之后的职责。

    层名一律走 `HaltTrigger.display`（与拒绝理由、`query_halt` 同源），顺序按枚举定义的
    顺序——日报是逐日对照读的，顺序一变，昨天这一行与今天这一行就不是同一件事。
    """
    blocking = halt.blocking_declarations(now=now)
    live = halt.live_declarations(now=now)

    source = (
        f"按生效中的阶段：{_regime_display(regime)}"
        if regime
        else "此刻没有生效中的阶段判定"
    )
    if not blocking:
        lines = [f"停止声明：此刻没有任何一层在拦（{source}）"]
    else:
        counts: list[str] = []
        for trigger in HaltTrigger:
            count = sum(1 for row in blocking if halt.trigger_of(row) == trigger)
            if count:
                counts.append(f"{trigger.display} {count} 条")
        lines = [
            f"停止声明：此刻 {len(blocking)} 条在拦（{'、'.join(counts)}；{source}）"
        ]

    if len(live) > len(blocking):
        lines.append(f"  另有 {len(live) - len(blocking)} 条活着，开关关着所以不拦")
    return lines


def event_library_staleness(*, now: datetime) -> tuple[datetime | None, int | None]:
    """事件库最近一次入库的时刻与距今天数。**从未录入过任何事件返回 `(None, None)`**。

    与 `event_library_line` 拆开（第②段 ②f）：日报要的是「那一刻 + 多少天」这一整句话，
    而事件熔断开关的 `reason` 只要那个天数。两个数必须同源——各算一遍就是给「事件库有
    多陈旧」造第二个答案，而两边不一致时读的人分不出该信哪个（这与那条让确认页复用
    `event_library_line` 的理由是同一条）。

    两条来源取更晚的那一条：候选事件也是情报，人不处置不代表它没入库。
    """
    newest = MajorEvent.objects.order_by("-created_at").only("created_at").first()
    candidate = (
        CandidateEvent.objects.order_by("-created_at").only("created_at").first()
    )
    stamps = [row.created_at for row in (newest, candidate) if row is not None]
    if not stamps:
        return None, None
    latest = max(stamps)
    return latest, (now - latest).days


def event_library_line(*, now: datetime) -> str:
    """事件库衰减（CONTEXT.md 第 80 条：超过 14 天无新事件入库要告警）。

    放在第④段而不是第③段：这是**机制自身的健康**信号，不是「未来有什么事件」。两处都写
    会让同一件事有两个出口，而它们迟早会不一致。

    **公开**（第②段 ②f）：事件熔断的上线确认页要回显同一句话。空库上线等于熔断永久
    空转，而空转的样子与正常运行完全一样（CONTEXT.md 第 169 条）——所以「最近入库距今
    几天」在日报与确认页里必须是同一个算法算出来的，另写一份就是给「事件库有多陈旧」
    造第二个答案，而两边不一致时读的人分不出该信哪个。
    """
    limit = config.EVENTS.coverage_decay_days
    latest, age = event_library_staleness(now=now)
    if latest is None:
        return f"事件库：从未录入过任何事件（上限 {limit} 天无新事件即衰减告警）"
    text = (
        f"事件库：最近入库 {format_business(latest)}（距今 {age} 天，上限 {limit} 天）"
    )
    if age > limit:
        text += "——⚠ 已超过上限，事件情报可能已经衰减"
    return text


# --------------------------------------------------------------------------- #
# 第五段：昨日日报的投递结果
# --------------------------------------------------------------------------- #


def _section_delivery(symbol: str, run_day: date) -> str:
    """第⑤段：昨日日报的投递结果。

    这一段的全部意义是让「机制昨晚哑了一回」第二次出现时有人能看见——所以它读的是**昨天
    那一行自己**，不是「今天有没有收到」。缺昨日那一行时照实说缺，并点出「缺」本身意味着
    什么：判定任务昨天也跑过而这里空着，那是生成环节的问题，不是正常。

    **它是回复式的，这正是看门狗存在的理由**（CONTEXT.md:175）：本段说「昨天那份没投
    出去」，只有在**今天这一份投得出去**时才说得到人。明天也失败，沉默就自我延续——
    没有任何东西会响。所以本段照实报，但**不承担**发现连续失败的职责：那是
    `check_report_delivery` 的事，它读同一张表的 `delivered_at`，不等这一段的文字。

    空集受众（一个 `is_active` 用户都没有）按「没投出去」记，不按「投出去了」记——
    把空集当成功会让本段永远报「已投递」，而它恰恰是一份永远显示成功的投递报告。
    """
    prev_day = run_day - timedelta(days=1)
    prev = (
        DailyReport.objects.filter(symbol=symbol, run_day=prev_day)
        .only(
            "created_at",
            "delivered_at",
            "delivery_attempts",
            "delivery_error",
            "delivery",
        )
        .first()
    )
    if prev is None:
        return (
            f"昨日（{prev_day}）没有日报可查。"
            "若昨天的判定任务跑过，这本身就是生成环节的问题，不是正常状态。"
        )
    results = prev.delivery or {}
    if prev.delivered_at is not None:
        return (
            f"昨日日报生成于 {format_business(prev.created_at)}，"
            f"投递成功于 {format_business(prev.delivered_at)}"
            f"（共 {len(results)} 人，用了 {prev.delivery_attempts} 轮）。"
        )
    if not results:
        return (
            f"昨日日报生成于 {format_business(prev.created_at)}，"
            "但没有 is_active 用户，无处可投——按「没投出去」记，不按「投出去了」记。"
        )
    return (
        f"昨日日报生成于 {format_business(prev.created_at)}，**至今未投递成功**"
        f"（共 {len(results)} 人，已尝试 {prev.delivery_attempts} 轮）。"
        f"最近一次失败：{prev.delivery_error or '未说明'}。"
        "这一条已由独立的投递看门狗在昨日截止时刻升级告警，不等本段——"
        "本段只在「今天这份投得出去」时才说得到人。"
    )


# --------------------------------------------------------------------------- #
# 渲染与裁剪
# --------------------------------------------------------------------------- #


def render_body(sections: dict, *, symbol: str, run_day: date) -> str:
    """把五段拼成一份正文。落库的 `sections` 是分段的那一份，正文是它的渲染结果。

    空段照样出标题并写「（本节无内容）」：**缺一节与「这一节没有内容」必须能被区分**，
    否则读者不知道是机制没话说还是渲染漏了。
    """
    horizon = config.REPORT.event_horizon_days
    lines = [f"行情阶段日报 ｜ {symbol} ｜ 运行日 {run_day}"]
    for name in SECTIONS:
        if name == SECTION_CHANGE:
            title = _SECTION_TITLES[name]
            body = (sections or {}).get(name) or ""
            if not body:
                # 无活跃会话的用户不该看到停用段——**整节不出现**，不留空标题。
                continue
        else:
            title = _SECTION_TITLES[name].format(horizon=horizon)
            body = (sections or {}).get(name) or "（本节无内容）"
        lines.extend(["", f"【{title}】", body])
    return "\n".join(lines)


def crop_for(report: DailyReport, strategy_ids: set[str]) -> dict:
    """某一行的五段裁成某个用户该看的样子，返回**新的 `sections`**。

    判定段（以及③④⑤）对所有人相同——判定是有用的公共信息。停用段只含「你实际受影响的
    策略」（CONTEXT.md 第 173 条）：一个你没在跑的策略被停了纯属噪声，而噪声会让人开始
    忽略日报，那时「必发」就只剩形式。

    一个**空集合**就是「这个人没有任何活跃会话」——那时整节都不出现（`render_change` 返回
    空串，这里把键删掉），而**不是**出现一节写着「无变化」：后者会被读成「机制看了我的
    策略，说没事」，而事实是机制压根没在管他。所以不需要第二个「要不要这一段」的开关，
    「筛完为空就整节不出现」这一条规则同时覆盖了这两种情形。
    """
    sections = dict(report.sections or {})
    change = (report.landscape or {}).get(SECTION_CHANGE)
    render = render_change(change, strategy_ids=set(strategy_ids or ()))
    if render:
        sections[SECTION_CHANGE] = render
    else:
        sections.pop(SECTION_CHANGE, None)
    return sections


def user_affected_strategy_ids(user_id) -> set[str]:
    """这个用户**实际在跑**的策略 id。

    裁的是活跃会话（`LiveSession.ACTIVE_STATUSES`），不是策略的 `is_active`：后者是
    「这个策略退役了没有」的人工总开关（CONTEXT.md 第 106 条），与「谁在跑它」是两件事。
    """
    return {
        str(sid)
        for sid in LiveSession.objects.filter(
            user_id=user_id,
            status__in=LiveSession.ACTIVE_STATUSES,
        )
        .values_list("strategy_id", flat=True)
        .distinct()
    }


# --------------------------------------------------------------------------- #
# 投递（第①段单元 8iv）
# --------------------------------------------------------------------------- #

#: 逐人明细里 `ok=False` 时那句 `error`。失败只有一种可观测的形状（出站口返回未送达），
#: 所以措辞也只有这一种——把它写宽（「网络错误」之类）就是替出站口猜原因。
_DELIVERY_FAILED = "出站通知口返回未送达（推送通路失败，或接收人为空）"

#: 看门狗比日报截止时刻晚多久才动手。**三个心跳**（`snapshot-daily-equity` 五分钟一轮）：
#:
#: 截止那一刻心跳才**被允许**写日报（`_ready` 判的是 `now >= deadline`），写完还要等
#: 下一轮投递任务才送出去，而投递任务与心跳是两条独立的 beat 条目、先后不保证——所以
#: 「到点」之后至少有两个写入者要走，留三个心跳的余量。同刻去判「没投出去」等于拿
#: 看门狗去抢它盯的那个写入者，而抢跑制造的是**每天必然出现**的假告警；假告警的真实
#: 代价是真告警跟着一起被忽略。
#:
#: 晚三个心跳在这里不损失什么：「今天这份日报有没有送到」等到 09:15 与等到 09:00 没有
#: 区别，那是一条日频结论，不是一条行情信号。
#:
#: **它与 beat 的间隔是一对**：余量必须大于投递任务与心跳两条 beat 条目各自的间隔之和。
#: `test_delivery.py` 钉着这一条——把 beat 调密不违例，调稀就会违例。
WATCHDOG_GRACE = timedelta(minutes=15)

#: 同一运行日只**成功**告警一次。与 `daily_snapshot._alerted_on` 同一条理由：本任务
#: 五分钟一轮，日报持续投不出去时逐轮告警会变成骚扰，而骚扰的结果是用户把通知静音——
#: 那又回到「沉默」了。只在**真的有人被通知到**时才记账（同那处的取舍）：没人听见的
#: 喊话不算喊过，下一轮还得喊。进程重启会让这本账清零，代价是重新喊一遍，可接受。
_alerted_on: dict[date, bool] = {}


def deliver_daily_report(
    run_day: date | str | None = None, *, now: datetime | None = None
) -> dict:
    """把今天那一份日报投给每个 `is_active` 用户，回一行摘要。**幂等**。

    投递口是 ``apps.trading.alerts.notify_user``——本仓库唯一的出站通知口，
    **不新增第二个投递机制**（CONTEXT.md:175「不新建告警系统」）。正文按人裁剪
    （`crop_for` + `user_affected_strategy_ids`）：同一份 `sections` 对每个人渲染出不同
    正文，这正是正文不落库的原因（见模型 docstring）。

    投递记录落回 `DailyReport` 自己的那一行（三列标量 + `delivery` 明细），理由与不变式
    写在模型 docstring 里；这里只负责**算一次**：`delivered_at` 非空 ⟺ 明细里至少有一格
    且每格都 `ok`。

    **已成功的人不重投**：明细是只增不改的账。同一个人重投一份日报是纯骚扰，而骚扰的
    结果是他不再看日报——那正是「必发」要避免的。

    **入参是运行日而不是日报摘要**：要投的那一份按 `(symbol, run_day)` 现查。这样投递
    与「日报是谁、以什么形状写出来的」解耦——投递任务与看门狗因此可以共用一个签名，
    而摘要形状将来变了也不会把投递带塌。

    `now` 只在置 `delivered_at` 时用一次，其余判断都用库里的状态，所以重入安全。
    """
    now = now or timezone.now()
    day = _as_run_day(run_day, now)
    symbol = judgement.SYMBOL

    row = DailyReport.objects.filter(symbol=symbol, run_day=day).first()
    if row is None:
        return {
            "symbol": symbol,
            "run_day": day.isoformat(),
            "attempted": False,
            "reason": "no_report",
            "delivered": False,
            "note": "今天这一份还没写出来（判定没结论，且未到截止时刻）",
        }
    if row.delivered_at is not None:
        return _delivery_summary(row, attempted=False, reason="already_delivered")

    # 正文在这里**同步**渲染好，只把「发」那一步交给 async：裁剪要查活跃会话，那是裸的
    # 同步查询，不能出现在 `async def` 里（CLAUDE.md：绝不在 async 上下文里写裸的同步
    # 数据库查询）。渲染也不该放进去——它是 DB 查询，不是 IO。
    pending = [
        (str(user.pk), _body_for(row, user.pk, symbol, day))
        for user in get_user_model().objects.filter(is_active=True)
        if not (row.delivery or {}).get(str(user.pk), {}).get("ok")
    ]
    results = dict(row.delivery or {})
    if pending:
        results.update(asyncio.run(_send_to_each(pending)))

    row.delivery = results
    if pending:
        # 「轮」= 「真的投了一轮」。没有受众时这一列不动——把「无处可投」记成第 288 轮
        # 会让第⑤段那句「已尝试 N 轮」变成一句自己都解释不了的数字。
        row.delivery_attempts += 1
    row.delivery_error = _delivery_error(results)
    if results and all(entry.get("ok") for entry in results.values()):
        row.delivered_at = now
    row.save(
        update_fields=[
            "delivery",
            "delivery_attempts",
            "delivery_error",
            "delivered_at",
        ]
    )
    outcome = _delivery_summary(row, attempted=bool(pending), reason="")
    logger.info("[regime] 日报投递 %s", outcome)
    return outcome


def _as_run_day(run_day: date | str | None, now: datetime) -> date:
    """运行日：给了就用，没给就按业务时区的自然日算。

    「没给」是 beat 的常态——投递任务与看门狗都不持有运行日，它们只知道「现在」。
    """
    if run_day:
        return date.fromisoformat(run_day) if isinstance(run_day, str) else run_day
    return _business_day(now)


def _body_for(row: DailyReport, user_id, symbol: str, run_day: date) -> str:
    """这一份日报对**这个人**的正文。逐人不同，所以不落库。"""
    return render_body(
        crop_for(row, user_affected_strategy_ids(user_id)),
        symbol=symbol,
        run_day=run_day,
    )


async def _send_to_each(pending: list[tuple[str, str]]) -> dict:
    """逐人投一遍，返回**本轮新投的**那些格子。

    收的是一串 `(user_id, 正文)`——渲染已经在同步世界里做完，这里只剩等待，所以不会
    在 async 上下文里碰数据库。
    """
    from apps.trading.alerts import notify_user

    sent: dict[str, dict] = {}
    for user_id, body in pending:
        ok = bool(await notify_user(user_id, body))
        sent[user_id] = {
            "ok": ok,
            "at": timezone.now().isoformat(),
            "error": "" if ok else _DELIVERY_FAILED,
        }
    return sent


def _delivery_error(results: dict) -> str:
    """最近一轮的失败，一句话。空 = 全部送达。"""
    if not results:
        return "没有 is_active 用户，这份日报无处可投"
    failed = [key for key, entry in results.items() if not entry.get("ok")]
    if not failed:
        return ""
    return (
        f"{len(failed)}/{len(results)} 人未送达："
        f"{results[failed[0]].get('error') or '未说明'}"
    )


def _delivery_summary(row: DailyReport, *, attempted: bool, reason: str = "") -> dict:
    results = row.delivery or {}
    return {
        "symbol": row.symbol,
        "run_day": row.run_day.isoformat(),
        "attempted": attempted,
        "reason": reason,
        "targets": len(results),
        "delivered_count": sum(1 for e in results.values() if e.get("ok")),
        "attempts": row.delivery_attempts,
        "delivered": row.delivered_at is not None,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        "error": row.delivery_error,
        "note": row.delivery_error or "全部送达",
    }


# --------------------------------------------------------------------------- #
# 投递看门狗（第①段单元 8iv）
# --------------------------------------------------------------------------- #


def check_report_delivery(
    run_day: date | str | None = None, *, now: datetime | None = None
) -> dict:
    """投递看门狗：当天日报到点还没投出去，就升级告警。**只读，什么都不写回。**

    ## 为什么它必须独立于日报

    第⑤段的自报是**回复式**的（下一条日报里说上一条坏了），它对「连续失败」完全无用，
    而连续失败恰恰是它最该发现的形态——明天也失败，沉默就自我延续，没有任何东西会响。
    所以这条路径读的是 `DailyReport` 表本身（`delivered_at`），**不是日报自己发了什么**
    （CONTEXT.md:175）。两条路径因此互相独立：一条坏了，另一条还在。

    ## 它与「新任务不自己发告警」不冲突

    那条纪律管的是**任务自己的故障**（要往上抛）；这里要说的是一件
    **关于世界的事实**（今天的日报没到），本任务本身没有出错。所以：本任务自己的异常
    照旧往上抛（`apps.regime.tasks` 里那层不吞），而这条升级消息走已有的出站通知口
    （`notify_user`），没新造告警系统。

    ## 它自己哑掉怎么办

    beat 任务的死活目前没有第二个观察者——这是 CONTEXT.md:180 承认的那条缝。它靠另一条
    路径兜底：日报本身每天必到，用户**收到**日报就是「投递通路还活着」的日常证据，而
    看门狗只在日报没到时才响。「看门狗与日报同时哑」只有第⑤段（下一天）能说出来。

    ## 「今天」与「到点」的口径

    运行日按**业务时区**的自然日取（`_business_day`），「到点」= 日报截止时刻
    (`_deadline`) + `WATCHDOG_GRACE`。两者都从 `config.REPORT` 的同一对字段来，
    所以改配置不会只改到一处。

    **不用 crontab 表达式定时**：本仓 Celery 的 `TIME_ZONE` / `CELERY_TIMEZONE` 都是
    UTC，`crontab(hour=9)` 会在北京 17:00 触发，而把北京钟点翻译成 UTC 表达式等于把
    「09:00 是给人看的钟点」这条口径复制到第二个地方。改成固定间隔轮询 + 在函数里按
    业务时区判时刻，口径就只有一处。
    """
    now = now or timezone.now()
    day = _as_run_day(run_day, now)
    moment = _deadline(day) + WATCHDOG_GRACE
    if now < moment:
        return {
            "checked": False,
            "escalated": False,
            "reason": "before_deadline",
            "run_day": day.isoformat(),
            "note": f"未到 {format_business(moment)}，本轮不判",
        }

    row = (
        DailyReport.objects.filter(symbol=judgement.SYMBOL, run_day=day)
        .only(
            "symbol",
            "run_day",
            "created_at",
            "delivered_at",
            "delivery_attempts",
            "delivery_error",
        )
        .first()
    )
    if row is not None and row.delivered_at is not None:
        return {
            "checked": True,
            "escalated": False,
            "reason": "delivered",
            "run_day": day.isoformat(),
            "note": f"已投递（{format_business(row.delivered_at)}）",
        }

    users = list(get_user_model().objects.filter(is_active=True))
    if not users:
        # 「告警」是给具体某个人的即时消息。没有人可给时它就不是一条告警，
        # 也不是一个该被记进 `_alerted_on` 的日子——将来有人了还得能响。
        return {
            "checked": True,
            "escalated": False,
            "reason": "no_recipients",
            "run_day": day.isoformat(),
            "note": "没有 is_active 用户，这条消息无人可给",
        }
    if _alerted_on.get(day):
        return {
            "checked": True,
            "escalated": False,
            "reason": "already_alerted",
            "run_day": day.isoformat(),
            "note": "今日已成功告警过一次，不重复",
        }

    text = _escalation_text(row, day, moment)
    delivered = asyncio.run(_alert_each([str(u.pk) for u in users], text))
    if any(delivered):
        _alerted_on[day] = True
    logger.warning(
        "[regime] 日报投递看门狗升级告警（%s 人，送达 %s）", len(users), sum(delivered)
    )
    return {
        "checked": True,
        "escalated": True,
        "reason": "no_report" if row is None else "undelivered",
        "run_day": day.isoformat(),
        "recipients": len(users),
        "delivered_count": sum(1 for d in delivered if d),
        "note": text,
    }


async def _alert_each(user_ids: list[str], text: str) -> list[bool]:
    """逐个投同一句话。**没有裁剪**——这条消息对所有人一样，它说的是机制本身的状态，
    不是「你的策略怎么了」。"""
    from apps.trading.alerts import notify_user

    return [bool(await notify_user(user_id, text)) for user_id in user_ids]


def _escalation_text(row: DailyReport | None, run_day: date, moment: datetime) -> str:
    """升级告警的正文。两种收场要分得开——「没生成」与「生成了没投出去」是两条不同的
    故障路径，读到的人要去查的地方不一样，混成一句「日报没到」等于把诊断成本推给他。"""
    head = (
        "⚠️ 今日行情阶段日报没有投递成功\n"
        f"运行日：{run_day}\n"
        f"截至：{format_business(moment)}"
    )
    if row is None:
        middle = (
            "今天的日报**根本没有生成**——到点为止日报表里没有这一天的行，也就谈不上"
            "投递。是生成那一段（心跳链路）没写成，不是投递这一段的问题。"
        )
    else:
        middle = (
            f"今天的日报生成于 {format_business(row.created_at)}，但至今没有投递成功"
            f"（已尝试 {row.delivery_attempts} 轮）。\n"
            f"最近一次失败：{row.delivery_error or '未说明'}"
        )
    tail = (
        "\n\n这条消息来自**独立的投递看门狗**，不是日报自己——"
        "日报发不出去的时候，它报不了自己。"
    )
    return f"{head}\n\n{middle}{tail}"


# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #


def _note(judgement_result: dict, deactivation: dict, landscape: dict) -> str:
    """这一份日报为什么长这样，一句话。给日志与任务返回值用。"""
    if judgement_result.get("base_regime"):
        head = f"判定出结论（{judgement_result.get('effective_regime')}）"
    else:
        head = (
            f"今日判定缺失（{judgement_result.get('skipped') or '未说明'}），"
            "处于保持的上一有效状态"
        )
    change = landscape.get(SECTION_CHANGE) or {}
    return f"{head}；{change.get('note') or '无今昨比对'}"


def _summary(row: DailyReport, outcome: str) -> dict:
    change = (row.landscape or {}).get(SECTION_CHANGE) or {}
    return {
        "symbol": row.symbol,
        "run_day": row.run_day.isoformat(),
        "outcome": outcome,
        "written": outcome == OUTCOME_CREATED,
        "judgement_missing": row.judgement_missing,
        "suggested_count": (row.landscape or {}).get("count", 0),
        "change_status": change.get("status"),
        "note": row.note,
    }
