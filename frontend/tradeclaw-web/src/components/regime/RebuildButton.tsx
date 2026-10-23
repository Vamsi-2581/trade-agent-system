"use client";

/**
 * 池化表重算：投递 + 轮询 + 四种收场分开显示（P5 T5 的契约，ADR 0006）。
 *
 * 这个组件的全部风险是**把不同的收场说成同一种**：
 *
 *   - `reused`（输入未变，复用第 N 代）与 `duplicate`（并发撞上唯一约束）都是**成功的
 *     动作**，只是没有产生新记录。把它们显示成红色失败，会让人以为机制坏了，而真相
 *     恰恰相反——约束在起作用。所以这两档是中性色。
 *   - `failed` 才是红色，且要带上 `failure` **原文**（那是 `rebuild_pool` 自己写下的、
 *     带业务含义的那句话；Celery 的 traceback 是另一回事，标成 `celery_error`）。
 *   - `outcome === null` 不等于「重算中」：它说的是「投递成功但 worker 没取走，或结果
 *     已过期」——Celery 对未知 id 也回 PENDING，分不出来就如实写出来（`state_ambiguous`）。
 *
 * 文案一律取后端的 `display`：前端不重拼「复用第 N 代」「本次没有产生新记录」这些句子，
 * 它们的家在 `pool_rebuild.OUTCOME_DISPLAY` / `outcome_display()`。
 */

import { useEffect, useRef, useState } from "react";
import { regimeApi, type RegimeRebuildStatus } from "@/lib/api";
import { formatNumber } from "@/lib/format";

type Phase = "idle" | "confirm" | "working" | "settled";

type Tone = "green" | "neutral" | "red" | "info";

/** 五档收场 → 色。`building` 是「还在跑」，不是收场，给信息色。 */
const OUTCOME_TONE: Record<string, Tone> = {
  new_generation: "green",
  reused: "neutral",
  duplicate: "neutral",
  failed: "red",
  building: "info",
};

const TONE_CLASS: Record<Tone, string> = {
  green: "text-green",
  neutral: "text-text2",
  red: "text-red",
  info: "text-blue",
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * **第一次**读状态之前的等待（毫秒）。投递返回时 worker 几乎一定还没取走任务，所以这一刻
 * 读到的必然是 `outcome === null`——一次刚投递成功的重算会被当场说成「还没有 worker 取走」。
 *
 * 取 3 秒而不是回执里的 `poll_after_seconds`（60 秒）：那个数是「重算是分钟级，别一秒一问」
 * 的建议节拍，而实测一轮池化只跑 0.4–0.5 秒（40 条回测）。等 60 秒才出结果，会让人以为
 * 页面卡住了，而真相是它一直在等。3 秒同时是「worker 有取走的机会」的合理下限：等不到就
 * 继续按 `poll_after_seconds` 轮询，`pending` 那句话仍然会说出口。
 */
const FIRST_POLL_DELAY_MS = 3_000;

export interface RebuildButtonProps {
  /** 写权限（ADR 0005：这是界面纪律，不是安全边界）。非 staff 直接不渲染。 */
  isStaff: boolean;
  /** 一轮重算到达终局时回调（页面据此刷新矩阵与代际历史）。 */
  onFinished?: (status: RegimeRebuildStatus) => void;
  /**
   * **第一次**读状态的等待（毫秒）。默认 `FIRST_POLL_DELAY_MS`。
   *
   * 它和下面的 `pollIntervalMs` 是两个旋钮而不是一个：首次等待只需「worker 有机会取走」，
   * 而后续节拍是「还在跑时多久回来问一次」。
   */
  firstPollDelayMs?: number;
  /**
   * 后续轮询的间隔（毫秒）。默认用后端回执里的 `poll_after_seconds`——
   * 那是建议值，测试里才需要钉一个确定的间隔。
   */
  pollIntervalMs?: number;
}

export default function RebuildButton({
  isStaff,
  onFinished,
  firstPollDelayMs,
  pollIntervalMs,
}: RebuildButtonProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [status, setStatus] = useState<RegimeRebuildStatus | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mounted = useRef(true);
  const finished = useRef(onFinished);

  useEffect(() => {
    finished.current = onFinished;
  }, [onFinished]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, []);

  if (!isStaff) return null;

  const pollOnce = async (id: string, afterSeconds: number) => {
    try {
      const next = await regimeApi.getRebuildStatus(id);
      if (!mounted.current) return;
      setStatus(next);

      if (next.done) {
        setPhase("settled");
        finished.current?.(next);
        return;
      }
      if (next.outcome?.value !== "building") {
        // `outcome === null`（投递成功但没人取走 / 结果已过期）也是「不再等下去了」：
        // 继续轮询等于把「没开始」显示成「在跑」。
        setPhase("settled");
        return;
      }
      // 还在跑：按后端建议的节奏回来问。卡死的 building 不切断轮询——它可能只是慢，
      // 而 `stale_building` 那一句会一直挂在旁边（见下面的展示）。
      const interval =
        pollIntervalMs ?? Math.max(1, afterSeconds) * 1000;
      timer.current = setTimeout(() => void pollOnce(id, afterSeconds), interval);
    } catch (caught) {
      if (!mounted.current) return;
      setError(messageOf(caught));
      setPhase("settled");
    }
  };

  const startRebuild = async () => {
    setPhase("working");
    setError(null);
    setStatus(null);
    try {
      const dispatch = await regimeApi.rebuildPool();
      if (!mounted.current) return;
      setTaskId(dispatch.task_id);
      if (!dispatch.dispatched) {
        setError("后端没有接受这次投递（dispatched=false）。");
        setPhase("settled");
        return;
      }
      // **第一次读状态要等一会儿**（T9 冒烟实测出来的一个真缺陷）。
      //
      // 从前的写法是「投递完立刻读」。那一刻 worker 几乎一定还没把任务取走，Celery 回
      // PENDING、`outcome === null`，而下面那条判据会说「投递成功但还没有 worker 取走」
      // ——一次刚投递成功的重算被当场读成「没人在跑」，真正的收场（新代 / 复用）永远
      // 走不到。
      //
      // 这一句等待同时保住了下面那条判据的含义：等过之后**仍然**是 `outcome === null`，
      // 才是真的「没有 worker 取走，或结果已过期」——那时说这句话是有依据的。
      const firstDelay = firstPollDelayMs ?? FIRST_POLL_DELAY_MS;
      timer.current = setTimeout(
        () => void pollOnce(dispatch.task_id, dispatch.poll_after_seconds),
        firstDelay
      );
    } catch (caught) {
      if (!mounted.current) return;
      setError(messageOf(caught));
      setPhase("settled");
    }
  };

  const tone: Tone | null = status?.outcome
    ? OUTCOME_TONE[status.outcome.value] ?? "info"
    : null;

  return (
    <section
      data-testid="rebuild-panel"
      className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl p-3 text-xs font-mono space-y-2"
    >
      <div className="flex items-center gap-3">
        <button
          type="button"
          data-testid="rebuild-trigger"
          onClick={() => setPhase("confirm")}
          disabled={phase === "confirm" || phase === "working"}
          className="bg-bg3 hover:bg-bg2 disabled:opacity-40 text-text px-3 py-1.5 rounded-md"
        >
          重算池化表
        </button>
        <span className="text-text3">
          只重建池化表，不动切片；四种收场（新代 / 复用 / 并发 / 失败）分开显示。
        </span>
      </div>

      {phase === "confirm" && (
        <div
          data-testid="rebuild-confirm"
          className="border border-amber/40 bg-amber-dim rounded-md p-3 space-y-2"
        >
          <p className="text-amber">
            只重建池化表；切片若已陈旧需另行跑{" "}
            <code data-testid="rebuild-confirm-slices">recompute_regime_slices</code>。
          </p>
          <p className="text-text2">
            并发触发不会被拦：第二次会如实留下一条 duplicate 记录，那不是故障。
            输入未变时后端会复用当前代，本次不产生新记录。
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              data-testid="rebuild-confirm-ok"
              onClick={() => void startRebuild()}
              className="bg-amber/20 text-amber px-3 py-1 rounded-md"
            >
              确认重算
            </button>
            <button
              type="button"
              data-testid="rebuild-confirm-cancel"
              onClick={() => setPhase("idle")}
              className="bg-bg3 text-text2 px-3 py-1 rounded-md"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {phase === "working" && status === null && (
        <p data-testid="rebuild-progress" className="text-blue">
          重算已投递，正在轮询状态…
        </p>
      )}

      {error !== null && (
        <p data-testid="rebuild-error" className="text-red">
          重算请求失败：{error}
        </p>
      )}

      {status?.outcome && tone !== null && (
        <div
          data-testid="rebuild-outcome"
          data-tone={tone}
          data-outcome={status.outcome.value}
          className={`rounded-md border px-3 py-2 space-y-1 ${
            tone === "green"
              ? "border-green/40 bg-green-dim"
              : tone === "red"
                ? "border-red/40 bg-red-dim"
                : tone === "neutral"
                  ? "border-[rgba(255,255,255,0.12)] bg-bg3"
                  : "border-blue/40 bg-blue-dim"
          }`}
        >
          <p className={TONE_CLASS[tone]}>{status.outcome.display}</p>

          {status.outcome.value === "new_generation" && (
            <p data-testid="rebuild-cells-changed" className="text-text2">
              变化{" "}
              {formatNumber(
                status.cells_changed ?? status.generation?.cells_changed ?? 0,
                0
              )}{" "}
              格
            </p>
          )}

          {status.outcome.value === "failed" && (
            <p data-testid="rebuild-failure" className="text-red whitespace-pre-wrap break-all">
              {status.failure ||
                status.celery_error ||
                status.error ||
                "（后端没有给出失败原文）"}
            </p>
          )}

          {status.outcome.value === "building" && status.stale_building.stale && (
            <p data-testid="rebuild-stale" className="text-amber">
              {status.display ??
                `超过 ${status.stale_building.threshold_minutes} 分钟仍在重算中，检查 worker`}
            </p>
          )}

          {status.outcome.value === "building" && !status.stale_building.stale && (
            <p className="text-text2">
              已跑{" "}
              {status.stale_building.minutes === null
                ? "—"
                : formatNumber(status.stale_building.minutes, 0)}{" "}
              分钟（超过 {status.stale_building.threshold_minutes} 分钟会提示检查 worker）
            </p>
          )}
        </div>
      )}

      {status !== null && !status.outcome && (
        <div
          data-testid="rebuild-pending"
          className="border border-[rgba(255,255,255,0.12)] bg-bg3 rounded-md px-3 py-2 space-y-1"
        >
          <p className="text-text2">
            {status.display ?? "任务已投递但还没有 worker 取走，或这次的结果已经过期。"}
          </p>
          {status.state_ambiguous && (
            <p className="text-text3">
              Celery 分不出「结果过期」与「还没开始」：未知 id 与等待中的任务都回
              PENDING，所以这里两种可能都写着。
            </p>
          )}
          {taskId !== null && (
            <button
              type="button"
              data-testid="rebuild-recheck"
              onClick={() => {
                setPhase("working");
                setError(null);
                void pollOnce(taskId, 0);
              }}
              className="bg-bg2 text-text px-3 py-1 rounded-md"
            >
              再查一次
            </button>
          )}
        </div>
      )}
    </section>
  );
}
