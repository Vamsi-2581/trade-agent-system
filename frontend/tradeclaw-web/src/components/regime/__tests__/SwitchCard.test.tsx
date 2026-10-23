/**
 * 档位卡片与切换弹窗的契约（P5 #49，map #40 的上半页）。
 *
 * 这一块最容易犯的错是**把「谁说这句话」搞混**，所以每条用例盯的都是一个来源问题：
 *
 *   1. **写权限是界面纪律**：`isStaff=false` 时按钮不渲染、只留一行「需要管理员权限」，
 *      而**读**照旧（卡片上的档位与那句说明仍然显示）。ADR 0005 说得很清楚：服务端会挡，
 *      slash 通路没有收紧，前端这一层只是别让人按一个按下去才 403 的按钮。
 *   2. **二次确认之前的点击不写库**：点按钮先拉确认页（重请求），确认之后才 `switchMode`。
 *      「确认页」是后端渲染的整页文本，不是前端拼的一段话。
 *   3. **目标档位来自后端这一页的 `to_mode`**，不是前端按当前档反推——在「本来就是这一档」
 *      的请求上，反推出来的方向恰好是错的那个。
 *   4. **「本来就是这一档」不是错误**：`changed:false` 走 200，回执里那句就是「没有写第二
 *      条流水」。把它染红会让人再点一次，而再点一次只会再对一次账。
 *   5. **失败显示后端原文**：`ApiError.message`（「切换原因不能为空。」这类话是给人照着
 *      改的），不换成「操作失败」。
 *   6. **「这一档不是执行开关」说三次**：卡片常驻 / 弹窗 / 结果，三次渲染的都是同一个后端
 *      字段 `sw.note`。前端一旦自己补一句解释，这句话就有了两个版本。
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { RegimeBriefing, RegimeSwitch } from "@/lib/api";

const getBriefing = vi.hoisted(() => vi.fn());
const switchMode = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, regimeApi: { getBriefing, switchMode } };
});

import SwitchCard from "../SwitchCard";

// ── 夹具 ──

/** 后端 `mechanism_switch.NOT_EXECUTION_SWITCH` 的原样文本（前端**不许**另写一份）。 */
const NOTE =
  "⚠️ 这一档**不是执行开关**：拦人的是另外两个开关，它们各自开各自的——" +
  "出 Shadow 的效果是**留一条记录**（第①段的判定与切片被验证过了），不是打开了什么。";

const SHADOW_PAGE: RegimeBriefing = {
  kind: { value: "mechanism", display: "出 Shadow（判定 + 切片 + 自动停用）" },
  mode: { value: "shadow", display: "Shadow（只记录，不执行）" },
  to_mode: { value: "executing", display: "执行态" },
  body: "机制整体 · 出 Shadow 体检（这一步本身不改变任何东西）\n当前档位：Shadow（只记录，不执行）",
  summary: "人工出 Shadow（上线确认）：已跑 20 个自然日（门槛 20）",
  details: {},
  heavy: true,
};

const EXECUTING_SWITCH: RegimeSwitch = {
  kind: { value: "mechanism", display: "出 Shadow（判定 + 切片 + 自动停用）" },
  mode: { value: "executing", display: "执行态" },
  latest: {
    at: "2026-10-23T03:00:00Z",
    actor_kind: { value: "web", display: "网页" },
    actor_name: "ops",
    from_mode: { value: "shadow", display: "Shadow（只记录，不执行）" },
    to_mode: { value: "executing", display: "执行态" },
    reason: "人工出 Shadow（上线确认）",
  },
  never_switched: false,
  note: NOTE,
};

const SHADOW_SWITCH: RegimeSwitch = {
  kind: { value: "mechanism", display: "出 Shadow（判定 + 切片 + 自动停用）" },
  mode: { value: "shadow", display: "Shadow（只记录，不执行）" },
  latest: null,
  never_switched: true,
  note: NOTE,
};

function changedResult(over: Record<string, unknown> = {}) {
  return {
    kind: { value: "mechanism", display: "出 Shadow（判定 + 切片 + 自动停用）" },
    changed: true,
    from_mode: { value: "shadow", display: "Shadow（只记录，不执行）" },
    to_mode: { value: "executing", display: "执行态" },
    display: "机制整体已切换：Shadow（只记录，不执行） → 执行态",
    reason: SHADOW_PAGE.summary,
    at: "2026-10-23T04:00:00Z",
    ...over,
  };
}

/** 点开确认页（一次 `getBriefing`）再确认。 */
async function openConfirmation() {
  fireEvent.click(screen.getByTestId("switch-action"));
  await waitFor(() => expect(screen.getByTestId("switch-confirm")).toBeTruthy());
}

async function openAndConfirm() {
  await openConfirmation();
  fireEvent.click(screen.getByTestId("switch-confirm-ok"));
  await waitFor(() => expect(screen.getByTestId("switch-result")).toBeTruthy());
}

beforeEach(() => {
  vi.clearAllMocks();
  getBriefing.mockResolvedValue(SHADOW_PAGE);
  switchMode.mockResolvedValue(changedResult());
});

describe("写权限是界面纪律，不是安全边界", () => {
  it("非 staff：不渲染按钮，只显示一行「需要管理员权限」", () => {
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff={false} />);

    expect(screen.queryByTestId("switch-action")).toBeNull();
    expect(screen.getByTestId("switch-staff-hint").textContent).toContain(
      "需要管理员权限"
    );
    // 读照旧：档位与那句说明属于所有登录用户。
    expect(screen.getByTestId("switch-mode").textContent).toBe(SHADOW_SWITCH.mode.display);
    expect(screen.getByTestId("switch-note").textContent).toBe(NOTE);
  });

  it("staff：按钮可见，点了先拉一次确认页，还不写库", async () => {
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);

    await openConfirmation();

    expect(getBriefing).toHaveBeenCalledTimes(1);
    expect(getBriefing).toHaveBeenCalledWith("mechanism");
    expect(switchMode).not.toHaveBeenCalled();
  });

  it("取消就什么都不发生", async () => {
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);
    await openConfirmation();

    fireEvent.click(screen.getByTestId("switch-confirm-cancel"));

    expect(screen.queryByTestId("switch-confirm")).toBeNull();
    expect(switchMode).not.toHaveBeenCalled();
  });
});

describe("确认页与回执全部来自后端字段", () => {
  it("弹窗里显示后端的方向与整页正文，并提前说明理由由系统写入", async () => {
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);
    await openConfirmation();

    const dialog = within(screen.getByTestId("switch-confirm"));
    expect(dialog.getByTestId("switch-confirm-target").textContent).toContain(
      SHADOW_PAGE.to_mode.display
    );
    expect(dialog.getByTestId("switch-confirm-body").textContent).toBe(SHADOW_PAGE.body);
    expect(dialog.getByTestId("switch-confirm-summary").textContent).toBe(
      SHADOW_PAGE.summary
    );
    // 没有输入框：理由不许人写。
    expect(dialog.queryByRole("textbox")).toBeNull();
  });

  it("确认后按后端给的方向调 switchMode，结果回显后端回执", async () => {
    const onSwitched = vi.fn();
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff onSwitched={onSwitched} />);

    await openAndConfirm();

    // 参数来自 `briefing.to_mode`，不是前端按当前档反推出来的。
    expect(switchMode).toHaveBeenCalledWith("mechanism", "executing");
    expect(screen.getByTestId("switch-result-display").textContent).toBe(
      changedResult().display
    );
    expect(screen.getByTestId("switch-result-reason").textContent).toBe(
      SHADOW_PAGE.summary
    );
    expect(screen.getByTestId("switch-result").dataset.changed).toBe("true");
    expect(onSwitched).toHaveBeenCalledTimes(1);
  });

  it("`changed:false` 显示后端那句「本来就是这一档」，且不染成失败色", async () => {
    switchMode.mockResolvedValue(
      changedResult({
        changed: false,
        display: "机制整体本来就是执行态，没有写第二条流水。",
      })
    );
    render(<SwitchCard sw={EXECUTING_SWITCH} isStaff />);

    await openAndConfirm();

    const result = screen.getByTestId("switch-result");
    expect(result.dataset.changed).toBe("false");
    expect(screen.getByTestId("switch-result-display").textContent).toContain(
      "本来就是执行态"
    );
    expect(result.className).not.toContain("text-red");
  });

  it("后端拒绝时原样显示 `ApiError.message`", async () => {
    switchMode.mockRejectedValue(
      new ApiError("切换原因不能为空：RegimeMechanismSwitch 的 reason 是必填的", 400)
    );
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);

    await openConfirmation();
    fireEvent.click(screen.getByTestId("switch-confirm-ok"));

    await waitFor(() =>
      expect(screen.getByTestId("switch-error").textContent).toContain(
        "切换原因不能为空"
      )
    );
    // 失败时留在确认页，人可以直接再点一次（而不是被弹回卡片、要重走一遍）。
    expect(screen.getByTestId("switch-confirm")).toBeTruthy();
    expect(screen.queryByTestId("switch-result")).toBeNull();
  });

  it("确认页本身拉不到时，卡片上显示后端原文", async () => {
    getBriefing.mockRejectedValue(new ApiError("此操作需要管理员权限。", 403));
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);

    fireEvent.click(screen.getByTestId("switch-action"));

    await waitFor(() =>
      expect(screen.getByTestId("switch-error").textContent).toContain(
        "此操作需要管理员权限。"
      )
    );
    expect(screen.queryByTestId("switch-confirm")).toBeNull();
  });
});

describe("「这一档不是执行开关」说三次，且都是同一个后端字段", () => {
  it("卡片常驻 / 弹窗 / 结果各一次", async () => {
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);

    expect(screen.getByTestId("switch-note").textContent).toBe(NOTE);

    await openConfirmation();
    expect(screen.getByTestId("switch-confirm-note").textContent).toBe(NOTE);

    fireEvent.click(screen.getByTestId("switch-confirm-ok"));
    await waitFor(() => expect(screen.getByTestId("switch-result")).toBeTruthy());
    expect(screen.getByTestId("switch-result-note").textContent).toBe(NOTE);
  });

  it("`note` 为空（另外两个开关）时三处都不渲染，不补一句「无说明」", async () => {
    const breaker: RegimeSwitch = {
      kind: { value: "event_breaker", display: "事件熔断" },
      mode: { value: "shadow", display: "Shadow（只记录，不执行）" },
      latest: null,
      never_switched: true,
      note: null,
    };
    getBriefing.mockResolvedValue({
      ...SHADOW_PAGE,
      kind: breaker.kind,
      to_mode: { value: "executing", display: "执行态" },
    });
    render(<SwitchCard sw={breaker} isStaff />);

    expect(screen.queryByTestId("switch-note")).toBeNull();

    await openConfirmation();
    expect(screen.queryByTestId("switch-confirm-note")).toBeNull();

    fireEvent.click(screen.getByTestId("switch-confirm-ok"));
    await waitFor(() => expect(screen.getByTestId("switch-result")).toBeTruthy());
    expect(screen.queryByTestId("switch-result-note")).toBeNull();
  });
});

describe("最近一次切换流水", () => {
  it("从未切换过就说「从未切换过」，不假装有一条空流水", () => {
    render(<SwitchCard sw={SHADOW_SWITCH} isStaff />);

    expect(screen.getByTestId("switch-latest").textContent).toContain("从未切换过");
    expect(screen.queryByTestId("switch-latest-head")).toBeNull();
  });

  it("有流水时显示时刻、操作人与理由（display 全取后端）", () => {
    render(<SwitchCard sw={EXECUTING_SWITCH} isStaff />);

    const latest = screen.getByTestId("switch-latest");
    expect(latest.textContent).toContain("网页·ops");
    expect(latest.textContent).toContain(EXECUTING_SWITCH.latest!.from_mode.display);
    expect(screen.getByTestId("switch-latest-reason").textContent).toBe(
      "人工出 Shadow（上线确认）"
    );
  });
});
