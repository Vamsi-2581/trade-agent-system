/**
 * 交易界面「升级为实盘」的契约（回归测试）。
 *
 * 症状（2026-10-26 报告）：「在交易界面点升级为实盘没有反应」——按钮点下去，
 * 屏幕上什么都不变，列表也没变。
 *
 * 根因：前端把 promote 当成和 start/pause 一样的**一次 POST**（`doAction` 那一族），
 * 发出去的是无 body 的请求、也没有任何回显。而后端 `live_session_promote` 的契约是
 * **默认干跑**：不带 `confirmed` 只回一段摘要（`dry_run: true`）、什么都不改；
 * 目标实盘账户还必须在 body 里显式给出。于是后端每次都回 200 + 摘要，`request()`
 * 顺利返回，`doAction` 把摘要丢掉再重拉列表 —— 状态没变，界面自然「没反应」。
 *
 * 更糟的是 `sessionActionLoading` 是 hook 级的一个布尔，点一下**整页**所有按钮都
 * 灰 40ms 再恢复，用户连「点了但失败了」都看不出来。
 *
 * 这三条用例只替换网络边界（`global.fetch`），其余全是真的：真 `useLiveSessions`、
 * 真 `liveSessionApi.promote`、真 `SessionMiniCard` 与真确认层。后端按**真实契约**应答：
 *   - 干跑：200 + `{dry_run: true, message, ...}`（**没有 `error` 键**）
 *   - 带 `confirmed=true`：200 + 新会话回执
 *
 * 反向验证过：把 `onPromote` 换回「直接 POST 一次」，三条全红。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/layout/DashboardShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/hooks/useAccounts", () => ({
  useAccounts: () => ({ accounts: [liveAccount], loading: false }),
}));
vi.mock("@/hooks/usePositions", () => ({
  usePositions: () => ({ positions: { positions: [] }, loading: false }),
}));
vi.mock("@/hooks/useOrders", () => ({
  useOrders: () => ({ orders: [], loading: false }),
}));
vi.mock("@/hooks/useFrameControl", () => ({
  useFrameControl: () => ({
    status: {
      trading: "stopped",
      assist: "stopped",
      risk_guard: "stopped",
      order_executor: "stopped",
      data_feed: "stopped",
    },
    loading: false,
    actionLoading: false,
    start: () => {},
    stop: () => {},
  }),
}));

import TradingPage from "../page";

const paperSession = {
  id: "sess-paper-1",
  strategy: "strat-1",
  strategy_name: "Donchian",
  backtest_result: "bt-1",
  backtest_result_id: "bt-1",
  symbol: "DOGE/USDT",
  mode: "paper",
  status: "running",
  exchange_account: "acc-testnet",
  exchange_account_name: "binance (Testnet)",
  initial_capital: "10000",
  current_equity: "10000",
  realized_pnl: "0",
  config: {},
  started_at: null,
  stopped_at: null,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};

/** 后端 `live_session_promote` 干跑的真实响应（无 error 键）。 */
const dryRunBody = {
  action: "promote",
  dry_run: true,
  candidate: { id: "cand-1", name: "Donchian", status: "awaiting_live_approval" },
  paper_session: { id: "sess-paper-1", symbol: "DOGE/USDT", status: "running" },
  target_account: { id: "acc-live", label: "实盘主账户", testnet: false },
  will_inherit: { initial_capital: "10000", config: {} },
  message: "这是干跑：还没有做任何改动。",
};

/** 带 `confirmed=true` 之后的真实响应。 */
const realBody = {
  id: "sess-live-2",
  mode: "live",
  status: "pending",
  message: "已创建实盘会话，原 paper 会话已停止。",
};

/** 目标实盘账户（`testnet: false`）——没有它，promote 后端必然 400。 */
const liveAccount = {
  id: "acc-live",
  exchange: "binance",
  label: "实盘主账户",
  is_active: true,
  testnet: false,
  created_at: "2026-04-17T09:47:54Z",
  balance: null,
};

const calls: Array<{ url: string; method: string; body: string | undefined }> = [];

function json(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      const body = typeof init?.body === "string" ? init.body : undefined;
      calls.push({ url: String(url), method, body });
      if (String(url).includes("/promote")) {
        // 带 confirmed 才是真升；不带是干跑。与后端逐字一致。
        return json(200, body?.includes('"confirmed":true') ? realBody : dryRunBody);
      }
      if (String(url).includes("/api/trading/sessions")) {
        return json(200, [paperSession]);
      }
      if (String(url).includes("/api/trading/accounts")) {
        return json(200, [liveAccount]);
      }
      return json(200, []);
    })
  );
});

describe("交易界面「升级为实盘」", () => {
  it("点下去以后，用户必须看到后端那句「将执行什么」", async () => {
    render(<TradingPage />);

    const button = await screen.findByRole("button", { name: "升级为实盘" });
    fireEvent.click(button);

    // 第一步：选中目标实盘账户（后端硬要求，不选必然 400）。
    fireEvent.change(await screen.findByTestId("promote-target-account"), {
      target: { value: "acc-live" },
    });
    fireEvent.click(screen.getByTestId("promote-request"));

    // 用户看到的那句话必须是后端那句原文（干跑摘要）。这里刻意用整句而不是
    // 宽松正则：`/实盘账户/` 会被页面上无关的「暂无实盘账户」蒙对，循环就成了假绿。
    const shown = await screen.findByText(/这是干跑：还没有做任何改动/);
    expect(shown).toBeInTheDocument();
  });

  it("promote 请求必须带上后端要求的目标实盘账户（否则必然 400）", async () => {
    render(<TradingPage />);

    fireEvent.click(await screen.findByRole("button", { name: "升级为实盘" }));
    fireEvent.change(await screen.findByTestId("promote-target-account"), {
      target: { value: "acc-live" },
    });
    fireEvent.click(screen.getByTestId("promote-request"));

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes("/promote"))).toBe(true);
    });
    const promoteCall = calls.find((c) => c.url.includes("/promote"))!;
    expect(promoteCall.body).toContain("exchange_account_id");
  });

  it("确认之后才真的升级，并把新会话回执显示出来", async () => {
    render(<TradingPage />);

    fireEvent.click(await screen.findByRole("button", { name: "升级为实盘" }));
    fireEvent.change(await screen.findByTestId("promote-target-account"), {
      target: { value: "acc-live" },
    });
    fireEvent.click(screen.getByTestId("promote-request"));
    fireEvent.click(await screen.findByTestId("promote-confirm-ok"));

    expect(await screen.findByText(/已创建实盘会话/)).toBeInTheDocument();
    const confirmedCall = calls.filter((c) => c.url.includes("/promote")).at(-1)!;
    expect(confirmedCall.body).toContain('"confirmed":true');
  });
});
