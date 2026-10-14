"""调度表体检（CONTEXT.md 第 180 条那条路里**「跑了没」的那一半**）：纯函数层。

`beat_health_run.py` 是它的另一半（读 `PeriodicTask` 与 `celery_app.beat_schedule`）。
本模块只吃「调度表里此刻有哪些条目、各自上一次是什么时候发的」加上「文件里声明了哪些」，
不吃 DB、不读时钟。

## 为什么需要它：第 180 条要求的「跑了没」今天没有出口

第 180 条把新任务的失败可见性钉成两条互相独立的路：**接入已有的任务健康检查
（`check_task_health`）得到「任务跑了没」，日报第④段得到「结论新不新」**。第二条路是
活的（`report._section_health`），第一条路**结构上接不进去**：

- `check_task_health` 扫的是 Redis 里 `task:progress:*` 且 `status=running` 的 hash，
  而那种 hash 只有建过 `TaskTracker` 的任务才有（agent 任务、回测任务）；
- 它的僵尸分支还要 `user_id` 才能把消息发出去、要一份 `agent:tasks` 载荷才能自动重试
  —— beat 任务两样都没有。

于是 beat 任务的返回值落进 Redis DB2 的 Celery 结果后端，**没有读者**；一条不再被调度的
任务与一条正常跑的任务，从外面看起来完全一样（正是「沉默必须能被识别为异常」要堵的形状）。

## 读的是「调度器会发什么」，不是「文件里写了什么」

`last_run_at` 由 **beat 在派发时**盖，不由 worker 在成功时盖。所以本模块答的是
**「beat 把它发出去了没有」**，不是「任务成功了没有」——一条每 300 秒都发、每次都失败的
任务，在这里与一条成功的任务长得一模一样。那半边由日报第④段（各段结论新不新）回答，
两条路合起来才是第 180 条那句话。**这句话必须印在页面上**，否则这一行会把「发了」读成
「好了」，而那是比没有这一行更坏的东西。

## 四种「没在正常跑」，分开说

- `unscheduled`：**文件里声明了，调度表里没有这一行**。`django_celery_beat` 的
  `DatabaseScheduler` 只在 `setup_schedule`（beat 启动那一刻）把 `beat_schedule` 合并进
  库，之后往文件里加的条目不会生效——所以这个状态的典型成因是「条目是后来加的，beat 没
  重启过」。它不是「跑了但失败了」，是一条**永远不会被发出去**的条目。
- `never`：行在表里，`last_run_at` 是空的——一次都没发过。
- `late`：发过，但已经超过宽限轮数（`MISSED_ROUNDS` × 间隔）。

**没有固定间隔的条目（crontab）不定罪**：本模块拿不到「下一次该在什么时候」。硬套一个
「24 小时内跑过就算正常」是拿日历猜调度意图，猜错了这一页就会规律性地报假警——而假警
比沉默更快地教会人忽略这一页。它们照旧列在总括的计数里（「另有 N 条无固定间隔」），
只是不进问题清单。

## 宽限为什么是「轮数」而不是秒数，以及那个 3 分钟的下限

同一件事在 60 秒一轮与 300 秒一轮上的容差本来就该不一样：worker 忙一轮、容器重启一次
都会让某一条晚一轮。按「错过几轮」定容差，同一句话在两条任务上才是同一个意思。

**但轮数不是全部**：`last_run_at` 不是每派发一次就落库的。`ModelEntry.__next__` 只在内存
里推进，真正写库的是 `ModelEntry.save()`，而它由 `beat` 的 `should_sync()` 触发——那一条
的周期是 `celery.beat.Scheduler.sync_every`，**默认 180 秒**（本项目没有覆盖它）。所以库
里的 `last_run_at` 天生滞后：**最多 180 秒 + 该条目自己的一轮**，且它与「任务真的停了」
在单次读数上完全同形。

不把这一项算进判据的后果是**假警**，而且是规律性的：30 秒一轮的任务，3 轮宽限只有 90 秒
< 180 秒，于是它每时每刻都显示「已超期」——实测就是这个结果（三条 30 秒一轮的任务齐刷
刷报超期，而 beat 的日志显示它们每 30 秒都在正常派发）。这一页的整个存在理由是让人相信
它，而规律性的假警比沉默更快地毁掉这件事。

所以判据是 ``age > missed_rounds * period + SCHEDULER_WRITE_LAG``。代价说清楚：一条 30 秒
一轮的任务真的停了，这一页最多 4 分半之后才说得出。刻度更细的答案做不到——库里的数就是
3 分钟粒度的，硬报只能是把噪声当信号。

## 消息层只认三档（第③段单元 U3）

体检页四档都印，而**喊人**的那条消息（`alerts` → `alert_body`）只认三档。两处收窄各有
一条理由，且都是「不加状态的判据就不成立」：

- **`never`（从未发出去过）不喊。** `PeriodicTask` 没有「这一行什么时候进表」这一格：
  它只有 `date_changed`，而那是 `auto_now`——落库时被顶掉，实测跟着 `last_run_at` 走
  （库里 `check-task-health` 是 `last_run_at 08:35:52 / date_changed 08:36:22`）。于是
  「刚被合并进来、落库还没轮到」与「真的从没发出去过」在库里的样子**完全一样**，而每
  一次 beat 重启、每加一条新条目都会撞上这个窗口。拿它喊人，等于让这条机制的第一条消息
  说假话——而这正是「假警比沉默更快地毁掉这一页」那句要防的。它留在页上。
- **`late` 要有旁证。** 全表最近的一条（`newest_entry`）自己也超期时，说明是 beat 整体
  没在跑（停机、或刚重启还没落库）——那句话说不成「哪一条坏了」，所以一条 `late` 都不
  喊。旁证只读**这一轮**的库状态，因此不怕 `--concurrency=4`。

**账不在这里。** 本模块是纯层、无状态；「同一批问题当天喊过没有」那本账在
`beat_health_run.check`——它是进程状态，不是口径。那里也记着为什么不用「连续两轮才喊」
（每个 worker 子进程各一份账时，那个规则的失败方向是**沉默**）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable, Sequence

#: 宽限轮数：连续错过这么多轮才算「没在正常跑」。3 是「一次都不许少报、但单轮抖动不算」的
#: 折中——worker 重启、一轮被排在长任务后面、beat 自己卡一下都在一轮的量级里。
#:
#: **它是本文件的常量，不进 `config.GROUPS`**：那一组是「会被一起调整、一起审计」的机制
#: 参数，而 `config.full_snapshot()` 会被内嵌进每一条判定记录（`config_snapshot`）——把
#: 一个调度体检的容差塞进去，会让日后读判定记录的人以为它与判定有关。且它不改变任何动作：
#: 只决定这一行说「按时」还是「超期」。等它开始触发消息时（那是一条独立的单元）再谈搬家。
MISSED_ROUNDS = 3

#: 调度器把 `last_run_at` 落库的周期上限（秒）：`celery.beat.Scheduler.sync_every`，
#: 默认 3 分钟，本项目没有覆盖它（也没有覆盖 `beat_sync_every`——那一个只会让落库**更
#: 频繁**，所以这个数是**上界**，任何时候都成立）。判据里必须含它，理由见模块 docstring。
#:
#: 与 `MISSED_ROUNDS` 同一条：留在这里、不进 `config.GROUPS`（那组会被内嵌进判定记录）。
SCHEDULER_WRITE_LAG = 180.0


class BeatProblem(str, Enum):
    """一条条目「没在正常跑」的三种形状。**分开说**：它们对应的下一步动作不同。"""

    UNSCHEDULED = "unscheduled"
    DISABLED = "disabled"
    NEVER_RAN = "never"
    LATE = "late"

    @property
    def display(self) -> str:
        return _PROBLEM_DISPLAY[self]


_PROBLEM_DISPLAY = {
    BeatProblem.UNSCHEDULED: "从未进入调度表",
    BeatProblem.DISABLED: "已被停用（调度器不会发它）",
    BeatProblem.NEVER_RAN: "从未发出去过",
    BeatProblem.LATE: "已超期",
}


@dataclass(frozen=True)
class Entry:
    """调度表里的一行。`period_seconds` 是 `None` 表示这一条没有固定间隔（crontab）。"""

    name: str
    task: str
    period_seconds: int | None
    last_run_at: datetime | None
    total_run_count: int = 0
    enabled: bool = True

    @property
    def judgeable(self) -> bool:
        """这一条有没有一个固定间隔可供定罪（见模块 docstring）。"""
        return self.period_seconds is not None

    def age_seconds(self, *, now: datetime) -> float | None:
        """距上次派发多久。**没发过是 `None`**，不是 0——0 会被读成「刚刚发过」。"""
        if self.last_run_at is None:
            return None
        return (now - self.last_run_at).total_seconds()


@dataclass(frozen=True)
class Finding:
    """一条「没在正常跑」。`entry` 为 `None` 时表示它连调度表都没进去。"""

    problem: BeatProblem
    name: str
    entry: Entry | None = None

    def age_seconds(self, *, now: datetime) -> float | None:
        return None if self.entry is None else self.entry.age_seconds(now=now)

    @property
    def key(self) -> tuple[str, BeatProblem]:
        """这一条的身份 = 条目名 + 档位（`alerts` 的调用方按它记账）。

        **不含 `entry`**：那上面挂着 `last_run_at` / `total_run_count`，每轮都在变，拿它
        当身份等于每轮都是新问题。**含档位**：「A 从『已超期』变成『从未进入调度表』」是
        另一件更坏的事，该另喊一条。
        """
        return (self.name, self.problem)


def is_late(
    entry: Entry,
    *,
    now: datetime,
    missed_rounds: int = MISSED_ROUNDS,
    write_lag: float = SCHEDULER_WRITE_LAG,
) -> bool:
    """这一条是不是「发过、但已超期」。**没发过与没有固定间隔都不算超期**（各是另一档）。

    抽出来是因为两处必须用同一个判断：挑问题清单（`findings`）与给 `late` 找旁证
    （`alerts`）。各写一遍的话，同一轮里会出现一处说「已超期」、另一处说「不算」。
    """
    if entry.last_run_at is None or not entry.judgeable:
        return False
    age = entry.age_seconds(now=now) or 0.0
    return age > missed_rounds * entry.period_seconds + write_lag


def findings(
    entries: Iterable[Entry],
    declared: Sequence[str],
    *,
    now: datetime,
    missed_rounds: int = MISSED_ROUNDS,
    write_lag: float = SCHEDULER_WRITE_LAG,
) -> list[Finding]:
    """把「没在正常跑」的条目挑出来，按**声明顺序**（`declared` 的顺序）排。

    顺序取声明顺序而不是表里的顺序：这一页是给人对照 `beat_schedule` 读的，两处顺序不一致
    会让人以为漏了几条。表里有、文件里没有的（DB 建的动态任务）不进问题清单——它们不是
    「没在跑」，只是不归这份文件管。
    """
    by_name = {entry.name: entry for entry in entries}
    out: list[Finding] = []
    for name in declared:
        entry = by_name.get(name)
        if entry is None:
            out.append(Finding(BeatProblem.UNSCHEDULED, name))
            continue
        if not entry.enabled:
            out.append(Finding(BeatProblem.DISABLED, name, entry))
            continue
        if entry.last_run_at is None:
            out.append(Finding(BeatProblem.NEVER_RAN, name, entry))
            continue
        if is_late(entry, now=now, missed_rounds=missed_rounds, write_lag=write_lag):
            out.append(Finding(BeatProblem.LATE, name, entry))
    return out


def newest_entry(entries: Iterable[Entry]) -> Entry | None:
    """全表**最近一次被派发**的那一条（谁都没发过时是 `None`）。

    两个读者共用：体检页那句「最近一条 X 距今 …」，与 `alerts` 给 `late` 找的旁证。
    """
    return max(
        (entry for entry in entries if entry.last_run_at is not None),
        key=lambda entry: entry.last_run_at,
        default=None,
    )


#: 消息层认的档位。`NEVER_RAN` 不在其中，而 `LATE` 还要额外过一道旁证——理由见模块
#: docstring 的「消息层只认三档」。**体检页照旧四档都印**：收窄的只是「喊人」。
_ALERTABLE = frozenset(
    {BeatProblem.UNSCHEDULED, BeatProblem.DISABLED, BeatProblem.LATE}
)


def alerts(
    found: Sequence[Finding],
    *,
    newest: Entry | None,
    now: datetime,
    missed_rounds: int = MISSED_ROUNDS,
    write_lag: float = SCHEDULER_WRITE_LAG,
) -> list[Finding]:
    """`found` 里**值得喊人**的那几条，顺序照旧（见模块 docstring 的「消息层只认三档」）。

    ``newest`` 是 `newest_entry` 的结果，由调用方传进来而不是在这里现算：体检页那一段要
    用它印「最近一条」，一页上两个「最近一条」必须出自同一次读数。

    两道筛：`NEVER_RAN` 一律不喊；全表最近的一条自己也超期时，`LATE` 一条都不喊（那不是
    「哪一条坏了」）。**`UNSCHEDULED` / `DISABLED` 不受第二条影响**——它们是「文件说要跑、
    而它不会被发出去」，与 beat 此刻是不是整体停着无关，且修理动作（重启 beat）正好同一
    个。
    """
    out = [finding for finding in found if finding.problem in _ALERTABLE]
    if newest is not None and is_late(
        newest, now=now, missed_rounds=missed_rounds, write_lag=write_lag
    ):
        out = [finding for finding in out if finding.problem is not BeatProblem.LATE]
    return out


def counts(
    entries: Sequence[Entry],
    declared: Sequence[str],
    *,
    now: datetime,
    missed_rounds: int = MISSED_ROUNDS,
    write_lag: float = SCHEDULER_WRITE_LAG,
) -> dict[str, int]:
    """总括那一行要的几个数。`extra` = 表里有、文件里没有的（DB 建的动态任务）。"""
    problems = findings(
        entries, declared, now=now, missed_rounds=missed_rounds, write_lag=write_lag
    )
    names = set(declared)
    return {
        "declared": len(declared),
        "scheduled": len([e for e in entries if e.name in names]),
        "problems": len(problems),
        "crontab": len(
            [e for e in entries if e.name in names and e.judgeable is False]
        ),
        "extra": len([e for e in entries if e.name not in names]),
    }


def describe(
    entries: Sequence[Entry],
    declared: Sequence[str],
    *,
    now: datetime,
    missed_rounds: int = MISSED_ROUNDS,
    write_lag: float = SCHEDULER_WRITE_LAG,
) -> list[str]:
    """这一段的**唯一**渲染器（体检页正文用它）。返回的行不带缩进，缩进由调用方加。

    正常时只占两行（`·` 那条逐条清单只在真的有问题时才出现）：这一页是体检，不是流水
    ——每一轮都把 11 条印一遍，读者会开始跳过它，而跳过之后它再说什么都等于没说。
    """
    found = findings(
        entries, declared, now=now, missed_rounds=missed_rounds, write_lag=write_lag
    )
    stat = counts(
        entries, declared, now=now, missed_rounds=missed_rounds, write_lag=write_lag
    )
    newest = newest_entry(entries)

    head = f"调度表（beat）：声明的 {stat['declared']} 条"
    if found:
        head += f"里 {len(found)} 条没在正常跑"
    else:
        head += "都在调度表里、都没有超期"
    detail = []
    if newest is not None:
        detail.append(
            f"最近一条 {newest.name} 距今 {duration(newest.age_seconds(now=now) or 0.0)}"
        )
    others = []
    if stat["crontab"]:
        others.append(f"{stat['crontab']} 条无固定间隔（crontab，本页不定罪）")
    if stat["extra"]:
        others.append(f"{stat['extra']} 条不在文件里（DB 建的动态任务）")
    if others:
        detail.append("另有 " + "、".join(others))
    if detail:
        head += "（" + "；".join(detail) + "）"

    lines = [head]
    for finding in found:
        lines.append("   · " + _finding_line(finding, now=now))
    if found:
        lines.append(
            "   · **`beat_schedule` 只在 beat 启动时合并进 `PeriodicTask` 表一次**："
            "文件里后来加的条目，要重启 beat 才会被调度（这一页读的是「调度器此刻会发什么」）。"
        )
    lines.append(
        "   · 这一行只答「beat 把它**发出去了没有**」，且读数有 "
        f"{duration(write_lag)}的粒度下限（`last_run_at` 由调度器定期落库，不是每次派发都"
        "写）——所以「距今 3 分」不等于「3 分钟没跑」；而发出去了但任务每次都失败，在这里"
        "与成功长得一样（那看各段结论新不新：日报第④段）。"
    )
    return lines


def _finding_line(finding: Finding, *, now: datetime) -> str:
    entry = finding.entry
    text = f"{finding.name}：{finding.problem.display}"
    if entry is not None and entry.period_seconds is not None:
        text += f"（每 {duration(entry.period_seconds)}一轮"
        age = finding.age_seconds(now=now)
        if age is not None:
            text += f"，上次距今 {duration(age)}"
        text += "）"
    elif entry is not None:
        text += "（无固定间隔）"
    return text


def alert_body(found: Sequence[Finding], *, now: datetime) -> str:
    """那条即时消息的正文。``found`` 是 `alerts` 挑出来的（调用方保证它非空）。

    与 `describe` **刻意不是同一段文字**：体检页那段是给人对着 `beat_schedule` 逐条核对
    的（有总括、有「另有 N 条…」、有那一长句读数说明），而这条消息要的是**人动手做一件
    事**——所以说清单、说后果、说怎么办。

    三个细节都是必需的：

    - **逐条清单复用 `_finding_line`**：同一件事在页上与消息里必须逐字同源，否则「页上
      那条」与「消息里那条」会被读成两件事。清单按声明顺序（`findings` 定的）。
    - **「怎么办」只印出现的那几档**：三档的下一步动作不同（重启 beat / 重新启用 / 看
      日志），印一条用不上的出路，读的人会去找一个不存在的开关。
    - **末尾两段说清射程**：这一行只答「beat 把它**派出去了**没有」，且发出去了但每次都
      失败在这里与成功长得一样——不说，读者会把「发出去了」读成「好了」，那是比没有这
      条消息更坏的东西（与 `describe` 末尾那句同一个理由）。
    """
    lines = [
        f"🚨 调度表体检：{len(found)} 条 beat 条目没在正常跑",
        "后果：这几条管的事此刻停了，而从外面看与正常运行一模一样——它们的返回值进的是"
        "没人读的 Celery 结果后端，也没有第二处会主动提这件事。",
        "",
    ]
    lines.extend("· " + _finding_line(finding, now=now) for finding in found)

    shapes = {finding.problem for finding in found}
    remedies: list[str] = []
    if BeatProblem.UNSCHEDULED in shapes:
        remedies.append(
            "· 从未进入调度表 → **重启 beat**：`beat_schedule` 只在 beat 启动那一刻合并进"
            " `PeriodicTask` 表一次，往文件里加的条目在那之前**永远不会**被派发。"
        )
    if BeatProblem.DISABLED in shapes:
        remedies.append(
            "· 已被停用 → 库里那一行被关掉了（本仓库没有 admin：要有人去把它重新启用）。"
        )
    if BeatProblem.LATE in shapes:
        remedies.append(
            "· 已超期 → 别的条目在正常跑、只有它发不出去（这就是「最近一条不超期」那条例"
            "外的意思）：先重启 beat；重启后它还在清单里，就要看 beat 与 worker 的日志。"
        )
    if remedies:
        lines.append("")
        lines.append("怎么办：")
        lines.extend(remedies)

    lines.extend(
        [
            "",
            "这句话只答「beat 把它**派出去了**没有」：读数有 3 分钟粒度（`last_run_at` 由"
            "调度器定期落库，不是每次派发就写），而发出去了但任务每次都失败，在这里与成功"
            "长得一样——那看日报第④段的「结论新不新」。",
            "同一批问题当天不会每轮重喊（清单变了会再喊一条）；体检页：`/regime mech`。",
        ]
    )
    return "\n".join(lines)


def duration(seconds: float) -> str:
    """秒数说成人话。**只是给这一页读的**，不参与任何判断。

    天数是整数时不写「1 天 0 小时」：这一页上最该被一眼看见的是量级。
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"
