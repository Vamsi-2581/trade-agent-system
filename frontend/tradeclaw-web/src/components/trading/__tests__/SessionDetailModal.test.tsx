/**
 * 会话详情弹窗的契约。
 *
 * 这个弹窗的全部风险都在「数字从哪来」：
 *
 *   1. **统计必须逐字来自接口**，尤其不得由 `current_equity - initial_capital` 现算。
 *      两者按构造相等（同一个写入方），所以一个现算的假数看起来完全正常——本仓最贵的
 *      那类错误。下面的夹具刻意让两者**不等**，于是任何现算都会露馅。
 *   2. **「样本不足」由后端的 `sample_sufficient` 决定**，不是前端拿 n 跟 30 比大小。
 *      有一条用例把「n 很大但仍标样本不足」钉住：门槛只有一处（后端），前端不许有第二处。
 *   3. **不出现收益率**（会话的资金归属无法切分），也**不出现一排 0.00**（还没成交 ≠
 *      一笔没赢过）。
 *   4. 熔断层只在真有层时出现，标题逐字用接口给的 `text`；接口挂了如实报错，不拿列表里
 *      那份旧数据假装是真的。
 */

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveSession, LiveSessionDetail } from "@/lib/api";

const getDetail = vi.hoisted(() => vi.fn());
const getBacktest = vi.hoisted(() => vi.fn());
const getStrategies = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  liveSessionApi: { getDetail },
  backtestApi: { getDetail: getBacktest },
  tradingApi: { getStrategies },
}));

import SessionDetailModal from "../SessionDetailModal";

// ── 夹具 ──

const session: LiveSession = {
  id: "sess-1",
  strategy: "strat-1",
  strategy_name: "rsi_cross",
  backtest_result: "bt-1",
  backtest_result_id: "bt-1",
  symbol: "BTC/USDT",
  mode: "paper",
  status: "running",
  exchange_account: "acc-1",
  exchange_account_name: "binance · 模拟",
  initial_capital: "1000.00000000",
  // 刻意与 realized_pnl 不等：现算出来的 7999 一旦出现就是回归
  current_equity: "8999.00000000",
  realized_pnl: "12.50000000",
  config: {},
  started_at: "2026-09-01T00:00:00Z",
  stopped_at: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const detail: LiveSessionDetail = {
  ...session,
  stats: {
    realized_pnl_total: "12.50000000",
    closed_orders: 3,
    winning_orders: 1,
    win_rate: 0.3333,
    sample_threshold: 30,
    sample_sufficient: false,
    avg_pnl: "4.16666667",
    best_pnl: "20.00000000",
    worst_pnl: "-4.00000000",
    caveats: [
      "不含未实现部分：会话停止时不关仓，敞口留在账户上（见账户持仓）。",
      "不含熔断减仓实现的盈亏：减仓作用于账户持仓，其子单不属于任何会话。",
    ],
  },
  params: {
    resolved: true,
    strategy_name: "rsi_cross",
    rows: [
      { key: "rsi_period", declared: true, default: 14, override: null, effective: 14 },
      {
        key: "position_pct",
        declared: true,
        default: 0.1,
        override: 0.25,
        effective: 0.25,
      },
      {
        key: "stop_loss_pct",
        declared: false,
        default: null,
        override: 0.05,
        effective: 0.05,
      },
    ],
    config: { position_pct: 0.25, stop_loss_pct: 0.05 },
  },
  halt_layers: [],
  orders_total: 2,
  orders: [
    {
      id: "ord-1",
      request_id: "req-1",
      exchange_account: 1,
      symbol: "BTC/USDT",
      side: "sell",
      order_type: "market",
      quantity: "0.01000000",
      price: null,
      status: "filled",
      exchange_order_id: "ex-1",
      filled_quantity: "0.01000000",
      avg_fill_price: "61000.00000000",
      realized_pnl: "12.50000000",
      error_message: "",
      live_session_id: "sess-1",
      strategy_id: "strat-1",
      strategy_name: "rsi_cross",
      triggered_strategy: "rsi_cross",
      created_at: "2026-09-02T03:04:05Z",
      updated_at: "2026-09-02T03:04:05Z",
    },
    {
      id: "ord-2",
      request_id: "req-2",
      exchange_account: 1,
      symbol: "BTC/USDT",
      side: "buy",
      order_type: "market",
      quantity: "0.01000000",
      price: null,
      status: "filled",
      exchange_order_id: "ex-2",
      filled_quantity: "0.01000000",
      avg_fill_price: "60000.00000000",
      realized_pnl: null,
      error_message: "",
      live_session_id: "sess-1",
      strategy_id: "strat-1",
      strategy_name: "rsi_cross",
      triggered_strategy: "rsi_cross",
      created_at: "2026-09-02T02:00:00Z",
      updated_at: "2026-09-02T02:00:00Z",
    },
  ],
};

const backtest = {
  id: "bt-1",
  strategy: 1,
  strategy_name: "rsi_cross",
  symbol: "BTC/USDT",
  timeframe: "4h",
  start_date: "2026-01-01",
  end_date: "2026-02-01",
  initial_capital: "10000.00000000",
  final_capital: "11000.00000000",
  total_return_pct: 10.0,
  sharpe_ratio: 1.2,
  max_drawdown_pct: -5.5,
  win_rate: 0.55,
  total_trades: 40,
  git_commit_hash: "abc123",
  parameters: {},
  review_status: "approved",
  review_notes: "",
  reviewed_at: null,
  created_at: "2026-02-02T00:00:00Z",
};

const strategies = [
  {
    id: "strat-1",
    name: "rsi_cross",
    code_path: "strategies/rsi_cross.py",
    git_commit_hash: "deadbeef",
    is_active: true,
    created_at: "2026-01-01T00:00:00Z",
  },
];

function renderModal(over: Partial<LiveSession> = {}) {
  return render(
    <SessionDetailModal session={{ ...session, ...over }} onClose={vi.fn()} />
  );
}

/** 详情已到手（初次的 promise 已落定） */
async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  getDetail.mockResolvedValue(detail);
  getBacktest.mockResolvedValue(backtest);
  getStrategies.mockResolvedValue(strategies);
});

afterEach(() => {
  vi.useRealTimers();
});

// ── ② 收益统计 ──

describe("收益统计", () => {
  it("数字逐字来自接口", async () => {
    renderModal();

    const stats = within(await screen.findByTestId("session-stats"));
    expect(stats.getByText("+12.50")).toBeTruthy(); // realized_pnl_total
    expect(stats.getByText("3")).toBeTruthy(); // closed_orders
    expect(stats.getByText("1/3")).toBeTruthy(); // 胜率的分母
    expect(stats.getByText("33.3%")).toBeTruthy();
    expect(stats.getByText("+4.17")).toBeTruthy(); // avg_pnl
    expect(stats.getByText("+20.00")).toBeTruthy(); // best
    expect(stats.getByText("-4.00")).toBeTruthy(); // worst
  });

  it("合计不得由 current_equity - initial_capital 现算", async () => {
    renderModal();

    const stats = within(await screen.findByTestId("session-stats"));
    expect(stats.getByText("+12.50")).toBeTruthy();
    // 8999 - 1000 = 7999：现算出来的数一个字形都不许出现
    expect(stats.queryByText(/7,?999/)).toBeNull();
  });

  it("样本不足的判定来自后端的 flag，不是前端拿 n 比 30", async () => {
    // n=3 且 flag=false → 标注出现
    renderModal();
    expect(await screen.findByText("样本不足")).toBeTruthy();

    // n=50 但 flag=false → 仍然出现：门槛只有后端一处，前端不许自己比大小
    getDetail.mockResolvedValue({
      ...detail,
      stats: {
        ...detail.stats,
        closed_orders: 50,
        winning_orders: 30,
        sample_sufficient: false,
      },
    });
    render(<SessionDetailModal session={session} onClose={vi.fn()} />);
    await screen.findByText("1/3");
    expect(screen.getAllByText("样本不足").length).toBe(2);
  });

  it("样本够了就不标注", async () => {
    getDetail.mockResolvedValue({
      ...detail,
      stats: { ...detail.stats, closed_orders: 40, sample_sufficient: true },
    });

    renderModal();

    const stats = within(await screen.findByTestId("session-stats"));
    expect(stats.queryByText("样本不足")).toBeNull();
  });

  it("不出现任何收益率", async () => {
    renderModal();
    await screen.findByTestId("session-stats");

    expect(screen.queryByText(/收益率/)).toBeNull();
    expect(screen.queryByText(/回报率/)).toBeNull();
    // 回测自己的 total_return_pct 也不许混进来当会话收益（它属于回测页）
    expect(screen.queryByText(/\+10\.0/)).toBeNull();
  });

  it("没成交时说「尚无成交」，不是一排 0.00", async () => {
    getDetail.mockResolvedValue({
      ...detail,
      stats: {
        ...detail.stats,
        realized_pnl_total: "0.00000000",
        closed_orders: 0,
        winning_orders: 0,
        win_rate: null,
        sample_sufficient: false,
        avg_pnl: null,
        best_pnl: null,
        worst_pnl: null,
      },
      orders: [],
      orders_total: 0,
    });

    renderModal();

    const stats = within(await screen.findByTestId("session-stats"));
    expect(stats.getByText(/该会话尚无成交/)).toBeTruthy();
    expect(stats.queryByText("0.00")).toBeNull();
    expect(stats.queryByText("胜率")).toBeNull();
    expect(screen.getByText("该会话尚无订单。")).toBeTruthy();
  });

  it("口径边界随数字一起显示（未实现 / 熔断减仓）", async () => {
    renderModal();

    const stats = within(await screen.findByTestId("session-stats"));
    expect(stats.getByText(/不含未实现部分/)).toBeTruthy();
    expect(stats.getByText(/不含熔断减仓实现的盈亏/)).toBeTruthy();
  });
});

// ── ③ 运行参数 ──

describe("运行参数", () => {
  it("三列都摆出来：策略默认 / 本会话覆盖 / 实际生效", async () => {
    renderModal();

    expect(await screen.findByText("rsi_period")).toBeTruthy();
    expect(screen.getByText("position_pct")).toBeTruthy();
    // 未声明的键照列，并标出来（runner 无白名单，它真的会到策略手上）
    expect(screen.getByText("策略未声明")).toBeTruthy();
    expect(screen.getByText("stop_loss_pct")).toBeTruthy();
  });

  it("解析不到策略实现类时如实说，并回 config 原文", async () => {
    getDetail.mockResolvedValue({
      ...detail,
      params: { resolved: false, strategy_name: "s1", rows: [], config: { a: 1 } },
    });

    renderModal();

    expect(await screen.findByText(/解析不到策略实现类/)).toBeTruthy();
    expect(screen.getByText(/\"a\": 1/)).toBeTruthy();
  });
});

// ── ⑥ 熔断层 ──

describe("熔断层", () => {
  it("没有层时整块不出现", async () => {
    renderModal();
    await screen.findByTestId("session-stats");

    expect(screen.queryByText(/正在拦这条会话/)).toBeNull();
  });

  it("有层时标题逐字用接口给的 text", async () => {
    getDetail.mockResolvedValue({
      ...detail,
      halt_layers: [
        {
          text: "全局 · 高波动档（全局）",
          reason: "BTC 30 日波动率分位 0.97",
          opened_at: "2026-09-02T01:00:00Z",
          expires_at: "2026-09-02T04:00:00Z",
        },
      ],
    });

    renderModal();

    expect(await screen.findByText("正在拦这条会话")).toBeTruthy();
    expect(screen.getByText("全局 · 高波动档（全局）")).toBeTruthy();
    expect(screen.getByText(/BTC 30 日波动率分位 0.97/)).toBeTruthy();
  });
});

// ── ④⑤ 来源回测 / 策略本体 ──

describe("来源回测与策略本体", () => {
  it("回测摘要给出品种/周期/区间与跳转", async () => {
    renderModal();

    expect(await screen.findByText(/BTC\/USDT · 4h · 2026-01-01 ~ 2026-02-01/)).toBeTruthy();
    const link = screen.getByText("查看回测 →").closest("a");
    expect(link?.getAttribute("href")).toBe("/backtest/bt-1");
  });

  it("没有来源回测时说清楚，不编一个", async () => {
    renderModal({ backtest_result_id: null, backtest_result: null });

    expect(await screen.findByText(/没有来源回测/)).toBeTruthy();
  });

  it("策略本体显示代码版本与启停状态", async () => {
    renderModal();

    expect(await screen.findByText("deadbeef")).toBeTruthy();
    expect(screen.getByText("已启用")).toBeTruthy();
    expect(screen.getByText("strategies/rsi_cross.py")).toBeTruthy();
  });
});

// ── ⑦ 订单流水 ──

describe("订单流水", () => {
  it("截断时连同总数一起说明", async () => {
    getDetail.mockResolvedValue({ ...detail, orders_total: 51 });

    renderModal();

    expect(await screen.findByText("最近 2 条（共 51 条）")).toBeTruthy();
  });

  it("未截断时只说总数", async () => {
    renderModal();

    expect(await screen.findByText("共 2 条")).toBeTruthy();
  });

  it("逐单的已实现盈亏照接口显示，开仓单是「—」而不是 0", async () => {
    renderModal();

    const orders = within(await screen.findByTestId("session-orders"));
    expect(orders.getByText("+12.50")).toBeTruthy(); // 平仓单
    expect(orders.getAllByText("—").length).toBeGreaterThan(0); // 开仓单（realized_pnl 为 null）
  });
});

// ── 失败与交互 ──

describe("取数失败与关闭", () => {
  it("接口挂掉时如实报错，不显示统计数字", async () => {
    getDetail.mockRejectedValue(new Error("API error: 500"));

    renderModal();

    expect(await screen.findByText("API error: 500")).toBeTruthy();
    expect(screen.getByText(/详情取数失败/)).toBeTruthy();
    expect(screen.queryByTestId("session-stats")).toBeNull();
    // 列表里那份会话信息还在（它本来就有，不依赖详情接口）
    const header = within(screen.getByTestId("session-detail-header"));
    expect(header.getByText("BTC/USDT")).toBeTruthy();
    expect(screen.getByLabelText("会话详情")).toBeTruthy();
    // ①的「已实现盈亏」只认详情接口：详情没到手就是「—」，
    // 不许拿列表里的 `realized_pnl`（+12.50）顶上假装详情取到了
    const overview = within(screen.getByTestId("session-overview"));
    expect(overview.getAllByText("—").length).toBeGreaterThan(0);
    expect(overview.queryByText("+12.50")).toBeNull();
  });

  it("轮询中途失败：已取到的数留着，但要说它可能已过期", async () => {
    vi.useFakeTimers();
    getDetail.mockResolvedValueOnce(detail); // 打开时那次成功
    getDetail.mockRejectedValue(new Error("API error: 500")); // 之后每一轮都挂

    render(<SessionDetailModal session={session} onClose={vi.fn()} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25000);
    });

    // 一次网络抖动不该把真数擦掉
    const stats = within(screen.getByTestId("session-stats"));
    expect(stats.getByText("+12.50")).toBeTruthy();
    // 但也绝不能假装这就是刚刚取到的
    expect(screen.getByText(/是最后一次成功取到的那份，可能已过期/)).toBeTruthy();
  });

  it("Esc 关闭", async () => {
    const onClose = vi.fn();
    render(<SessionDetailModal session={session} onClose={onClose} />);
    await settle();

    fireEvent.keyDown(window, { key: "Escape" });

    expect(onClose).toHaveBeenCalled();
  });

  it("打开时取一次；running 跟随 10s 轮询，stopped 不轮询", async () => {
    vi.useFakeTimers();
    getDetail.mockResolvedValue(detail);

    const running = render(<SessionDetailModal session={session} onClose={vi.fn()} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25000);
    });
    expect(getDetail.mock.calls.length).toBeGreaterThanOrEqual(3);
    running.unmount();

    getDetail.mockClear();
    render(
      <SessionDetailModal session={{ ...session, status: "stopped" }} onClose={vi.fn()} />
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25000);
    });
    expect(getDetail).toHaveBeenCalledTimes(1);
  });
});
