import logging
import os
from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger(__name__)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.dev')

# Set strategy path for auto-discovery of strategy modules
from apps.strategy_engine.registry import StrategyRegistry
StrategyRegistry.set_strategy_path('/root/.tradelogx/strategies')

app = Celery('trade_agent')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# 显式导入 task 模块（autodiscover_tasks 在某些环境下不生效）
import apps.agent.tasks  # noqa: F401
import apps.backtest.tasks  # noqa: F401
import apps.signal_monitor.tasks  # noqa: F401
import apps.channel.tasks  # noqa: F401
import apps.trading.tasks  # noqa: F401
import apps.regime.tasks  # noqa: F401

# Grid search independent queue
app.conf.task_routes = {
    'apps.backtest.tasks.run_grid_search_task': {'queue': 'grid_search'},
}

app.conf.beat_schedule = {
    # Sync positions from exchange every 5 minutes
    # TODO: apps.trading.tasks.sync_positions 尚未实现，启用后需补上
    # 'sync-positions': {
    #     'task': 'apps.trading.tasks.sync_positions',
    #     'schedule': 300.0,
    # },
    # Clean expired agent memory (L2) every hour
    # TODO: apps.memory.tasks.clean_expired_memory 尚未实现
    # 'clean-agent-memory': {
    #     'task': 'apps.memory.tasks.clean_expired_memory',
    #     'schedule': crontab(minute=0),
    # },
    # Clean old audit logs every day at 02:00 UTC
    # TODO: apps.agent.tasks.clean_old_audit_logs 尚未实现
    # 'clean-audit-logs': {
    #     'task': 'apps.agent.tasks.clean_old_audit_logs',
    #     'schedule': crontab(hour=2, minute=0),
    # },
    # Check task health every 30 seconds — heartbeat, zombie detection, auto-retry
    'check-task-health': {
        'task': 'apps.agent.tasks.check_task_health',
        'schedule': 30.0,
    },
    # Check session expiry every 30 seconds
    'check-session-expiry': {
        'task': 'apps.agent.tasks.check_session_expiry',
        'schedule': 30.0,
    },
    # Check signal monitors every 30 seconds
    'check-signals': {
        'task': 'apps.signal_monitor.tasks.check_signals',
        'schedule': 30.0,
    },
    # Sync perpetual funding rates every 8h (after settlement points 00/08/16 UTC)
    'sync-funding-rates': {
        'task': 'apps.backtest.tasks.sync_funding_rates',
        'schedule': crontab(hour='1,9,17', minute=30),
    },
    # Refresh expiring Feishu user tokens every 15 minutes
    'refresh-feishu-user-tokens': {
        'task': 'apps.channel.tasks.refresh_feishu_user_tokens',
        'schedule': 900.0,
    },
    # Clean expired signal monitors every hour
    'clean-expired-monitors': {
        'task': 'apps.signal_monitor.tasks.clean_expired_monitors',
        'schedule': crontab(minute=15),
    },
    # 悬挂订单兜底扫描（本地已落单但交易所 ID 为空的行 → 反查交易所）
    # 交易框架活跃时本任务自行跳过，由进程内成交同步（10s 一轮）负责
    'check-order-status': {
        'task': 'apps.trading.tasks.check_order_status',
        'schedule': 60.0,
    },
    # 当日净值快照（日内回撤检查的期初净值只有这一个来源：daily_snapshots 表
    # 在此之前没有任何写入方，检查于是必然走「无法获取期初资金数据」拒绝下单且不告警）
    # 5 分钟一轮 + 幂等（当日只写第一条）→ 不需要「必须在日界准确跑」
    'snapshot-daily-equity': {
        'task': 'apps.trading.tasks.snapshot_daily_equity',
        'schedule': 300.0,
    },
    # 日报投递（第①段单元 8iv）：把当天那一份日报按人裁剪后送到每个 is_active 用户。
    # 幂等（已送达的人不重投），所以密一点只是几次空转。
    # 5 分钟一轮而不是 60 秒：日报是日频产物，投递滞后一轮心跳（≤5 分钟）无所谓，
    # 真正的及时性靠任务内的重试（60 秒起、翻倍）保证，不靠 beat 的密度。
    # **间隔与 report.WATCHDOG_GRACE 是一对**：宽限期必须比「本条间隔 + 心跳间隔」
    # 之和大（心跳是喂日报的那个写入者），否则看门狗会早于「投递最晚什么时候才轮到」
    # 动手，而抢跑的表现是每天一条假告警。把这两条 beat 调**密**不违例，调稀就违例。
    # test_delivery.py::test_the_grace_covers_the_two_intervals_it_bounds 钉着这一条。
    'regime-report-deliver': {
        'task': 'apps.regime.tasks.deliver_report',
        'schedule': 300.0,
    },
    # 日报投递看门狗（第①段单元 8iv）：当天日报过了 report.REPORT 的截止时刻
    # （+ WATCHDOG_GRACE）还是 delivered_at 为空，就升级告警。
    # **与投递任务分开一条 beat 条目是必须的**：合成一条等于让看门狗去报自己的失败——
    # 它恰好是失败的那一个时，它不会响（CONTEXT.md:175）。
    # 一天绝大多数轮次是「未到截止时刻」或「已投递」，都是不写一行、不发一条的空转。
    # 不用 crontab 表达式把「北京 09:00」翻成 UTC 小时：判定链路之所以反复强调
    # 「北京 08:00 == 日线换线」，就是因为口译钟点的地方只能有一处，而
    # CELERY_TIMEZONE 是 UTC——`crontab(hour=9)` 会在北京 17:00 触发。
    'regime-report-delivery-watchdog': {
        'task': 'apps.regime.tasks.check_report_delivery',
        'schedule': 300.0,
    },
    # 停止声明窗口同步（第②段单元 ②c）：把事件表与生效判定上的事实对账成
    # HaltDeclaration 行——声明表是 halt 状态机唯一读的那份状态，判定函数不读事件表。
    # **固定间隔，不用 crontab**：这条任务只关心「多久跑一次」，不关心「每天几点」。
    # 间隔就是这套写法的敞口上限：一段窗口结束后、下一段开启前如果窄于一轮（比如两件
    # 高影响事件挨得极近），那一小段里谁都拦不住——而事件熔断不可人工豁免（见
    # halt_sync.py 的模块 docstring）。所以这个数是**安全参数**，调稀要按同一句话权衡。
    'regime-halt-window-sync': {
        'task': 'apps.regime.tasks.sync_halt_windows',
        'schedule': 300.0,
    },
    # 行情阶段 gate 同步（第③段单元 ③b）：把「本阶段该停哪些策略」对账成声明行（与
    # 上面那条同一张表、不同一档），并在**Shadow 期也照跑**——档位关着时它不拦人，但
    # 声明行该解除的仍要解除（阶段说清之后把同一次动作再敲一遍，就是靠这条补齐的）。
    # **与上面那条分开一条 beat 条目**：两条任务写同一张表的两档，各自认自己的开关怀
    # 疑，合成一条会让「哪一档没对上」这件事被另一档的成功盖住。
    # 固定间隔，不用 crontab：它只关心「多久跑一次」——行情阶段本身是日频判定的产物
    # （`regime_daily`），取数用不上比一轮 5 分钟更密的节奏；而**区间下限是安全参数**：
    # 阶段换档到「这一批策略真被停掉」之间的滞后上限就是这一轮，调稀要按同一句话权衡。
    'regime-gate-sync': {
        'task': 'apps.regime.tasks.sync_gate',
        'schedule': 300.0,
    },
    # 候选事件到期清理（CONTEXT.md 第 37 / 94 条）：把过了 14 天失效期、还没人确认的候选
    # 标成「已丢弃」。**这一条此前没有任何调度入口**，所以「到期即丢弃」只存在于实现与
    # 测试里，而模型与命令的注释都当它在跑。
    # **与上面两条分开一条 beat 条目**：它写的是候选表，那两条碰的是声明表，失败面不同。
    # 固定间隔，不用 crontab：它只关心「多久跑一次」，不关心「每天几点」；而失效期是
    # 14 天，5 分钟一轮换来的唯一好处是「到期」到「被丢弃」之间的窗口短到可以忽略
    # （那个窗口里候选的回显会以「已过失效期」开头）。一轮的代价是一次带索引的查询加
    # 可能的零行更新。
    # **要重启 beat 才会被调度**：`beat_schedule` 只在 beat 启动那一刻合并进
    # `PeriodicTask` 一次（见 `apps/regime/beat_health.py` 的模块 docstring），在那之前
    # 体检页的「调度表（beat）」会把它印成「从未进入调度表」。
    'regime-candidate-expiry': {
        'task': 'apps.regime.tasks.expire_candidates',
        'schedule': 300.0,
    },
    # 调度表体检（第③段单元 U3）：把「哪几条 beat 条目没在正常跑」变成一条给全体用户
    # 的即时消息（体检页那一段是它的第一半，见 `apps/regime/beat_health.py`）。
    # **自己必须是一条独立的条目**：它盯的正是「条目有没有进调度表 / 有没有发出去」，
    # 挂在任何一条被盯的条目上都会让那一条同时当裁判与当事人。
    # 固定间隔、不是 crontab：它只关心「多久看一次」。300 秒是与被盯的那些条目同一个量级
    # ——判据里已经含了落库滞后（180 秒）这一项，再密也只是把同一份读数看两遍。
    # **要重启 beat 才会被调度**（这一条自己也不例外）：在那之前它不会跑，而这件事恰好
    # 正是它要报告的形状——只有体检页看得见。
    'regime-beat-health': {
        'task': 'apps.regime.tasks.check_beat_health',
        'schedule': 300.0,
    },
}

# 使用数据库调度器，支持动态添加/删除定时任务
app.conf.beat_scheduler = 'django_celery_beat.schedulers:DatabaseScheduler'

# ---------------------------------------------------------------------------
# Worker startup signal — one-time recovery check
# ---------------------------------------------------------------------------

from celery.signals import worker_ready


@worker_ready.connect
def on_worker_ready(sender=None, **kwargs):
    """Trigger startup recovery check exactly once on worker boot."""
    from apps.agent.tasks import startup_recovery_check
    logger.info("[worker_ready] triggering startup recovery check")
    # Run synchronously so the worker doesn't proceed without recovery
    try:
        result = startup_recovery_check()
        logger.info("[worker_ready] recovery result: %s", result)
    except Exception:
        logger.exception("[worker_ready] recovery failed, worker continuing")
