import { describe, expect, it } from "vitest";

import { formatNumber, formatOrderPnl, formatPnl } from "@/lib/format";

/**
 * 两个盈亏口径必须分开，不能合并：
 *   - `formatPnl`：0 是真数（持仓的未实现盈亏、会话合计）→ 显示 `0.00`
 *   - `formatOrderPnl`：订单行 → 0 与 null 都是「—」
 *
 * 分开的原因见 `formatOrderPnl` 的注释：开仓成交的 realized_pnl 在交易所口径里
 * 就是 0，落库以交易所为准（对账要能逐字比对），但显示成 `+0.00` 会让每张进场单
 * 看起来像打平的平仓单。
 */

describe("formatPnl（0 是真数）", () => {
  it("0 显示 0.00，中性色", () => {
    expect(formatPnl("0")).toEqual({ text: "0.00", color: "text-text3" });
    expect(formatPnl("0.00000000")).toEqual({ text: "0.00", color: "text-text3" });
    expect(formatPnl(0)).toEqual({ text: "0.00", color: "text-text3" });
  });

  it("正负号与配色", () => {
    expect(formatPnl("12.5")).toEqual({ text: "+12.50", color: "text-green" });
    expect(formatPnl("-4")).toEqual({ text: "-4.00", color: "text-red" });
  });

  it("千分位", () => {
    expect(formatPnl("1234.5").text).toBe("+1,234.50");
  });
});

describe("formatOrderPnl（0 当空）", () => {
  it("null / 空串 / 非法值都是「—」", () => {
    for (const v of [null, "", "abc"]) {
      expect(formatOrderPnl(v)).toEqual({ text: "—", color: "text-text3" });
    }
  });

  it("0 也是「—」，不是 +0.00", () => {
    expect(formatOrderPnl("0")).toEqual({ text: "—", color: "text-text3" });
    expect(formatOrderPnl("0.00000000")).toEqual({ text: "—", color: "text-text3" });
    expect(formatOrderPnl("0E-8")).toEqual({ text: "—", color: "text-text3" });
    expect(formatOrderPnl(0)).toEqual({ text: "—", color: "text-text3" });
  });

  it("非零照常带符号，与 formatPnl 逐字一致", () => {
    expect(formatOrderPnl("12.50000000")).toEqual(formatPnl("12.5"));
    expect(formatOrderPnl("-3.4959")).toEqual(formatPnl("-3.4959"));
  });
});

describe("formatNumber", () => {
  it("非法值回 0.00", () => {
    expect(formatNumber("nope")).toBe("0.00");
  });
});
