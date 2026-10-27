/**
 * 交易页停止横幅的契约（ADR 0001 的 halt 读面）。
 *
 * 这一块的全部风险是**把一句话说得比它本身更确定**，所以每条用例盯的都是同一个问题的
 * 一个面：
 *
 *   1. **常显，且三种情形各说各的话**。「无层」= 放行，是用户要看见的答案；但「还没读到」
 *      与「取数失败」**不许**冒充它——拿不知道当放行，方向恰好是最危险的那一侧。有一条用例
 *      把这两支分别钉住。
 *   2. **halt 是集合**。造三层，断言**三层都在文档里**、条数等于数据长度。这是防回归的
 *      关键用例：本仓写过「同时生效时只报一层，会让人按报出来的那层去排查，而解除它之后单
 *      还是下不出去」——所以任何「取第一条」「只显示最严重的一层」的写法都必须在这里变红。
 *   3. **`text` 逐字来自后端**。夹具里的 `text` 刻意写成带 `trigger=` / `作用域` 的原文，
 *      并断言节点文本**恰好等于**它：前端一旦按 label/trigger/scope 重拼一遍，或者给
 *      trigger/scope 加中文映射，这里就会对不上。**不断言前端拼出来的字符串。**
 *   4. **阶段只渲染 `.display`**：`value`（`high_vol`）不许出现在文档里；`regimeState` 为
 *      `null` 时那一段整段不渲染，也不补一句「未知阶段」。
 *   5. **状态翻转有可测标记**：`data-halt-transition` / `halt-flash-*`，不依赖真实计时器。
 *      另外根节点常驻 `data-halt-state`（`halted` / `clear` / `pending` / `unknown`）与
 *      `data-halt-layers`（集合规模），页面集成时按它们对齐，不必解析中文文案。
 *
 * 夹具里的时刻一律用固定 ISO 串，时间文本走 `formatMoment`（与组件同一个口径），所以断言
 * 不受运行环境时区影响。
 */

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HaltLayerPayload, Labelled } from "@/lib/api";
import { formatMoment } from "@/lib/format";

const getHaltState = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  regimeApi: { getHaltState },
}));

import HaltBanner from "../HaltBanner";
import { HALT_STATE_POLL_MS, useHaltState } from "@/hooks/useHaltState";

// ── 夹具 ──

/** 三层同时生效。`text` 是后端 `halt.HaltLayer.text` 的形状（带 trigger 与作用域原文）。 */
const LAYER_A: HaltLayerPayload = {
  text: "全局 · 高波动档（trigger=vol_quantile_097，作用域 scope=global_all）",
  reason: "BTC 30 日波动率分位 0.97 ≥ 门槛 0.90",
  opened_at: "2026-09-02T01:00:00Z",
  expires_at: "2026-09-02T04:00:00Z",
};

const LAYER_B: HaltLayerPayload = {
  text: "账户 cust-7 · 回撤超限（trigger=drawdown，作用域 scope=account）",
  reason: "本账户 24h 回撤 8.4% ≥ 门槛 6%",
  opened_at: "2026-09-02T02:30:00Z",
  expires_at: null,
};

const LAYER_C: HaltLayerPayload = {
  text: "策略 rsi_cross · 池化停用（trigger=pool_state，作用域 scope=strategy）",
  reason: "池化表判该策略在 high_vol 阶段停用",
  opened_at: "2026-09-02T03:05:00Z",
  expires_at: "2026-09-03T00:00:00Z",
};

const THREE_LAYERS = [LAYER_A, LAYER_B, LAYER_C];

const REGIME: Labelled = {
  value: "high_vol",
  display: "高波动档（30 日分位 ≥ 0.90）",
};

const banner = () => screen.getByTestId("halt-banner");

type BannerProps = {
  regimeState: Labelled | null;
  haltLayers: HaltLayerPayload[];
  asOf: string | null;
  loading: boolean;
  error: string | null;
};

/** 默认是「加载完成、无层」（放行）那一帧；各用例只覆盖自己关心的那几个 prop。 */
const BASE: BannerProps = {
  regimeState: REGIME,
  haltLayers: [],
  asOf: null,
  loading: false,
  error: null,
};

function renderBanner(over: Partial<BannerProps> = {}) {
  return render(<HaltBanner {...BASE} {...over} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  getHaltState.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

// ── 常显与三种情形 ──

describe("常显与三种情形", () => {
  it("无层时渲染「当前无停止判定命中」，且一个层都不渲染", () => {
    renderBanner();

    // 常显：横幅本身在，结论也在
    expect(banner()).toBeTruthy();
    expect(screen.getByText("当前无停止判定命中")).toBeTruthy();
    // 一个层都没有
    expect(screen.queryByTestId("halt-layers")).toBeNull();
    expect(screen.queryAllByTestId("halt-layer").length).toBe(0);
    expect(banner().getAttribute("data-halt-layers")).toBe("0");
    expect(banner().getAttribute("data-halt-state")).toBe("clear");
  });

  it("加载中：给占位，**不**写「当前无停止判定命中」", () => {
    renderBanner({ loading: true });

    expect(screen.getByTestId("halt-pending")).toBeTruthy();
    expect(banner().getAttribute("data-halt-state")).toBe("pending");
    // 「还没读到」不许冒充放行
    expect(screen.queryByText("当前无停止判定命中")).toBeNull();
  });

  it("取数失败且手上没有数据：如实报错，**不**写「当前无停止判定命中」", () => {
    renderBanner({ error: "API error: 500" });

    expect(screen.getByText("API error: 500")).toBeTruthy();
    expect(screen.getByTestId("halt-unknown")).toBeTruthy();
    expect(banner().getAttribute("data-halt-state")).toBe("unknown");
    expect(screen.getByText(/停止状态还没成功读到过/)).toBeTruthy();
    // 拿失败当放行是最危险的那一侧
    expect(screen.queryByText("当前无停止判定命中")).toBeNull();
  });

  it("加载完无层之后，加载占位与「未知」都不再出现", () => {
    renderBanner({ loading: false, error: null });

    expect(screen.queryByTestId("halt-pending")).toBeNull();
    expect(screen.queryByTestId("halt-unknown")).toBeNull();
    // 唯一一种「放行」：读到过、且没有层
    expect(banner().getAttribute("data-halt-state")).toBe("clear");
    expect(screen.getByText("当前无停止判定命中")).toBeTruthy();
  });
});

// ── 多层全部列出（防回归关键用例） ──

describe("多层全部列出", () => {
  it("三层同时生效时，三层的 text 都在文档里，一条都不许省", () => {
    renderBanner({ haltLayers: THREE_LAYERS, asOf: LAYER_C.opened_at });

    const list = within(screen.getByTestId("halt-layers"));
    const items = list.getAllByTestId("halt-layer");

    // 条数 = 数据长度（任何「只报第一层」「只报最严重的一层」的写法都会在这里变红）
    expect(items.length).toBe(THREE_LAYERS.length);
    expect(banner().getAttribute("data-halt-layers")).toBe("3");
    expect(banner().getAttribute("data-halt-state")).toBe("halted");

    for (const layer of THREE_LAYERS) {
      expect(list.getByText(layer.text)).toBeTruthy();
    }
  });

  it("每层的 text 是被后端原样透出来的，不是前端按 trigger/作用域重拼的", () => {
    renderBanner({ haltLayers: THREE_LAYERS, asOf: LAYER_C.opened_at });

    const nodes = screen.getAllByTestId("halt-layer-text");
    expect(nodes.length).toBe(3);

    // **恰好等于**夹具里那一串（含 `trigger=...，作用域 scope=...` 原文）。
    // 前端一旦重拼这句话——哪怕只是把 `scope=global_all` 换成中文——这里就对不上。
    expect(nodes.map((n) => n.textContent)).toEqual(THREE_LAYERS.map((l) => l.text));
  });

  it("每层带出 reason 与生效时刻；到期与无到期分开说", () => {
    renderBanner({ haltLayers: THREE_LAYERS, asOf: LAYER_C.opened_at });

    const items = screen.getAllByTestId("halt-layer");

    THREE_LAYERS.forEach((layer, index) => {
      const textNode = within(items[index]).getByTestId("halt-layer-text");
      // 整个条目的文本里必须同时有 reason 与生效时刻（格式与组件同一个口径）
      const item = textNode.parentElement as HTMLElement;
      expect(item.textContent).toContain(layer.reason);
      expect(item.textContent).toContain(`起 ${formatMoment(layer.opened_at)}`);
    });

    // LAYER_A 有到期，LAYER_B 没有
    expect(items[0].textContent).toContain(`至 ${formatMoment(LAYER_A.expires_at)}`);
    expect(items[1].textContent).toContain("无到期");
  });

  it("层数变化时条数跟着变（两层就是两条）", () => {
    const view = renderBanner({ haltLayers: [LAYER_A, LAYER_C], asOf: LAYER_C.opened_at });
    expect(screen.getAllByTestId("halt-layer").length).toBe(2);

    view.rerender(
      <HaltBanner
        regimeState={REGIME}
        haltLayers={[LAYER_A, LAYER_B, LAYER_C]}
        asOf={LAYER_C.opened_at}
        loading={false}
        error={null}
      />
    );
    expect(screen.getAllByTestId("halt-layer").length).toBe(3);
  });
});

// ── 当前行情阶段 ──

describe("当前行情阶段", () => {
  it("为 null 时那一段整段不渲染，也不补一句「未知阶段」", () => {
    renderBanner({ regimeState: null });

    expect(screen.queryByTestId("halt-regime-state")).toBeNull();
    expect(screen.queryByText(/当前行情阶段/)).toBeNull();
    expect(screen.queryByText(/未知阶段/)).toBeNull();
    // 但横幅本身照旧常显
    expect(banner()).toBeTruthy();
    expect(screen.getByText("当前无停止判定命中")).toBeTruthy();
  });

  it("非 null 时渲染的是后端给的 display，不是 value 的翻译", () => {
    renderBanner({ regimeState: REGIME });

    const node = screen.getByTestId("halt-regime-state");
    expect(node.textContent).toContain(REGIME.display);
    // `value` 是 slug，不该被前端搬到界面上（也不许被翻译成中文）
    expect(node.textContent).not.toContain(REGIME.value);
  });

  it("有层在拦时，阶段与层同时可见", () => {
    renderBanner({ regimeState: REGIME, haltLayers: [LAYER_A], asOf: LAYER_A.opened_at });

    expect(screen.getByTestId("halt-regime-state").textContent).toContain(REGIME.display);
    expect(screen.getByText(LAYER_A.text)).toBeTruthy();
    expect(screen.queryByText("当前无停止判定命中")).toBeNull();
  });
});

// ── 状态变化的视觉强化 ──

describe("状态变化的视觉强化", () => {
  it("挂载时不算一次变化：首帧不带任何强化标记", () => {
    renderBanner({ haltLayers: THREE_LAYERS, asOf: LAYER_C.opened_at });

    expect(banner().getAttribute("data-halt-transition")).toBeNull();
    expect(banner().getAttribute("class")).not.toContain("halt-flash");
  });

  it("无层 → 有层：出现 engaged 标记", () => {
    const view = renderBanner();
    expect(banner().getAttribute("data-halt-transition")).toBeNull();

    view.rerender(
      <HaltBanner
        regimeState={REGIME}
        haltLayers={[LAYER_A]}
        asOf={LAYER_A.opened_at}
        loading={false}
        error={null}
      />
    );

    expect(banner().getAttribute("data-halt-transition")).toBe("engaged");
    expect(banner().getAttribute("class")).toContain("halt-flash-engaged");
  });

  it("有层 → 无层：出现 cleared 标记", () => {
    const view = renderBanner({ haltLayers: THREE_LAYERS, asOf: LAYER_C.opened_at });

    view.rerender(
      <HaltBanner
        regimeState={REGIME}
        haltLayers={[]}
        asOf={null}
        loading={false}
        error={null}
      />
    );

    expect(banner().getAttribute("data-halt-transition")).toBe("cleared");
    expect(banner().getAttribute("class")).toContain("halt-flash-cleared");
    // 强调只是视觉：结论仍然在那句话里
    expect(screen.getByText("当前无停止判定命中")).toBeTruthy();
  });

  it("强调是短暂的：计时器到点后标记自行褪去，结论不受影响", async () => {
    vi.useFakeTimers();
    const view = renderBanner();

    view.rerender(
      <HaltBanner
        regimeState={REGIME}
        haltLayers={[LAYER_B]}
        asOf={LAYER_B.opened_at}
        loading={false}
        error={null}
      />
    );
    expect(banner().getAttribute("data-halt-transition")).toBe("engaged");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(banner().getAttribute("data-halt-transition")).toBeNull();
    expect(screen.getByText(LAYER_B.text)).toBeTruthy();
  });
});

// ── 取数失败与旧数据 ──

describe("取数失败与旧数据", () => {
  it("失败时旧数据照旧渲染，并说明它可能已过期", () => {
    renderBanner({
      haltLayers: THREE_LAYERS,
      asOf: LAYER_C.opened_at,
      error: "API error: 500",
    });

    // 一次抖动不该把「正在拦」的三层擦掉
    expect(screen.getAllByTestId("halt-layer").length).toBe(3);
    expect(screen.getByText(LAYER_B.text)).toBeTruthy();
    // 但也绝不能假装这就是刚刚取到的
    expect(screen.getByText("API error: 500")).toBeTruthy();
    expect(screen.getByText(/最后一次成功取到的那份，可能已过期/)).toBeTruthy();
  });

  it("有截止时刻就显示（走 formatMoment），没有就不渲染那一段", () => {
    const view = renderBanner({ haltLayers: [LAYER_A], asOf: LAYER_A.opened_at });
    expect(screen.getByTestId("halt-as-of").textContent).toContain(
      formatMoment(LAYER_A.opened_at)
    );

    view.rerender(
      <HaltBanner
        regimeState={REGIME}
        haltLayers={[LAYER_A]}
        asOf={null}
        loading={false}
        error={null}
      />
    );
    expect(screen.queryByTestId("halt-as-of")).toBeNull();
  });
});

// ── useHaltState（同一个读面的取数纪律） ──

/** 把 hook 的四个返回值摊成可断言的东西。 */
function HaltProbe() {
  const { data, loading, error, refetch } = useHaltState();
  return (
    <div>
      <span data-testid="probe-data">
        {data === null ? "null" : String(data.halt_layers.length)}
      </span>
      <span data-testid="probe-loading">{String(loading)}</span>
      <span data-testid="probe-error">{error ?? ""}</span>
      <button data-testid="probe-refetch" onClick={() => void refetch()}>
        refetch
      </button>
    </div>
  );
}

describe("useHaltState", () => {
  it("轮询间隔是 60 秒", () => {
    expect(HALT_STATE_POLL_MS).toBe(60_000);
  });

  it("首帧 `data` 是 null（不是空对象）", async () => {
    // 永不落定的请求：首帧就是「还没读到」
    getHaltState.mockReturnValue(new Promise(() => {}));

    render(<HaltProbe />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByTestId("probe-data").textContent).toBe("null");
    expect(screen.getByTestId("probe-loading").textContent).toBe("true");
  });

  it("轮询失败不清空 data：旧层留着，错误走 error", async () => {
    vi.useFakeTimers();
    getHaltState.mockResolvedValueOnce({ halt_layers: [LAYER_A, LAYER_B] });
    getHaltState.mockRejectedValue(new Error("API error: 500"));

    render(<HaltProbe />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByTestId("probe-data").textContent).toBe("2");
    expect(screen.getByTestId("probe-loading").textContent).toBe("false");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HALT_STATE_POLL_MS + 1000);
    });

    // 一次网络抖动不该把真东西擦掉
    expect(screen.getByTestId("probe-data").textContent).toBe("2");
    expect(screen.getByTestId("probe-error").textContent).toBe("API error: 500");
  });

  it("按 60 秒轮询", async () => {
    vi.useFakeTimers();
    getHaltState.mockResolvedValue({ halt_layers: [] });

    render(<HaltProbe />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HALT_STATE_POLL_MS * 2 + 100);
    });

    expect(getHaltState.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("refetch 立刻重拉一次，不必等下一个 60 秒", async () => {
    getHaltState.mockResolvedValue({ halt_layers: [] });

    render(<HaltProbe />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(getHaltState).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByTestId("probe-refetch"));
    });
    expect(getHaltState).toHaveBeenCalledTimes(2);
  });
});
