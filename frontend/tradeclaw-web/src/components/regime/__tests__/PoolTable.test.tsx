/**
 * 池化表矩阵的契约（P5 子票 #44 的上半：矩阵、五态颜色、角标、行内展开、冷启动）。
 *
 * 这一块的风险全部在「显示层自己发明了一件事」：
 *
 *   1. **五种状态必须各一色**，认不出来的落中性灰、绝不落成绿色——把一格 `unknown` 染成
 *      「适用」的绿，会让人以为机制放行了某条策略。
 *   2. **`state` / `reason` 的中文取后端给的 `display`**。夹具刻意把 display 写成
 *      「后端渲染：……」，于是任何前端硬编码的「适用 / 不适用」都会在这一条上露馅。
 *   3. **展开出来的依据逐字来自 `evidence`**：哪几个回测 id、什么区间、合并成交数、品种、
 *      原型留痕（原文字 + 命中词）。这些数一个都不许前端推导（证据里没有「每份回测多少笔」
 *      这件事，就一个数都不许出现）。
 *   4. **冷启动不是空表格**：没有当前代时给后端那句 `display` 与一条重算指引。
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type {
  Labelled,
  RegimeGeneration,
  RegimePool,
  RegimePoolCell,
} from "@/lib/api";

import PoolTable from "../PoolTable";

// ── 夹具 ──

const labelled = (value: string, display?: string): Labelled => ({
  value,
  display: display ?? `后端渲染：${value}`,
});

/** 后端 `strategy_lifecycle.LIFECYCLE_DISPLAY` 的两句原样文本（前端**不许**另写一份）。 */
const ACTIVE_LIFECYCLE = labelled("active", "在册（机制会管它）");
const RETIRED_LIFECYCLE = labelled("retired", "已退役（机制不再管它）");

const REGIMES = [
  labelled("high_vol", "高波动"),
  labelled("downtrend", "下行趋势"),
  labelled("uptrend", "上行趋势"),
  labelled("range", "箱体"),
];

const generation: RegimeGeneration = {
  id: 12,
  status: labelled("ready", "已就绪"),
  actor_kind: labelled("web", "网页"),
  actor_name: "eazlong",
  pool_version: 4,
  started_at: "2026-10-22T10:00:00Z",
  finished_at: "2026-10-22T10:04:43Z",
  candidates: 18,
  results_used: 42,
  cells_total: 60,
  cells_changed: 7,
  exclusions: { no_merge_inputs: 3, unusable_capital: 0, no_payload: 1 },
  failure: "",
  outcome: labelled("new_generation", "新代已就绪"),
  stale_building: { stale: false, minutes: null, threshold_minutes: 30 },
};

function cell(
  strategyId: string,
  strategyName: string,
  regime: Labelled,
  state: Labelled,
  over: Partial<RegimePoolCell> = {}
): RegimePoolCell {
  return {
    key: `${strategyId}|${regime.value}`,
    strategy: {
      id: strategyId,
      name: strategyName,
      lifecycle: ACTIVE_LIFECYCLE,
      latest_change: null,
    },
    regime,
    source: labelled("strategy", "本策略切片"),
    state,
    reason: labelled("", ""),
    needs_review: false,
    evidence: {},
    ...over,
  };
}

const fitCell = cell("7", "rsi_cross", REGIMES[0], labelled("fit"), {
  evidence: {
    pool_version: 4,
    results: [
      {
        result_id: 101,
        symbol: "BTC/USDT",
        window: { start: "2026-01-01", end: "2026-06-30" },
      },
      {
        result_id: 102,
        symbol: "ETH/USDT",
        window: { start: "2026-02-01", end: "2026-07-31" },
      },
    ],
    symbols: ["BTC/USDT", "ETH/USDT"],
    trades: 11693,
    months: 24,
    regime_days: 311,
    threshold: { min_trades: 30, min_months: 6, met: true },
    metrics: { sharpe: 1.1 },
    full_sample: { sharpe: 0.4 },
    conflict: null,
  },
});

const reviewCell = cell("8", "volume_reclaim", REGIMES[1], labelled("unfit"), {
  needs_review: true,
  reason: labelled("direction_conflict", "方向冲突"),
  evidence: {
    trades: 44,
    months: 7,
    regime_days: 90,
    symbols: ["BTC/USDT"],
    threshold: { min_trades: 30, min_months: 6, met: true },
    conflict: {
      symbol: "SOL/USDT",
      symbol_state: "fit",
      pooled_state: "unfit",
      symbol_trades: 31,
      symbol_months: 6,
      symbol_results: [201],
    },
  },
});

const archetypeCell = cell("9", "mean_revert", REGIMES[2], labelled("neutral"), {
  source: labelled("archetype", "原型兜底"),
  reason: labelled("archetype_evidence", "借用原型样本"),
  evidence: {
    trades: 344,
    months: 12,
    regime_days: 200,
    symbols: ["BTC/USDT", "ETH/USDT"],
    threshold: { min_trades: 30, min_months: 6, met: true },
    archetype: {
      name: "mean_reversion",
      source: "text",
      raw_text: "策略类型：均值回归型",
      matched_word: "均值回归",
    },
    own_sample: {
      trades: 0,
      months: 0,
      regime_days: 3,
      results: [{ result_id: 301, symbol: "BTC/USDT", window: { start: "2026-03-01", end: "2026-04-01" } }],
    },
  },
});

const unknownCell = cell("10", "newborn", REGIMES[3], labelled("unknown"));
const blanketCell = cell("11", "blanket_guard", REGIMES[0], labelled("blanket"));

const pool: RegimePool = {
  has_generation: true,
  display: "",
  generation,
  regimes: REGIMES,
  cells: [
    fitCell,
    reviewCell,
    archetypeCell,
    unknownCell,
    blanketCell,
    // 同一策略的第二格：验证同一行里多格可各自展开
    cell("7", "rsi_cross", REGIMES[3], labelled("neutral")),
  ],
};

// ── 矩阵与五态颜色 ──

describe("矩阵", () => {
  it("列序取后端的 regimes 数组，不是前端另排一份", () => {
    render(<PoolTable pool={pool} />);

    const table = screen.getByTestId("pool-table");
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((node) => node.textContent ?? "");
    expect(headers).toEqual(["策略", "高波动", "下行趋势", "上行趋势", "箱体"]);
  });

  it("五种状态各一色，认不出来的不落成绿色", () => {
    render(<PoolTable pool={pool} />);

    expect(screen.getByTestId(`pool-cell-${fitCell.key}`).getAttribute("data-tone")).toBe(
      "green"
    );
    expect(
      screen.getByTestId(`pool-cell-${reviewCell.key}`).getAttribute("data-tone")
    ).toBe("red");
    expect(
      screen.getByTestId(`pool-cell-${archetypeCell.key}`).getAttribute("data-tone")
    ).toBe("amber");
    expect(
      screen.getByTestId(`pool-cell-${unknownCell.key}`).getAttribute("data-tone")
    ).toBe("muted");
    expect(
      screen.getByTestId(`pool-cell-${blanketCell.key}`).getAttribute("data-tone")
    ).toBe("blue");

    // 颜色真的落到了 class 上（只给 data-tone 不染色就是「标记有了、颜色没了」）
    expect(screen.getByTestId(`pool-cell-${fitCell.key}`).className).toContain(
      "bg-green-dim"
    );
    expect(screen.getByTestId(`pool-cell-${unknownCell.key}`).className).not.toContain(
      "bg-green"
    );
  });

  it("中文取后端的 display，前端不翻译 slug", () => {
    render(<PoolTable pool={pool} />);

    expect(screen.getByText("后端渲染：fit")).toBeTruthy();
    expect(screen.getByText("后端渲染：unfit")).toBeTruthy();
    // 硬编码「适用」一旦出现就是第二个说法
    expect(screen.queryByText("适用")).toBeNull();
    expect(screen.queryByText("不适用")).toBeNull();
    expect(screen.getByText("方向冲突")).toBeTruthy();
  });

  it("needs_review 加角标，只有那一格有", () => {
    render(<PoolTable pool={pool} />);

    const reviewed = screen.getByTestId(`pool-cell-${reviewCell.key}`);
    expect(within(reviewed).getByTestId("needs-review-badge")).toBeTruthy();
    const plain = screen.getByTestId(`pool-cell-${fitCell.key}`);
    expect(within(plain).queryByTestId("needs-review-badge")).toBeNull();
  });

  it("source=archetype 加「原型兜底」标", () => {
    render(<PoolTable pool={pool} />);

    const fallback = screen.getByTestId(`pool-cell-${archetypeCell.key}`);
    expect(within(fallback).getByTestId("archetype-badge")).toBeTruthy();
    expect(within(fallback).getByTestId("archetype-badge").textContent).toBe("原型兜底");
    expect(
      within(screen.getByTestId(`pool-cell-${fitCell.key}`)).queryByTestId("archetype-badge")
    ).toBeNull();
  });
});

// ── 行内展开 ──

describe("行内展开", () => {
  it("点一格展开它的证据，再点收起", () => {
    render(<PoolTable pool={pool} />);
    const cell = screen.getByTestId(`pool-cell-${fitCell.key}`);
    expect(screen.queryByTestId(`pool-cell-detail-${fitCell.key}`)).toBeNull();

    fireEvent.click(cell);
    expect(screen.getByTestId(`pool-cell-detail-${fitCell.key}`)).toBeTruthy();
    expect(cell.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(cell);
    expect(screen.queryByTestId(`pool-cell-detail-${fitCell.key}`)).toBeNull();
  });

  it("展开内容逐字来自 evidence：回测 id / 区间 / 合并成交数 / 品种", () => {
    render(<PoolTable pool={pool} initialExpanded={new Set([fitCell.key])} />);

    const evidence = within(screen.getByTestId("pool-cell-evidence"));
    // 哪几个回测结果
    expect(evidence.getByText("#101")).toBeTruthy();
    expect(evidence.getByText("#102")).toBeTruthy();
    // 覆盖区间
    expect(evidence.getByText("2026-01-01 ~ 2026-06-30")).toBeTruthy();
    expect(evidence.getByText("2026-02-01 ~ 2026-07-31")).toBeTruthy();
    // 合并成交数（走 format.ts，带千分位）
    expect(evidence.getByText("11,693")).toBeTruthy();
    // 品种
    expect(evidence.getByText("BTC/USDT、ETH/USDT")).toBeTruthy();
    // 门槛（达标与否是后端给的布尔，不是前端拿数比大小）
    expect(evidence.getByTestId("pool-evidence-threshold").textContent).toContain(
      "已达标"
    );
    expect(evidence.getByText(/成交 ≥ 30 且覆盖 ≥ 6 个月/)).toBeTruthy();
  });

  it("原型兜底：原文字 + 命中词原样显示，并带上本策略自己的样本", () => {
    render(<PoolTable pool={pool} initialExpanded={new Set([archetypeCell.key])} />);

    const evidence = within(screen.getByTestId("pool-cell-evidence"));
    expect(evidence.getByTestId("pool-evidence-raw-text").textContent).toBe(
      "策略类型：均值回归型"
    );
    expect(evidence.getByTestId("pool-evidence-matched-word").textContent).toBe("均值回归");
    expect(evidence.getByText("mean_reversion")).toBeTruthy();
    // 自己被退回的那份样本（0 笔也要显示成 0，不是「—」）
    const own = within(evidence.getByTestId("pool-evidence-own-sample"));
    expect(own.getByText(/成交/)).toBeTruthy();
    expect(own.getByText(/回测/)).toBeTruthy();
    expect(own.getByText("#301")).toBeTruthy();
  });

  it("待复核的格子把冲突品种一起说清", () => {
    render(<PoolTable pool={pool} initialExpanded={new Set([reviewCell.key])} />);

    const note = within(screen.getByTestId("pool-cell-review-note"));
    expect(note.getByText(/不产生停用建议/)).toBeTruthy();
    expect(note.getByText("SOL/USDT")).toBeTruthy();
    expect(note.getByText(/31 笔/)).toBeTruthy();
  });

  it("同一策略的两格可以各自展开", () => {
    render(
      <PoolTable
        pool={pool}
        initialExpanded={new Set([fitCell.key, "7|range"])}
      />
    );

    expect(screen.getByTestId(`pool-cell-detail-${fitCell.key}`)).toBeTruthy();
    expect(screen.getByTestId("pool-cell-detail-7|range")).toBeTruthy();
  });

  it("原始证据整份摊开，认不出来的键也不吞掉", () => {
    render(<PoolTable pool={pool} initialExpanded={new Set([fitCell.key])} />);

    const raw = within(screen.getByTestId("pool-evidence-raw"));
    expect(raw.getByText(/"pool_version": 4/)).toBeTruthy();
  });
});

// ── 冷启动与空态 ──

describe("空态", () => {
  it("冷启动：给后端那句 display 与重算指引，不是空白表格", () => {
    render(
      <PoolTable
        pool={{
          has_generation: false,
          display:
            "还没有任何一代池化表：先跑一次池化重算（POST /api/regime/pool/rebuild/ 或 recompute_regime_slices）",
          generation: null,
          regimes: REGIMES,
          cells: [],
        }}
      />
    );

    const empty = within(screen.getByTestId("pool-empty"));
    expect(empty.getByText(/还没有任何一代池化表/)).toBeTruthy();
    // 后端那句 display 与指引里各出现一次，所以按「至少一处」断言
    expect(empty.getAllByText(/recompute_regime_slices/).length).toBeGreaterThanOrEqual(1);
    expect(empty.getByText(/「重算池化表」/)).toBeTruthy();
    expect(screen.queryByTestId("pool-table")).toBeNull();
  });

  it("有代但没有格子时如实说，不渲染一张空表", () => {
    render(<PoolTable pool={{ ...pool, cells: [] }} />);

    expect(screen.getByTestId("pool-no-cells")).toBeTruthy();
    expect(screen.queryByTestId("pool-table")).toBeNull();
  });
});

// ── 排除计数 ──

describe("排除计数", () => {
  it("三个键恒在（计数为 0 也在），按原因分开且不翻译", () => {
    render(<PoolTable pool={pool} />);

    const exclusions = within(screen.getByTestId("pool-exclusions"));
    expect(exclusions.getByTestId("pool-exclusion-no_merge_inputs").textContent).toContain(
      "3"
    );
    // 0 也在：某个原因从没出现过 vs 这个键被改名了，要能分开
    expect(
      exclusions.getByTestId("pool-exclusion-unusable_capital").textContent
    ).toContain("0");
    expect(exclusions.getByTestId("pool-exclusion-no_payload").textContent).toContain("1");
    expect(exclusions.getByText("no_merge_inputs")).toBeTruthy();
  });
});

// ── 行状态（已退役）与「删除策略」动作槽 ──

/** 一条退役流水（`StrategyLifecycleChange` 的读面形状）。 */
const RETIREMENT = {
  at: "2026-10-23T08:00:00Z",
  actor_kind: labelled("web", "网页"),
  actor_name: "ops",
  from_active: ACTIVE_LIFECYCLE,
  to_active: RETIRED_LIFECYCLE,
  reason: "人工退役（从机制里删除）：rsi_cross",
};

/** 同一张表，但 rsi_cross 那一行已经退役。 */
function retiredPool(): RegimePool {
  const retired = cell("7", "rsi_cross", REGIMES[0], labelled("neutral"), {
    strategy: {
      id: "7",
      name: "rsi_cross",
      lifecycle: RETIRED_LIFECYCLE,
      latest_change: RETIREMENT,
    },
  });
  return { ...pool, cells: [retired, ...pool.cells.filter((c) => c.strategy.id !== "7")] };
}

describe("行状态", () => {
  it("在册的行没有角标", () => {
    render(<PoolTable pool={pool} />);

    expect(screen.queryByTestId("retired-badge-7")).toBeNull();
  });

  it("已退役的行带角标，且角标文本取后端的 display", () => {
    render(<PoolTable pool={retiredPool()} />);

    const badge = screen.getByTestId("retired-badge-7");
    expect(badge.textContent).toBe(RETIRED_LIFECYCLE.display);
    // 谁、何时、为什么 —— 角标上挂得住，人 hover 得出来（否则「删除了」是一条无主记录）
    expect(badge.getAttribute("title")).toContain("ops");
    expect(badge.getAttribute("title")).toContain("网页");
    expect(badge.getAttribute("title")).toContain(RETIREMENT.reason);
  });

  it("退役的行仍然在表里（记录保留），不是从这一代里消失", () => {
    render(<PoolTable pool={retiredPool()} />);

    expect(screen.getByTestId("pool-row-7")).toBeTruthy();
    expect(screen.getByTestId(`pool-cell-${"7"}|${REGIMES[0].value}`)).toBeTruthy();
  });
});

describe("操作列", () => {
  it("不给动作槽时与从前逐像素一致：没有「操作」列", () => {
    render(<PoolTable pool={pool} />);

    const headers = within(screen.getByTestId("pool-table"))
      .getAllByRole("columnheader")
      .map((node) => node.textContent ?? "");
    expect(headers).toEqual(["策略", "高波动", "下行趋势", "上行趋势", "箱体"]);
    expect(screen.queryByTestId("pool-row-action-7")).toBeNull();
  });

  it("给了动作槽就多一列，且每一行都渲染", () => {
    render(
      <PoolTable
        pool={pool}
        renderStrategyAction={(strategy) => (
          <button data-testid={`fake-action-${strategy.id}`}>{strategy.name}</button>
        )}
      />
    );

    const headers = within(screen.getByTestId("pool-table"))
      .getAllByRole("columnheader")
      .map((node) => node.textContent ?? "");
    expect(headers).toEqual(["策略", "高波动", "下行趋势", "上行趋势", "箱体", "操作"]);

    // 5 个策略行各一个（`rowsOf` 按策略归并）
    expect(screen.getByTestId("fake-action-7")).toBeTruthy();
    expect(screen.getByTestId("fake-action-8")).toBeTruthy();
    expect(screen.getByTestId("fake-action-11")).toBeTruthy();
  });

  it("行内展开的 colSpan 跟着多出来的那一列走", () => {
    const open = new Set([fitCell.key]);
    render(
      <PoolTable
        pool={pool}
        initialExpanded={open}
        renderStrategyAction={() => <span />}
      />
    );

    const detail = screen.getByTestId(`pool-cell-detail-${fitCell.key}`);
    const cellNode = detail.querySelector("td");
    expect(cellNode?.getAttribute("colspan")).toBe("6"); // 1 策略 + 4 阶段 + 1 操作
  });
});
