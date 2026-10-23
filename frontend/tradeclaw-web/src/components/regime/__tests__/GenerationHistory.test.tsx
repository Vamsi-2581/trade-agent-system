/**
 * 代际历史的契约（P5 子票 #44 的中段：列表 + 与上一代的差异）。
 *
 * 两个风险：
 *
 *   1. **「当前代」标记来自页面传进来的 id**（`overview.pool.generation.id`）。代际端点自己
 *      不标当前代；组件若去「猜」哪一条是当前代（比如取第一行），两处各查一次之间翻了一代
 *      就会给出两条都自称当前的行。
 *   2. **变化格数有两个数**：记录里的 `recorded_cells_changed` 与逐格差异的 `counts` 合计。
 *      两者必须相等，不相等时页面必须当场说明——否则「说变了 3 格、列表里有 5 条」没人
 *      会报错。
 */

import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RegimeGeneration, RegimeGenerations } from "@/lib/api";

const getGenerations = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  regimeApi: { getGenerations },
}));

import GenerationHistory from "../GenerationHistory";

const labelled = (value: string, display: string) => ({ value, display });

function generation(id: number, over: Partial<RegimeGeneration> = {}): RegimeGeneration {
  return {
    id,
    status: labelled("ready", "已就绪"),
    actor_kind: labelled("web", "网页"),
    actor_name: "eazlong",
    pool_version: 4,
    started_at: "2026-10-22T10:00:00Z",
    finished_at: "2026-10-22T10:04:43Z",
    candidates: 18,
    results_used: 42,
    cells_total: 72,
    cells_changed: 7,
    exclusions: { no_merge_inputs: 0, unusable_capital: 0, no_payload: 0 },
    failure: "",
    outcome: labelled("new_generation", "新代已就绪"),
    stale_building: { stale: false, minutes: null, threshold_minutes: 30 },
    ...over,
  };
}

const generations = [
  generation(12),
  generation(11, {
    cells_changed: 0,
    outcome: labelled("reused", "输入未变，复用第 11 代，本次没有产生新记录"),
  }),
  generation(10, {
    status: labelled("failed", "已失败"),
    failure: "同一输入指纹已有一代就绪",
    outcome: labelled("duplicate", "并发触发：已有另一代先一步就绪（不是故障）"),
  }),
];

const diff: NonNullable<RegimeGenerations["diff"]> = {
  generation: 12,
  previous_generation: 11,
  previous_finished_at: "2026-10-21T09:00:00Z",
  recorded_cells_changed: 3,
  counts: { changed: 1, added: 1, removed: 1 },
  cells: [
    {
      key: "7|downtrend",
      strategy: { id: "7", name: "rsi_cross" },
      regime: labelled("downtrend", "下行趋势"),
      kind: "changed",
      before: {
        state: labelled("fit", "适用"),
        reason: labelled("", ""),
        source: labelled("strategy", "本策略切片"),
      },
      after: {
        state: labelled("unfit", "不适用"),
        reason: labelled("insufficient_evidence", "证据不足"),
        source: labelled("strategy", "本策略切片"),
      },
    },
    {
      key: "9|range",
      strategy: { id: "9", name: "mean_revert" },
      regime: labelled("range", "箱体"),
      kind: "added",
      before: null,
      after: {
        state: labelled("neutral", "中性"),
        reason: labelled("archetype_evidence", "借用原型样本"),
        source: labelled("archetype", "原型兜底"),
      },
    },
    {
      key: "10|range",
      strategy: { id: "10", name: "newborn" },
      regime: labelled("range", "箱体"),
      kind: "removed",
      before: {
        state: labelled("unknown", "未知"),
        reason: labelled("no_regime_days", "该阶段没出现"),
        source: labelled("strategy", "本策略切片"),
      },
      after: null,
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  getGenerations.mockResolvedValue({
    count: generations.length,
    page: 1,
    page_size: 20,
    has_next: false,
    results: generations,
    diff,
  } satisfies RegimeGenerations);
});

describe("代际列表", () => {
  it("每一代把「谁、何时、算的什么」都摆出来", () => {
    render(<GenerationHistory generations={generations} currentGenerationId={12} />);

    const ready = within(screen.getByTestId("generation-row-12"));
    // 状态 / 触发方 / 收场都是后端的 display
    expect(ready.getByText("已就绪")).toBeTruthy();
    expect(ready.getByText("网页 · eazlong")).toBeTruthy();
    expect(ready.getByText("新代已就绪")).toBeTruthy();
    // 计数走 format.ts（候选 18 / 用上 42 / 格子 72 / 变化 7）
    expect(ready.getByText("18")).toBeTruthy();
    expect(ready.getByText("42")).toBeTruthy();
    expect(ready.getByText("72")).toBeTruthy();
    expect(ready.getByText("7")).toBeTruthy();

    const reused = within(screen.getByTestId("generation-row-11"));
    expect(
      reused.getByText("输入未变，复用第 11 代，本次没有产生新记录")
    ).toBeTruthy();

    const duplicate = within(screen.getByTestId("generation-row-10"));
    expect(duplicate.getByText("已失败")).toBeTruthy();
    expect(
      duplicate.getByText("并发触发：已有另一代先一步就绪（不是故障）")
    ).toBeTruthy();
    // 失败原文照接口给
    expect(duplicate.getByText("同一输入指纹已有一代就绪")).toBeTruthy();
  });

  it("「当前代」标记只挂在 overview 给的那一代上", () => {
    render(<GenerationHistory generations={generations} currentGenerationId={12} />);

    expect(
      within(screen.getByTestId("generation-row-12")).getByTestId(
        "current-generation-badge"
      )
    ).toBeTruthy();
    expect(
      within(screen.getByTestId("generation-row-11")).queryByTestId(
        "current-generation-badge"
      )
    ).toBeNull();
    expect(
      within(screen.getByTestId("generation-row-10")).queryByTestId(
        "current-generation-badge"
      )
    ).toBeNull();
  });

  it("卡死的 building 用后端给的阈值说出来", () => {
    render(
      <GenerationHistory
        generations={[
          generation(13, {
            status: labelled("building", "重算中"),
            finished_at: null,
            outcome: labelled("building", "重算中"),
            stale_building: { stale: true, minutes: 47, threshold_minutes: 30 },
          }),
        ]}
      />
    );

    const stale = screen.getByTestId("generation-stale-13");
    expect(stale.textContent).toContain("超过 30 分钟仍在重算中，检查 worker");
  });

  it("没有代时如实说，不渲染空表", () => {
    render(<GenerationHistory generations={[]} />);

    expect(screen.getByTestId("generations-empty").textContent).toContain(
      "还没有任何一代池化表"
    );
    expect(screen.queryByTestId("generation-history")).toBeNull();
  });
});

describe("与上一代的差异", () => {
  it("选中某代才拉差异，参数是 generation", async () => {
    render(<GenerationHistory generations={generations} />);
    expect(getGenerations).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByTestId(/generation-diff-/)[0]);

    await waitFor(() => expect(getGenerations).toHaveBeenCalledTimes(1));
    expect(getGenerations).toHaveBeenCalledWith({ generation: 12 });

    const block = within(await screen.findByTestId("generation-diff"));
    // 记录的变化格数与逐格合计都摆出来
    expect(block.getByTestId("diff-recorded").textContent).toBe("3");
    expect(block.getByTestId("diff-counts").textContent).toBe("3");
    expect(block.getByText(/改动 1 · 新增 1 · 移除 1/)).toBeTruthy();
  });

  it("逐格 diff 给出上一代与这一代的三元组", async () => {
    render(<GenerationHistory generations={generations} />);
    fireEvent.click(screen.getAllByTestId(/generation-diff-/)[0]);

    const block = within(await screen.findByTestId("generation-diff"));
    expect(block.getByText(/第 #12 代/)).toBeTruthy();
    expect(block.getByText(/第 #11 代/)).toBeTruthy();

    const changed = within(block.getByTestId("diff-cell-7|downtrend"));
    expect(changed.getByText("改动")).toBeTruthy();
    expect(changed.getByText("适用")).toBeTruthy();
    expect(changed.getByText("不适用：证据不足")).toBeTruthy();
    expect(changed.getByText("rsi_cross")).toBeTruthy();

    const added = within(block.getByTestId("diff-cell-9|range"));
    expect(added.getByText("新增")).toBeTruthy();
    expect(added.getByText("—")).toBeTruthy(); // 上一代没有这一格

    const removed = within(block.getByTestId("diff-cell-10|range"));
    expect(removed.getByText("移除")).toBeTruthy();
    expect(removed.getByText("未知：该阶段没出现")).toBeTruthy();
  });

  it("记录值与逐格合计不一致时当场说明（那是口径分叉）", async () => {
    getGenerations.mockResolvedValue({
      count: generations.length,
      page: 1,
      page_size: 20,
      has_next: false,
      results: generations,
      diff: { ...diff, recorded_cells_changed: 9 },
    } satisfies RegimeGenerations);

    render(<GenerationHistory generations={generations} />);
    fireEvent.click(screen.getAllByTestId(/generation-diff-/)[0]);

    const diverged = await screen.findByTestId("diff-diverged");
    expect(diverged.textContent).toContain("9");
    expect(diverged.textContent).toContain("3");
    expect(diverged.textContent).toContain("口径分叉");
  });

  it("第一代没有可比的上一代时说清楚", async () => {
    getGenerations.mockResolvedValue({
      count: generations.length,
      page: 1,
      page_size: 20,
      has_next: false,
      results: generations,
      diff: {
        ...diff,
        previous_generation: null,
        previous_finished_at: null,
        recorded_cells_changed: 3,
      },
    } satisfies RegimeGenerations);

    render(<GenerationHistory generations={generations} />);
    fireEvent.click(screen.getAllByTestId(/generation-diff-/)[0]);

    expect(
      (await screen.findByTestId("diff-first-generation")).textContent
    ).toContain("没有可比的上一代");
  });

  it("没有逐格差异时说「没有变化」，不是空白", async () => {
    getGenerations.mockResolvedValue({
      count: generations.length,
      page: 1,
      page_size: 20,
      has_next: false,
      results: generations,
      diff: { ...diff, recorded_cells_changed: 0, counts: { changed: 0, added: 0, removed: 0 }, cells: [] },
    } satisfies RegimeGenerations);

    render(<GenerationHistory generations={generations} />);
    fireEvent.click(screen.getAllByTestId(/generation-diff-/)[0]);

    expect((await screen.findByTestId("diff-empty")).textContent).toContain("没有变化");
  });

  it("差异取数失败时如实报错，不假装没有差异", async () => {
    getGenerations.mockRejectedValue(new Error("API error: 500"));
    render(<GenerationHistory generations={generations} />);

    fireEvent.click(screen.getAllByTestId(/generation-diff-/)[0]);

    const error = await screen.findByTestId("generation-diff-error");
    expect(error.textContent).toContain("API error: 500");
    expect(screen.queryByTestId("diff-table")).toBeNull();
  });

  it("连点两代时，先到的旧响应不许盖住后到的", async () => {
    let releaseFirst: (value: unknown) => void = () => {};
    const first = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    getGenerations
      .mockImplementationOnce(() => first)
      .mockResolvedValueOnce({
        count: generations.length,
        page: 1,
        page_size: 20,
        has_next: false,
        results: generations,
        diff: { ...diff, generation: 11, previous_generation: 10 },
      });

    render(<GenerationHistory generations={generations} />);
    fireEvent.click(screen.getByTestId("generation-diff-12"));
    fireEvent.click(screen.getByTestId("generation-diff-11"));

    expect((await screen.findByTestId("generation-diff")).textContent).toContain(
      "第 #11 代"
    );

    // 第一代那次（#12）晚到：不许把它盖回来
    releaseFirst({
      count: generations.length,
      page: 1,
      page_size: 20,
      has_next: false,
      results: generations,
      diff,
    });
    await waitFor(() => expect(getGenerations).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("generation-diff").textContent).toContain("第 #11 代");
    expect(screen.getByTestId("generation-diff").textContent).not.toContain("第 #12 代");
  });
});
