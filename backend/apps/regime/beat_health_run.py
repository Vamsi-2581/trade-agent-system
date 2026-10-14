"""调度表体检的取数：`beat_health.py` 的另一半。

与 `truth_run` / `deactivation_run` 同一种分工：纯层定口径与措辞，本模块只管
「数从哪来」。两个来源，**必须同时读**：

| 来源 | 是什么 | 答的问题 |
|------|--------|---------|
| `celery_app.app.conf.beat_schedule` | 文件里**声明**了哪些条目 | 本该有什么 |
| `PeriodicTask` 表 | 调度器**此刻**会发什么 | 实际有什么、发过没有 |

只读前者会把「文件里写了」当成「跑起来了」——而这两件事今天正好不一样（见
`beat_health.py` 的模块 docstring）；只读后者则答不出「有一条被漏掉了」，因为漏掉的那条
在表里根本不出现。

## 为什么是读 `PeriodicTask` 而不是自己记

`last_run_at` / `total_run_count` 是调度器自己写的（例：`check-session-expiry` 一行上已经
写着 16 万次派发）。另建一张心跳表就是在给「上次跑于何时」造第二个答案，而两个答案不一致
时读的人分不出该信哪个——与 `report.event_library_line` 不另算一份「事件库有多陈旧」同
一条。

**但它写得不勤**：`ModelEntry` 只在内存里推进，落库由 `beat` 的 `should_sync()` 触发
（`celery.beat.Scheduler.sync_every`，默认 180 秒）。所以这个数天生滞后 3 分钟量级——
判据里含这一项，理由与实测见 `beat_health.py` 的模块 docstring。这一条也有一个好处：
「16 万次」这种计数不该每 30 秒往库里写一次。

## 第三个读口：`check()`（第③段单元 U3）

`lines()` 是给体检页的，`check()` 是给 `tasks.check_beat_health` 的：同一份 `findings`，
收窄到值得喊人的那几档（`beat_health.alerts`），够格时给全体 `is_active` 用户发一条即时
消息（收件人与出口复用 `halt_notify.alert_everyone`，不新建投递机制）。

**账落在本模块、不在纯层**：`_alerted_on` 记「同一批问题、同一业务日已经喊过」。它在这
里，因为它是**进程状态**而纯层不持状态。为什么不用「连续两轮都在才喊」那条更严的规则，
见 `_alerted_on` 上面那段话——一句话：worker 是 `--concurrency=4`，每个子进程各持一份
账时，那个规则的失败方向是**沉默**。
"""

from __future__ import annotations

from datetime import date, datetime

from django.utils import timezone

from apps.common.time_utils import to_business
from apps.regime import beat_health
from apps.regime.beat_health import BeatProblem, Entry

#: `IntervalSchedule.period` → 秒。**不用它的 `.schedule` 属性反推**：那要把一个
#: celery 的调度对象再拆回来，而这一张表就是它的定义本身。
_PERIOD_SECONDS = {
    "microseconds": 1e-6,
    "seconds": 1.0,
    "minutes": 60.0,
    "hours": 3600.0,
    "days": 86400.0,
}


def declared() -> list[str]:
    """文件里声明的条目名，按文件顺序。**顺序即排版顺序**（见 `beat_health.findings`）。

    延迟 import `celery_app`：它在 import 时就会拉起任务模块与策略目录（`apps/agent/views.py`
    读同一份配置时也是这么做的），而本模块被体检页 import，不该把那份重量带进每一次 import。
    """
    from celery_app import app

    return list(app.conf.beat_schedule or {})


def entries() -> list[Entry]:
    """调度表里此刻的每一行。**含已停用的**：停用也是一种「没在跑」，只是成因不同。"""
    from django_celery_beat.models import PeriodicTask

    return [
        Entry(
            name=row.name,
            task=row.task,
            period_seconds=_period_seconds(row.interval),
            last_run_at=row.last_run_at,
            total_run_count=row.total_run_count or 0,
            enabled=row.enabled,
        )
        for row in PeriodicTask.objects.select_related("interval")
    ]


def _period_seconds(interval) -> int | None:
    """`IntervalSchedule` → 秒；`None` 表示这一条没有固定间隔（crontab / solar / clocked）。

    `None` 不是「不知道」而是「没有固定间隔」：`beat_health` 拿它当「不定罪」的判据。
    """
    if interval is None:
        return None
    factor = _PERIOD_SECONDS.get(interval.period)
    if factor is None:  # 将来 django_celery_beat 加了新周期档位
        return None
    return int(interval.every * factor)


def lines(*, now: datetime | None = None) -> list[str]:
    """体检页上那一段。**返回的行不带缩进**，缩进由调用方加（与 `truth.describe` 同一条）。"""
    return beat_health.describe(entries(), declared(), now=now or timezone.now())


# --------------------------------------------------------------------------- #
# 即时消息（第③段单元 U3）
# --------------------------------------------------------------------------- #

#: 「同一批问题、同一业务日已经喊过」的账。**进程内**，与 `report._alerted_on` 同一种写法
#: 与同一条理由（那条是「同一个运行日的日报投不出去只喊一次」）。
#:
#: 键是那一轮值得喊的**问题集合**（`Finding.key` 的 frozenset），不是单个问题：
#: 「修好 A、又坏了 B」和「A 之外还多了 B」都是**新的一批**，都该再喊一条；而同一批反复
#: 出现（每 300 秒一轮）只喊一条。旧日子的键在下一轮被清掉（`_forget_other_days`）。
#:
#: 两处诚实的说明：
#:
#: - **worker 是 4 个进程**（`docker-compose.yml` 的 `--concurrency=4`），这本账各进程
#:   一份，最坏情况是同一批问题被喊两到四条（重复）。刻意**不做**跨轮记忆（「连续两轮
#:   都在才喊」）：那条规则要求两轮落在同一个子进程里，而它的失败方向是**沉默**——正是
#:   这个单元要堵的形状。重复的代价比漏掉小（`halt_notify` 的模块 docstring 里同一句话）。
#: - 进程重启（部署、worker 崩了）会把账清零，代价是重新喊一遍一遍；与 `report._alerted_on`
#:   同一条取舍。
_alerted_on: dict[frozenset[tuple[str, BeatProblem]], date] = {}


def _forget_other_days(day: date) -> None:
    """只留今天的账。这本账只按「当天喊过没有」判定，留着旧日子只会让它长个儿。"""
    for key in [key for key, seen in _alerted_on.items() if seen != day]:
        del _alerted_on[key]


def check(*, now: datetime | None = None) -> dict:
    """体检一轮；够格就喊一条，返回摘要。**本函数不抛也不吞之外的异常**——读不到库就没有
    「发出去了没有」这句话可说，异常照旧往上抛（调用方是 `tasks.check_beat_health`）。

    返回值的 ``reason`` 是一个小词表，调用方按它决定记不记日志（空转的几档不记，见
    `tasks.check_beat_health`）：

    ==========================  ==========================================
    ``no_problems``             体检页也会印「都在正常跑」
    ``nothing_to_say``          有问题，但都在消息层的射程之外（`never` / 全表都超期的
                                `late`）——页上有、不喊人
    ``already_alerted_today``   同一批问题今天已经喊过
    ``no_recipients``           够格喊，但一个 `is_active` 用户都没有：**不记账**（将来
                                有人了还得能响，与 `report.check_report_delivery` 同一条）
    ``sent``                    喊出去了（``delivered`` > 0）
    ``send_failed``             够格喊、有人可发，但一条都没送达：**不记账**（没人听见的
                                喊话不算喊过，下一轮还得喊）
    ==========================  ==========================================

    ``alerted`` 只在**真的有人收到**时为真。返回的 ``problems`` 是体检页那份口径的总数，
    ``worth_alerting`` 才是这条消息认下的条数——两个数分开报，因为「页上有 N 条」与
    「喊了 M 条」本来就该能不一样（见 `beat_health.alerts`）。
    """
    from apps.regime import halt_notify

    at = now or timezone.now()
    rows = list(entries())
    found = beat_health.findings(rows, declared(), now=at)
    due = beat_health.alerts(found, newest=beat_health.newest_entry(rows), now=at)
    summary = {
        "checked": True,
        "problems": len(found),
        "worth_alerting": len(due),
        "alerted": False,
    }
    if not due:
        summary["reason"] = "nothing_to_say" if found else "no_problems"
        return summary

    day = to_business(at).date()
    _forget_other_days(day)
    key = frozenset(finding.key for finding in due)
    if key in _alerted_on:
        summary["reason"] = "already_alerted_today"
        return summary

    recipients = halt_notify.all_active_user_ids()
    if not recipients:
        summary["reason"] = "no_recipients"
        return summary

    outcome = halt_notify.alert_everyone(beat_health.alert_body(due, now=at))
    delivered = outcome["delivered"]
    if delivered:
        _alerted_on[key] = day
    summary["alerted"] = bool(delivered)
    summary["reason"] = "sent" if delivered else "send_failed"
    summary.update(outcome)
    return summary
