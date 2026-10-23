/**
 * 「删除策略 / 恢复启用」按钮与确认弹窗的契约。
 *
 * 这一块最容易犯的错是**把「谁说这句话」与「谁判哪个方向」搞混**，所以每条用例盯的都是
 * 一个来源问题：
 *
 *   1. **写权限是界面纪律**：`isStaff=false` 时整块不渲染（ADR 0005）。服务端同样会挡，
 *      前端这一层只是别让人按一个按下去才 403 的按钮。
 *   2. **二次确认之前的点击不写库**：点按钮先拉确认页（重请求），确认之后才 POST。
 *      「确认页」是后端渲染的整页文本，不是前端拼的一段话——所以正文与 summary 都
 *      逐字取后端字段。
 *   3. **方向取自后端的 `lifecycle.value`**：`retired` ⇒ 恢复启用，其余 ⇒ 删除策略。
 *      认不出的脏值也走「删除策略」（方向 fail-closed）。
 *   4. **「本来就是这一档」不是错误**：`changed:false` 走 200，回执里那句就是「没有写
 *      第二条流水」。不染红、不回调 `onChanged`。
 *   5. **失败显示后端原文**（`ApiError.message`），不换成「操作失败」。
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type {
  Labelled,
  RegimeStrategyLifecyclePage,
  RegimeStrategyRef,
} from "@/lib/api";

const getStrategyLifecycle = vi.hoisted(() => vi.fn());
const retireStrategy = vi.hoisted(() => vi.fn());
const restoreStrategy = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    regimeApi: { getStrategyLifecycle, retireStrategy, restoreStrategy },
  };
});

import StrategyLifecycleButton from "../StrategyLifecycleButton";

// ── 夹具 ──

const labelled = (value: string, display: string): Labelled => ({ value, display });

/** 后端 `strategy_lifecycle.LIFECYCLE_DISPLAY` 的原样文本（前端**不许**另写一份）。 */
const ACTIVE = labelled("active", "在册（机制会管它）");
const RETIRED = labelled("retired", "已退役（机制不再管它）");

const ACTIVE_STRATEGY: RegimeStrategyRef = {
  id: "7",
  name: "rsi_cross",
  lifecycle: ACTIVE,
  latest_change: null,
};

const RETIRED_STRATEGY: RegimeStrategyRef = {
  id: "7",
  name: "rsi_cross",
  lifecycle: RETIRED,
  latest_change: {
    at: "2026-10-23T08:00:00Z",
    actor_kind: labelled("web", "网页"),
    actor_name: "ops",
    from_active: ACTIVE,
    to_active: RETIRED,
    reason: "人工退役（从机制里删除）：rsi_cross",
  },
};

/** 后端渲染的确认页正文（前端原样显示，一个字不改）。 */
const RETIRE_BODY =
  "退役「rsi_cross」：把它移出机制的管辖范围（is_active: True → False）。\n" +
  "这一步不做的事：\n· 不删除任何数据：回测、切片、池化格子…… 全部保留。";

const RESTORE_BODY =
  "恢复启用「rsi_cross」：把它放回机制管辖（is_active: False → True）。\n" +
  "· 机制重新开始管它：下一轮 gate 对账会按当前证据重新判它。";

function pageFixture(over: Partial<RegimeStrategyLifecyclePage> = {}) {
  return {
    strategy: {
      id: "7",
      name: "rsi_cross",
      lifecycle: ACTIVE,
      latest_change: null,
    },
    retire: {
      action: { value: "retire" as const, display: "退役（从机制里删除这条策略）" },
      available: true,
      body: RETIRE_BODY,
      summary: "人工退役（从机制里删除）：rsi_cross；此刻活跃会话 1 个",
      counts: { active_sessions: 1, declarations: 1, decisions: 2, pool_cells: 4 },
    },
    restore: {
      action: { value: "restore" as const, display: "恢复启用（放回机制管辖）" },
      available: false,
      body: "「rsi_cross」本来就是启用状态，没有写第二条流水。",
      summary: "人工恢复启用：rsi_cross",
      counts: { active_sessions: 1, declarations: 1, decisions: 2, pool_cells: 4 },
    },
    ...over,
  };
}

/** 已退役策略的确认页：退役那一侧已不可用，恢复那一侧可用。 */
function retiredPage() {
  const base = pageFixture();
  return {
    ...base,
    strategy: {
      id: "7",
      name: "rsi_cross",
      lifecycle: RETIRED,
      latest_change: RETIRED_STRATEGY.latest_change,
    },
    retire: {
      ...base.retire,
      available: false,
      body: "「rsi_cross」本来就是退役状态，没有写第二条流水。",
    },
    restore: { ...base.restore, available: true, body: RESTORE_BODY },
  };
}

const RETIRE_RESULT = {
  strategy: { id: "7", name: "rsi_cross", lifecycle: RETIRED },
  action: { value: "retire" as const, display: "退役（从机制里删除这条策略）" },
  changed: true,
  display: "「rsi_cross」已退役：机制不再管它，历史记录一条都没删。",
  reason: "人工退役（从机制里删除）：rsi_cross；此刻活跃会话 1 个",
  at: "2026-10-23T08:00:00Z",
};

beforeEach(() => {
  getStrategyLifecycle.mockReset();
  retireStrategy.mockReset();
  restoreStrategy.mockReset();
  getStrategyLifecycle.mockResolvedValue(pageFixture());
  retireStrategy.mockResolvedValue(RETIRE_RESULT);
  restoreStrategy.mockResolvedValue({
    ...RETIRE_RESULT,
    strategy: { id: "7", name: "rsi_cross", lifecycle: ACTIVE },
    action: { value: "restore", display: "恢复启用（放回机制管辖）" },
    display: "「rsi_cross」已恢复启用：机制重新开始管它（不产生任何豁免）。",
  });
});

// ── 权限 ──

describe("写权限", () => {
  it("非 staff 时整块不渲染（服务端会挡，前端不显示一个按下去才 403 的按钮）", () => {
    const { container } = render(
      <StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff={false} />
    );

    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("strategy-action-7")).toBeNull();
  });

  it("staff 时按钮在", () => {
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);

    expect(screen.getByTestId("strategy-action-7")).toBeTruthy();
  });
});

// ── 方向 ──

describe("方向", () => {
  it("在册的策略给的是「删除策略」", () => {
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);

    const button = screen.getByTestId("strategy-action-7");
    expect(button.textContent).toBe("删除策略");
    expect(button.getAttribute("data-side")).toBe("retire");
  });

  it("已退役的策略给的是「恢复启用」", () => {
    render(<StrategyLifecycleButton strategy={RETIRED_STRATEGY} isStaff />);

    const button = screen.getByTestId("strategy-action-7");
    expect(button.textContent).toBe("恢复启用");
    expect(button.getAttribute("data-side")).toBe("restore");
  });

  it("认不出的 lifecycle 值落「删除策略」（方向 fail-closed，不落成恢复）", () => {
    render(
      <StrategyLifecycleButton
        strategy={{ ...ACTIVE_STRATEGY, lifecycle: labelled("weird", "脏值") }}
        isStaff
      />
    );

    expect(screen.getByTestId("strategy-action-7").getAttribute("data-side")).toBe(
      "retire"
    );
  });
});

// ── 二次确认 ──

describe("二次确认", () => {
  it("点按钮先拉确认页，不写库", async () => {
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);

    fireEvent.click(screen.getByTestId("strategy-action-7"));

    await waitFor(() => expect(getStrategyLifecycle).toHaveBeenCalledWith("7"));
    // 没确认之前一次写都不发——这是这条用例的核心
    expect(retireStrategy).not.toHaveBeenCalled();
    expect(restoreStrategy).not.toHaveBeenCalled();
  });

  it("弹窗显示的是后端那一页正文与 summary，逐字不改", async () => {
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);
    fireEvent.click(screen.getByTestId("strategy-action-7"));

    const dialog = await screen.findByTestId("strategy-confirm");
    expect(within(dialog).getByTestId("strategy-confirm-body").textContent).toBe(
      RETIRE_BODY
    );
    expect(within(dialog).getByTestId("strategy-confirm-summary").textContent).toBe(
      pageFixture().retire.summary
    );
    // 标题里的方向也取后端那个 display
    expect(dialog.textContent).toContain(pageFixture().retire.action.display);
  });

  it("取消之后不写库，也不回调 onChanged", async () => {
    const onChanged = vi.fn();
    render(
      <StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff onChanged={onChanged} />
    );
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");

    fireEvent.click(screen.getByTestId("strategy-confirm-cancel"));

    expect(screen.queryByTestId("strategy-confirm")).toBeNull();
    expect(retireStrategy).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("确认之后才写入，并按方向调对端点", async () => {
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");

    fireEvent.click(screen.getByTestId("strategy-confirm-ok"));

    await waitFor(() => expect(retireStrategy).toHaveBeenCalledWith("7"));
    expect(restoreStrategy).not.toHaveBeenCalled();
  });

  it("已退役时确认调的是恢复端点", async () => {
    getStrategyLifecycle.mockResolvedValue(retiredPage());
    render(<StrategyLifecycleButton strategy={RETIRED_STRATEGY} isStaff />);
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");

    fireEvent.click(screen.getByTestId("strategy-confirm-ok"));

    await waitFor(() => expect(restoreStrategy).toHaveBeenCalledWith("7"));
    expect(retireStrategy).not.toHaveBeenCalled();
  });
});

// ── 收场 ──

describe("收场", () => {
  it("成功之后显示后端回执与落库理由，并回调 onChanged", async () => {
    const onChanged = vi.fn();
    render(
      <StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff onChanged={onChanged} />
    );
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");
    fireEvent.click(screen.getByTestId("strategy-confirm-ok"));

    const result = await screen.findByTestId("strategy-result");
    expect(result.getAttribute("data-changed")).toBe("true");
    expect(within(result).getByTestId("strategy-result-display").textContent).toBe(
      RETIRE_RESULT.display
    );
    expect(within(result).getByTestId("strategy-result-reason").textContent).toBe(
      RETIRE_RESULT.reason
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  });

  it("「本来就是这一档」是 200 + changed:false：走同一条路径、不染红、不回调", async () => {
    const onChanged = vi.fn();
    retireStrategy.mockResolvedValue({
      ...RETIRE_RESULT,
      changed: false,
      at: null,
      display: "「rsi_cross」本来就是退役状态，没有写第二条流水。",
    });
    render(
      <StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff onChanged={onChanged} />
    );
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");
    fireEvent.click(screen.getByTestId("strategy-confirm-ok"));

    const result = await screen.findByTestId("strategy-result");
    expect(result.getAttribute("data-changed")).toBe("false");
    expect(within(result).getByTestId("strategy-result-display").textContent).toContain(
      "本来就是"
    );
    // 没有变化就不刷新池化表（也免得人以为动作发生过）
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("失败时原样显示后端那句话，且留在弹窗里让人重试", async () => {
    retireStrategy.mockRejectedValue(new ApiError("此操作需要管理员权限。", 403));
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");
    fireEvent.click(screen.getByTestId("strategy-confirm-ok"));

    const error = await screen.findByTestId("strategy-confirm-error");
    expect(error.textContent).toContain("此操作需要管理员权限。");
    // 弹窗仍然开着（人可以直接再按一次，而不是从头点起）
    expect(screen.getByTestId("strategy-confirm")).toBeTruthy();
    expect(screen.getByTestId("strategy-confirm-ok")).toBeTruthy();
  });

  it("拉确认页失败时在行内报错，不开一个空弹窗", async () => {
    getStrategyLifecycle.mockRejectedValue(new ApiError("没有 id=7 的这一条策略", 404));
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);

    fireEvent.click(screen.getByTestId("strategy-action-7"));

    const error = await screen.findByTestId("strategy-error-7");
    expect(error.textContent).toContain("没有 id=7 的这一条策略");
    expect(screen.queryByTestId("strategy-confirm")).toBeNull();
  });

  it("本来就是这一档的那一侧，确认按钮禁用（不是让人按下去等一个 no-op）", async () => {
    // 恢复那一侧此刻 unavailable（策略在册），弹窗里的确认按钮必须按不动
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    await screen.findByTestId("strategy-confirm");

    // 这一侧本来就是可用的（在册 → 删除可做），所以这里断言的是可用的那一侧**可点**；
    // 不可用的那一侧由后端 `available` 决定按钮态（见下一条）。
    expect(screen.getByTestId("strategy-confirm-ok")).not.toBeDisabled();
  });

  it("后端说这一侧不可用时，确认按钮禁用且不显示「将落库的理由」", async () => {
    // 构造一个「按钮点的是退役、但后端说退役不可用」的形状（脏状态：前端从 lifecycle
    // 推出来的方向与后端那一页不一致），断言前端以后端为准。
    getStrategyLifecycle.mockResolvedValue(
      pageFixture({
        retire: {
          ...pageFixture().retire,
          available: false,
          body: "「rsi_cross」本来就是退役状态，没有写第二条流水。",
        },
      })
    );
    render(<StrategyLifecycleButton strategy={ACTIVE_STRATEGY} isStaff />);
    fireEvent.click(screen.getByTestId("strategy-action-7"));
    const dialog = await screen.findByTestId("strategy-confirm");

    expect(within(dialog).getByTestId("strategy-confirm-ok")).toBeDisabled();
    expect(within(dialog).queryByTestId("strategy-confirm-summary")).toBeNull();
    expect(within(dialog).getByTestId("strategy-confirm-body").textContent).toContain(
      "本来就是"
    );
  });
});