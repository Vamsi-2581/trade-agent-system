/**
 * 交易页「实盘 / 模拟」分档的契约。
 *
 * 症状（2026-10-24 报告）：交易页上出现一张标着「实盘」的 binance 卡片，总资产 $0、
 * 持仓空，而账户里真实的实盘资产从不出现 —— 「binance 实盘没有同步现有资产」。
 *
 * 根因：前端读 `account.testnet_status`，而后端 `ExchangeAccountSerializer` 只发
 * `testnet`。`testnet_status` 这个字段在**整个 backend 里从不存在**（唯一出处是
 * `lib/api.ts` 的类型声明，由 5c7fdf5 把 `testnet` 改名而来）。它对每个账户都是
 * `undefined`，于是：
 *   ① 卡片徽标恒为「实盘」（`testnet_status ? "模拟" : "实盘"`）；
 *   ② `liveAccounts = accounts.filter(a => !a.testnet_status)` 收下**全部**账户，
 *      `paperAccounts` 恒为空 —— 实盘页签列出模拟账户，模拟页签永远「暂无模拟账户」。
 *
 * 「实盘」两字是用户判断「这张卡该不该有资产」的唯一依据。它错了，用户看到的就是
 * 「我的实盘资产没被同步」——实际上他在看的那个账户根本不是实盘账户。
 *
 * 夹具刻意用**后端的真实契约**（`testnet`）而不是被前端错读的 `testnet_status`：
 * 若夹具也写 `testnet_status`，这两条用例会随错误实现一起变绿。
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  accounts: [] as Array<Record<string, unknown>>,
  positions: { positions: [] as unknown[], executor_running: false },
  frame: {
    trading: "stopped",
    assist: "stopped",
    risk_guard: "stopped",
    order_executor: "stopped",
    data_feed: "stopped",
  },
}));

vi.mock("@/components/layout/DashboardShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@/hooks/useAccounts", () => ({
  useAccounts: () => ({ accounts: state.accounts, loading: false }),
}));

vi.mock("@/hooks/usePositions", () => ({
  usePositions: () => ({ positions: state.positions, loading: false }),
}));

vi.mock("@/hooks/useOrders", () => ({
  useOrders: () => ({ orders: [], loading: false }),
}));

vi.mock("@/hooks/useFrameControl", () => ({
  useFrameControl: () => ({
    status: state.frame,
    loading: false,
    actionLoading: false,
    start: () => {},
    stop: () => {},
  }),
}));

vi.mock("@/hooks/useLiveSessions", () => ({
  useLiveSessions: () => ({
    sessions: [],
    loading: false,
    actionLoading: false,
    start: () => {},
    pause: () => {},
    resume: () => {},
    stop: () => {},
    remove: () => {},
    // promote 不再是 `doAction` 那一族的一个键：它是两拍状态 + 三个回调
    // （见 `hooks/useLiveSessions.ts` 的 `PromotionState`）。这里照实模拟，
    // 免得这份夹具在 hook 契约变化后继续「看起来对」。
    promotion: { preview: null, result: null, error: null, busy: false },
    requestPromotion: () => {},
    confirmPromotion: () => {},
    dismissPromotion: () => {},
  }),
}));

import TradingPage from "../page";

/** `/api/trading/accounts/` 的**真实**响应形状：`ExchangeAccountSerializer` 发 `testnet`。 */
function account(overrides: Record<string, unknown> = {}) {
  return {
    id: "acc-testnet",
    exchange: "binance",
    label: "binance (Testnet)",
    is_active: true,
    testnet: true,
    created_at: "2026-04-17T09:47:54Z",
    balance: null,
    ...overrides,
  };
}

function cardFor(label: string): HTMLElement {
  const node = screen.getByText(label).closest(".bg-bg1");
  if (!node) throw new Error(`没有找到账户 ${label} 的卡片`);
  return node as HTMLElement;
}

describe("交易页实盘/模拟分档", () => {
  beforeEach(() => {
    state.accounts = [];
    state.positions = { positions: [], executor_running: false };
  });

  it("testnet 账户的卡片徽标必须是「模拟」，不能标成「实盘」", () => {
    state.accounts = [account()];

    render(<TradingPage />);

    const card = cardFor("binance (Testnet)");
    // 徽标只说一件事：这个账户是模拟还是实盘。写成「实盘」等于告诉用户
    // 「这张卡上的 $0 是你的实盘资产」——这正是用户报的那句话的来源。
    expect(within(card).getByText("模拟")).toBeInTheDocument();
    expect(within(card).queryByText("实盘")).toBeNull();
  });

  it("testnet 账户不能被「实盘」页签收下", () => {
    state.accounts = [account()];

    render(<TradingPage />);

    fireEvent.click(screen.getByRole("button", { name: "实盘" }));

    // 只有一个模拟账户时，「实盘」页签的正确回答是「暂无实盘账户」。
    expect(screen.getByText("暂无实盘账户")).toBeInTheDocument();
    expect(screen.queryByText("binance (Testnet)")).toBeNull();
  });

  it("框架没起时余额显示「—」并说明原因，不能编成 $0", () => {
    // 账户在库、余额拿不到（`/api/trading/accounts/` 只在 OrderExecutor 跑着时才去
    // 问交易所，否则 `balance` 是 null）。这是「没有数」，不是「数是 0」。
    state.accounts = [
      account({ label: "实盘主账户", testnet: false, balance: null }),
    ];

    render(<TradingPage />);

    const card = cardFor("实盘主账户");
    expect(within(card).getByText("总资产")).toBeInTheDocument();
    expect(within(card).queryAllByText("$0.00")).toHaveLength(0);
    expect(within(card).getAllByText("—").length).toBeGreaterThan(0);
    // 而且必须告诉用户为什么没有这个数、以及该点哪里。
    expect(
      screen.getByText(/交易框架未启动，持仓和余额数据暂不可用/),
    ).toBeInTheDocument();
  });
});