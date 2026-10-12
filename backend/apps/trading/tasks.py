"""订单与账户相关的 Celery 定时任务。

第①段强制前置：悬挂 ``pending`` 行的反查必须有一个**不依赖交易框架存活**的兜底
（CONTEXT.md 第 137 条：兜底扫描落成 beat 定时任务，当前 beat 里没有任何订单相关
任务）。进程内成交同步只在框架起来时跑，而悬挂行恰恰是「进程死过一次」的产物——
所以兜底必须是独立任务。

**悬挂扫描在框架跑时直接跳过**：那时扫描由进程内成交同步负责（10s 一轮），两处
同时写同一批行只会带来重复告警，不会带来更快的收敛。口径与进程内完全一致——
两处调的是同一个 ``sweep_dangling_orders``，共用同一个测试面。

第二件事是**净值快照的写入方**（CONTEXT.md 第 142 条）。它不能跳过：日内回撤
检查的期初净值只有这一个来源，而框架没跑的时候用户照样可能持仓浮亏。

净值快照这条心跳还顺带驱动**行情阶段判定**（第①段单元 4）：同一类「5 分钟一轮 +
幂等 + 无需在日界准确跑」的调度，理由与回退方式见 ``apps.regime.judgement``。

判定之后再顺带驱动**停用决策推导**（第①段单元 7）。排在判定之后是硬要求：推导读的是
判定刚落下的那条「当前生效阶段」。这一轮**不重建池化表**——重算是人触发的低频动作
（CONTEXT.md 第 118 条），日报只读「当前一代」。回退方式与判定一样：删掉
``_derive_deactivation`` 这一个调用，前两段职责各自完好。

接着把这一轮的判定与推导落成**当天那一条 Shadow 记录**（第①段单元 8i）。它是前两段的
**下游**，不是并列的一段：要落的建议清单正是推导的产物，挂在上游会永远写空。它也确实是
第①段在**机制侧**唯一的产出——Shadow 期机制不施加任何动作，能留下的就是这些行。回退方式
同前：删掉 ``_record_shadow`` 这一个调用，前面各段职责各自完好。

最后落**当天那一条日报**（第①段单元 8iii）。它又是 Shadow 的下游：第②段与 Shadow 的建议
清单同源，而且还要读昨天那份日报的结构化快照做差——两天的差要到「今天也说完了」才成立。
回退方式同前：删掉 ``_generate_report`` 这一个调用。**「每天固定一条、必发」由它保住**：
它是第①段唯一直接说给用户听的东西（CONTEXT.md 第 173 条）。
"""

import asyncio
import logging

from celery import shared_task
from django.db import close_old_connections

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="apps.trading.tasks.check_order_status")
def check_order_status(self) -> dict:
    """扫描悬挂订单并逐条消解（交易所反查 → failed / 未知 / 补回交易所 ID）。

    由 Celery Beat 调度。返回值进任务健康检查，不静默。
    """
    from apps.trading.pending_reconcile import sweep_dangling_orders

    # 交易框架在跑：让进程内那一轮负责，避免两个写入者
    from apps.trading.executor import OrderExecutor

    if OrderExecutor.get_instance() is not None:
        logger.info("[check_order_status] 交易框架活跃，跳过（由进程内成交同步负责）")
        return {"skipped": "order_executor_active"}

    try:
        result = asyncio.run(sweep_dangling_orders())
    except Exception as e:  # noqa: BLE001
        logger.error("[check_order_status] 悬挂订单扫描失败: %s", e, exc_info=True)
        raise
    finally:
        # 长时间运行/反复调度的任务必须自己收掉 DB 连接，否则连接会攒在 worker 上
        close_old_connections()

    payload = result.as_dict()
    if result.scanned:
        logger.info("[check_order_status] %s", payload)
    return payload


@shared_task(bind=True, name="apps.trading.tasks.snapshot_daily_equity")
def snapshot_daily_equity(self) -> dict:
    """写当日净值快照（每个有未停止会话的用户一条，当日只写第一条）。

    5 分钟一轮而非「日界跑一次」：幂等使得重试、进程重启、账户短暂不可达都能
    自愈，不需要「必须在日界准确跑」这种脆弱前提。取不到余额时不写、不编造，
    并把降级状态告警给用户（见 ``daily_snapshot``）。

    顺带跑当日**行情阶段判定**（第①段单元 4）。搭在这条心跳上是对 CONTEXT.md
    字面要求的一处有意偏离，理由与回退方式见 ``apps.regime.judgement`` 的模块
    docstring。两条职责的先后是刻意的：快照是在生产上跑了很久的既有链路，判定抛
    异常时不能连带吞掉它。判定本身失败（而不是数据不足）才往上抛——心跳 5 分钟
    后再来一次，判定写入是幂等的 ``get_or_create``，属 CONTEXT.md「读安全」那一类，
    重试无副作用；吞掉异常则会让判定静默死掉。

    快照与判定之间还插一步**日线续拉**（``sync_daily_candles``）：判定只读库里已落的
    日线，这一步把库内数据续到最新已收盘日，判定才不会天天停在「数据不新鲜」的降级
    收场。续拉失败（取不到新数据）**不往上抛**：那是「上游数据暂不可得」，不是代码故障，
    保持上一有效状态的降级由判定的 ``stale_candles`` / ``no_candles`` 收场承担，日报会
    明写「今日判定缺失」；抛了反而让这一轮日报不发，等于掐断了本该看见这句话的渠道。
    库表为空（冷启动未回填）是状态问题同样不抛。
    """
    from apps.trading.daily_snapshot import write_daily_snapshots

    try:
        result = asyncio.run(write_daily_snapshots())
    except Exception as e:  # noqa: BLE001
        logger.error("[snapshot_daily_equity] 净值快照写入失败: %s", e, exc_info=True)
        raise
    finally:
        # 长时间运行/反复调度的任务必须自己收掉 DB 连接，否则连接会攒在 worker 上
        close_old_connections()

    payload = result.as_dict()
    if result.users:
        logger.info("[snapshot_daily_equity] %s", payload)

    from apps.regime.candles import CandleFetchError, sync_daily_candles

    try:
        candle_report = sync_daily_candles()
    except CandleFetchError as e:
        # 拉不到新日线是上游数据暂不可得，不是代码故障：不抛。抛了会让这一轮日报不发，
        # 而「今日判定缺失」正是日报要写出来的话；保持上一有效状态的降级由判定的
        # stale_candles / no_candles 收场承担（见 apps.regime.judgement）。
        logger.error(
            "[snapshot_daily_equity] 日线续拉失败，判定按库内现状继续: %s", e, exc_info=True
        )
    except ValueError as e:
        # 表为空：冷启动回填还没跑，是状态问题不是故障；判定的 no_candles 收场会说出来
        logger.warning("[snapshot_daily_equity] 日线表为空，跳过续拉（需人工回填）: %s", e)
    else:
        # 日期序列化成 ISO 字符串：返回值进 Celery 结果，须恒为 JSON 安全
        payload["candles"] = {
            "created": candle_report.created,
            "updated": candle_report.updated,
            "unchanged": candle_report.unchanged,
            "last_date": candle_report.last_date.isoformat() if candle_report.last_date else None,
        }
        # 无新数据的一轮（日频运行最常见的一支）不落日志，免得 5 分钟一条刷屏
        if candle_report.wrote:
            logger.info("[snapshot_daily_equity] 日线续拉 %s", candle_report.summary())
    finally:
        close_old_connections()

    from apps.regime.judgement import run_daily_judgement

    try:
        payload["regime"] = run_daily_judgement()
    except Exception as e:  # noqa: BLE001
        logger.error("[snapshot_daily_equity] 行情阶段判定失败: %s", e, exc_info=True)
        raise
    finally:
        close_old_connections()

    if not payload["regime"].get("skipped"):
        logger.info("[snapshot_daily_equity] 行情阶段判定 %s", payload["regime"])

    _derive_deactivation(payload)

    _record_shadow(payload)

    return _generate_report(payload)


def _derive_deactivation(payload: dict) -> dict:
    """跑停用决策推导，挂进 `payload["deactivation"]`（第①段单元 7）。

    **为什么还是搭在这条心跳上、为什么排在判定之后**：与判定同一个理由——同一类「5 分钟
    一轮 + 幂等 + 无需在日界准确跑」的调度。排在判定之后的理由更硬：推导读的是判定刚落下的
    那条「当前生效阶段」（`deactivation_run.current_regime_state`），先跑会永远慢一拍——
    而且慢的那一拍**看起来完全正常**，只是每天晚一天停用。

    **这一轮不重建池化表**（CONTEXT.md 第 118 条）：重算是人触发的低频动作，日报只读
    「当前一代」。所以「池化表还没建过」是一种**合法收场**而非故障——推导会以
    `skipped="no_generation"` 加一句话回来（见 `deactivation_run` 的模块 docstring）。

    **注册表发现不在这里补**：`deactivation_run.managed_set()` 已经补过，而它是「解析得到
    实现类」的唯一出处。这里再补一遍就是第二处定义，迟早会漂。

    推导失败（不是数据不足）才往上抛：心跳 5 分钟后再来一次，`update_or_create` 幂等，
    重试无副作用；吞掉异常则会让停用决策静默死掉。**冷启动 / 状态过期 / 没有池化表**三种
    收场都不抛——它们是「什么都不动」，不是失败——但每一种都带着一句给人看的话
    （`note`），所以这里照样往日志里写一遍：**日志不算被看见**，那句话的正经出口是日报，
    这里只是不让它在任务层就消失。
    """
    from apps.regime.deactivation_run import run_deactivation

    try:
        payload["deactivation"] = run_deactivation()
    except Exception as e:  # noqa: BLE001
        logger.error("[snapshot_daily_equity] 停用决策推导失败: %s", e, exc_info=True)
        raise
    finally:
        # 长时间运行/反复调度的任务必须自己收掉 DB 连接，否则连接会攒在 worker 上
        close_old_connections()

    summary = payload["deactivation"]
    if summary["skipped"]:
        logger.info("[snapshot_daily_equity] 停用决策：%s", summary["note"])
    else:
        logger.info("[snapshot_daily_equity] 停用决策 %s", summary)

    return payload


def _record_shadow(payload: dict) -> dict:
    """把这一轮的判定与推导落成当天的 Shadow 记录（第①段单元 8i）。

    **为什么排在这两段之后**：要落的建议清单就是推导的产物（见 `apps.regime.shadow` 的
    模块 docstring），先写会永远写空。

    **为什么判定没有结论时不抛**：那是「今天没有结论」，不是这几层的失败——判定层已经
    把收场说清楚了（`stale_candles` / `no_candles` / `undecidable`），这一层照落一行，
    把「判定跑了但机制没表态」留下来。真正写不进去（DB 故障）才往上抛：心跳 5 分钟后再来
    一次，`get_or_create` + 占位行补写是幂等的，重试无副作用；吞掉异常则会让这张表静默
    停在某一天，而「这张表停在某一天」正是它要负责发现的事情。
    """
    from apps.regime.shadow import write_shadow_record

    try:
        payload["shadow"] = write_shadow_record(
            payload.get("regime") or {}, payload.get("deactivation") or {}
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[snapshot_daily_equity] Shadow 记录写入失败: %s", e, exc_info=True)
        raise
    finally:
        # 长时间运行/反复调度的任务必须自己收掉 DB 连接，否则连接会攒在 worker 上
        close_old_connections()

    return payload


def _generate_report(payload: dict) -> dict:
    """把这一轮的各段产物落成当天的日报（第①段单元 8iii）。

    **为什么排在 Shadow 之后**：第②段与 Shadow 的建议清单同源（都是这一轮推导的产物），
    而且还要读**昨天那份日报的结构化快照**做差。排在最末就是「这一轮能说的话都说完之后
    再说」。

    **为什么判定没有结论时照样往下走**：日报是「每天固定一条、必发」，判定缺失正是它要
    写出来的事情之一（第①段明写「今日判定缺失，处于保持的上一有效状态」）。**写不写由
    ``apps.regime.report`` 决定**：有结论就写，没结论就等到截止时刻再写——在那之前每 5
    分钟一轮都还有机会等到结论，而先写一条「判定缺失」会把当天这条**永久钉死**
    （一天一条是唯一约束），后面等到结论也改不回来。

    真正写不进去（DB 故障）才往上抛，与前两段同一种处置：心跳 5 分钟后再来一次，
    ``get_or_create`` 幂等，重试无副作用；吞掉异常则会让这张表静默停在某一天，而
    「这张表停在某一天」正是它要负责发现的事情。
    """
    from apps.regime.report import write_daily_report

    try:
        payload["report"] = write_daily_report(
            payload.get("regime") or {},
            payload.get("deactivation") or {},
            payload.get("shadow") or {},
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[snapshot_daily_equity] 日报生成失败: %s", e, exc_info=True)
        raise
    finally:
        # 长时间运行/反复调度的任务必须自己收掉 DB 连接，否则连接会攒在 worker 上
        close_old_connections()

    summary = payload["report"]
    if summary.get("written"):
        logger.info("[snapshot_daily_equity] 日报 %s", summary)
    else:
        logger.info("[snapshot_daily_equity] 日报未写：%s", summary.get("note"))

    return payload
