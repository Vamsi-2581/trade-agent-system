/**
 * 重算按钮的契约（P5 子票 #44 的下半：二次确认 + 四/五种收场分开显示）。
 *
 * 这一块最容易犯的错是**把不同的收场归并成同一种**：
 *
 *   - `reused` / `duplicate` 是**成功但没有产生新记录**，必须是中性色——把它们显示成红色
 *     失败，会让人以为机制坏了，而那是唯一约束在正常工作（ADR 0006）。
 *   - `failed` 才是红色，且必须带 `failure` 原文。
 *   - `outcome === null`（投递成功但 worker 没取走，或结果已过期）**不是重算中**。
 *   - 非 staff 不渲染：这是界面纪律（ADR 0005），服务端仍然会挡。
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RegimeRebuildStatus } from "@/lib/api";

const rebuildPool = vi.hoisted(() => vi.fn());
const getRebuildStatus = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  regimeApi: { rebuildPool, getRebuildStatus },
}));

import RebuildButton from "../RebuildButton";

function status(over: Partial<RegimeRebuildStatus> = {}): RegimeRebuildStatus {
  return {
    task_id: "task-1",
    state: "SUCCESS",
    done: true,
    outcome: { value: "new_generation", display: "新代已就绪" },
    generation: null,
    stale_building: { stale: false, minutes: null, threshold_minutes: 30 },
    ...over,
  };
}

/** 点开二次确认，再确认。 */
function askAndConfirm() {
  fireEvent.click(screen.getByTestId("rebuild-trigger"));
  fireEvent.click(screen.getByTestId("rebuild-confirm-ok"));
}

beforeEach(() => {
  vi.clearAllMocks();
  rebuildPool.mockResolvedValue({
    dispatched: true,
    task_id: "task-1",
    poll_after_seconds: 30,
  });
  getRebuildStatus.mockResolvedValue(status());
});

describe("权限与二次确认", () => {
  it("非 staff 不渲染（界面纪律，不是安全边界）", () => {
    render(<RebuildButton isStaff={false} />);

    expect(screen.queryByTestId("rebuild-panel")).toBeNull();
    expect(screen.queryByTestId("rebuild-trigger")).toBeNull();
  });

  it("staff 看得到按钮；点了先出二次确认，写清只重建池化表", () => {
    render(<RebuildButton isStaff />);

    fireEvent.click(screen.getByTestId("rebuild-trigger"));

    const confirm = within(screen.getByTestId("rebuild-confirm"));
    expect(confirm.getByText(/只重建池化表；切片若已陈旧需另行跑/)).toBeTruthy();
    expect(confirm.getByText("recompute_regime_slices")).toBeTruthy();
    // 提示并发触发不是故障（避免人以为第二次会被拦下）
    expect(confirm.getByText(/duplicate 记录，那不是故障/)).toBeTruthy();
    // 二次确认期间还没有投递
    expect(rebuildPool).not.toHaveBeenCalled();
  });

  it("取消就什么都不发生", () => {
    render(<RebuildButton isStaff />);

    fireEvent.click(screen.getByTestId("rebuild-trigger"));
    fireEvent.click(screen.getByTestId("rebuild-confirm-cancel"));

    expect(screen.queryByTestId("rebuild-confirm")).toBeNull();
    expect(rebuildPool).not.toHaveBeenCalled();
  });

  it("确认后才投递，并且轮询的是回执里的 task_id", async () => {
    render(<RebuildButton isStaff />);
    askAndConfirm();

    await waitFor(() => expect(rebuildPool).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(getRebuildStatus).toHaveBeenCalledWith("task-1")
    );
  });
});

describe("四种收场分开显示", () => {
  it("新代就绪：绿色 + cells_changed", async () => {
    getRebuildStatus.mockResolvedValue(status({ cells_changed: 7 }));
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const outcome = within(await screen.findByTestId("rebuild-outcome"));
    expect(outcome.getByText("新代已就绪")).toBeTruthy();
    expect(screen.getByTestId("rebuild-outcome").getAttribute("data-tone")).toBe("green");
    expect(screen.getByTestId("rebuild-outcome").className).toContain("bg-green-dim");
    expect(screen.getByTestId("rebuild-cells-changed").textContent).toContain("7");
  });

  it("reused：中性色，说清「本次没有产生新记录」", async () => {
    getRebuildStatus.mockResolvedValue(
      status({
        outcome: {
          value: "reused",
          display: "输入未变，复用第 5 代，本次没有产生新记录",
        },
        cells_changed: null,
      })
    );
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const outcome = await screen.findByTestId("rebuild-outcome");
    expect(outcome.getAttribute("data-tone")).toBe("neutral");
    expect(outcome.textContent).toContain("输入未变，复用第 5 代，本次没有产生新记录");
    // 中性色不是红色：复用不是失败
    expect(outcome.className).not.toContain("bg-red");
    expect(screen.queryByTestId("rebuild-failure")).toBeNull();
  });

  it("duplicate：中性色（唯一约束在起作用，不是红色故障）", async () => {
    getRebuildStatus.mockResolvedValue(
      status({
        outcome: {
          value: "duplicate",
          display: "并发触发：已有另一代先一步就绪，本次未替换当前代（不是故障）",
        },
        cells_changed: null,
      })
    );
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const outcome = await screen.findByTestId("rebuild-outcome");
    expect(outcome.getAttribute("data-tone")).toBe("neutral");
    expect(outcome.textContent).toContain("并发触发");
    expect(outcome.textContent).toContain("不是故障");
    expect(outcome.className).not.toContain("bg-red");
  });

  it("failed：红色 + failure 原文", async () => {
    getRebuildStatus.mockResolvedValue(
      status({
        state: "FAILURE",
        outcome: { value: "failed", display: "重算失败" },
        failure: "同一输入指纹已有一代就绪",
      })
    );
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const outcome = await screen.findByTestId("rebuild-outcome");
    expect(outcome.getAttribute("data-tone")).toBe("red");
    expect(outcome.textContent).toContain("重算失败");
    expect(screen.getByTestId("rebuild-failure").textContent).toBe(
      "同一输入指纹已有一代就绪"
    );
  });

  it("还在跑：轮询到不再是 building 才停", async () => {
    getRebuildStatus
      .mockResolvedValueOnce(
        status({
          state: "STARTED",
          done: false,
          outcome: { value: "building", display: "重算中" },
          stale_building: { stale: false, minutes: 2, threshold_minutes: 30 },
        })
      )
      .mockResolvedValueOnce(status({ cells_changed: 3 }));

    render(<RebuildButton isStaff pollIntervalMs={5} />);
    askAndConfirm();

    await waitFor(() => expect(getRebuildStatus).toHaveBeenCalledTimes(2));
    const outcome = await screen.findByTestId("rebuild-outcome");
    await waitFor(() =>
      expect(outcome.getAttribute("data-tone")).toBe("green")
    );
    expect(outcome.textContent).toContain("新代已就绪");
  });

  it("卡死的 building：把「超过 N 分钟仍在重算中，检查 worker」挂出来", async () => {
    getRebuildStatus.mockResolvedValue(
      status({
        state: "STARTED",
        done: false,
        outcome: { value: "building", display: "重算中" },
        stale_building: { stale: true, minutes: 47, threshold_minutes: 30 },
        display: "超过 30 分钟仍在重算中，检查 worker",
      })
    );
    render(<RebuildButton isStaff pollIntervalMs={10_000} />);
    askAndConfirm();

    const stale = await screen.findByTestId("rebuild-stale");
    expect(stale.textContent).toContain("超过 30 分钟仍在重算中，检查 worker");
    expect(screen.getByTestId("rebuild-outcome").getAttribute("data-tone")).toBe("info");
  });

  it("outcome=null 不是「重算中」：如实说还没开始或已过期，可再查一次", async () => {
    getRebuildStatus.mockResolvedValue(
      status({
        state: "PENDING",
        done: false,
        outcome: null,
        generation: null,
        display:
          "任务已投递但还没有 worker 取走，或这次的结果已经过期（不是「重算中」）",
        state_ambiguous: true,
      })
    );
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const pending = within(await screen.findByTestId("rebuild-pending"));
    expect(pending.getByText(/还没有 worker 取走/)).toBeTruthy();
    expect(pending.getByText(/Celery 分不出「结果过期」与「还没开始」/)).toBeTruthy();
    expect(screen.queryByTestId("rebuild-outcome")).toBeNull();

    fireEvent.click(screen.getByTestId("rebuild-recheck"));
    await waitFor(() => expect(getRebuildStatus).toHaveBeenCalledTimes(2));
  });
});

describe("请求失败", () => {
  it("投递失败是红字原文，不是白页", async () => {
    rebuildPool.mockRejectedValue(new Error("API error: 503"));
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const error = await screen.findByTestId("rebuild-error");
    expect(error.textContent).toContain("API error: 503");
    expect(screen.queryByTestId("rebuild-outcome")).toBeNull();
  });

  it("轮询失败也如实说，不假装重算成功", async () => {
    getRebuildStatus.mockRejectedValue(new Error("API error: 500"));
    render(<RebuildButton isStaff />);
    askAndConfirm();

    const error = await screen.findByTestId("rebuild-error");
    expect(error.textContent).toContain("API error: 500");
  });
});
