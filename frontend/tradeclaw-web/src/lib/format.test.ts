import { describe, expect, it } from "vitest";

import {
  capitalEfficiencyCards,
  formatEfficiencyMoney,
  formatEfficiencyPct,
  formatNumber,
  formatOrderPnl,
  formatPnl,
} from "@/lib/format";

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

// ── 资金效率的展示降级（spec #30 用户故事 12，子票 #36） ──

/**
 * 这一组的全部风险是**「量不了」被显示成「没占用」**，以及**两种类型互相混淆**。
 *
 * 后端给这一段的是两种数：
 *   - 金额：8 位定点**字符串**（`"10000.00000000"`）——钱从不经过二进制浮点；
 *   - 百分比：**百分点** float（`100.0` 就是 100%）——不是比值，所以**不再乘 100**。
 *
 * 而 `capital_utilization` / `idle_pct` / `time_in_market_pct` /
 * `return_per_capital_used_pct` 在 `invalid_capital` / `empty_window` / `no_trades`
 * 三种情形下是 `null`。**null 绝不能被兜成 0**：`0%` 读起来是「完全没占用资金」，
 * 而真相是「这一段没有资金效率证据」。
 */

describe("formatEfficiencyMoney（8 位定点字符串）", () => {
  it("千分位保留两位，不是原样吐字符串", () => {
    expect(formatEfficiencyMoney("10000.00000000")).toBe("10,000.00");
    expect(formatEfficiencyMoney("0.00000000")).toBe("0.00");
    expect(formatEfficiencyMoney("1234.56789000")).toBe("1,234.57");
  });

  it("null / undefined / 非法值都是「—」，不是 0.00", () => {
    expect(formatEfficiencyMoney(null)).toBe("—");
    expect(formatEfficiencyMoney(undefined)).toBe("—");
    expect(formatEfficiencyMoney("")).toBe("—");
    expect(formatEfficiencyMoney("nope")).toBe("—");
  });

  it("0 是**真数**，显示 0.00（与 null 不同）", () => {
    // 会话侧点观测口径下 avg_capital_used 恒为 0——那个 0 是真的算出来是 0，
    // 只是含义是「量不了时长」。它与「判不了」是两件事，不能一起变成「—」。
    expect(formatEfficiencyMoney("0.00000000")).toBe("0.00");
    expect(formatEfficiencyMoney(0)).toBe("0.00");
  });
});

describe("formatEfficiencyPct（百分点 float）", () => {
  it("不再乘 100：后端给的就是百分点", () => {
    // 25.0 意味着 25%。若误当比值再乘 100，这里会变成 2500.00%
    expect(formatEfficiencyPct(25.0)).toBe("25.00%");
    expect(formatEfficiencyPct(100.0)).toBe("100.00%");
    expect(formatEfficiencyPct(5.004)).toBe("5.00%");
  });

  it("0 是真数，显示 0.00%", () => {
    expect(formatEfficiencyPct(0)).toBe("0.00%");
  });

  it("null / undefined 是「—」，不是 0.00%", () => {
    expect(formatEfficiencyPct(null)).toBe("—");
    expect(formatEfficiencyPct(undefined)).toBe("—");
    expect(formatEfficiencyPct(NaN)).toBe("—");
  });
});

describe("capitalEfficiencyCards（三种降级）", () => {
  const full = {
    avg_capital_used: "2500.00000000",
    capital_utilization: 25.0,
    idle_pct: 75.0,
    time_in_market_pct: 12.5,
    return_per_capital_used_pct: 8.0,
    sample_sufficient: false,
  };

  it("有钱效率段时六张卡都在", () => {
    const cards = capitalEfficiencyCards(full);
    expect(cards.map((c) => c.label)).toEqual([
      "平均占用资金",
      "资金利用率",
      "闲置比例",
      "在仓时间占比",
      "占用资金回报",
      "效率样本",
    ]);
    expect(cards[0].value).toBe("2,500.00");
    expect(cards[1].value).toBe("25.00%");
    expect(cards[5].value).toBe("不足");
  });

  it("**键压根不存在**（存量回测）→ 空数组，整块不渲染", () => {
    // 这是现实里最常见的一支：老回测的 metrics 里没有这个键（不是 null、不是 {}）。
    expect(capitalEfficiencyCards(undefined)).toEqual([]);
    expect(capitalEfficiencyCards(null)).toEqual([]);
  });

  it("个别值是 null → 那一项被丢掉，其余照常显示", () => {
    const cards = capitalEfficiencyCards({
      ...full,
      return_per_capital_used_pct: null,
      time_in_market_pct: null,
    });
    const labels = cards.map((c) => c.label);
    expect(labels).not.toContain("占用资金回报");
    expect(labels).not.toContain("在仓时间占比");
    // 其余四项还在——一项判不了不该把整段藏起来
    expect(labels).toContain("资金利用率");
    expect(labels).toContain("闲置比例");
    expect(cards.length).toBe(4);
  });

  it("六个值全是 null → 没有数字卡（页面另有说明文字那一支）", () => {
    const cards = capitalEfficiencyCards({
      avg_capital_used: null,
      capital_utilization: null,
      idle_pct: null,
      time_in_market_pct: null,
      return_per_capital_used_pct: null,
      sample_sufficient: null,
    });
    expect(cards).toEqual([]);
  });

  it("判不了的那几项绝不出现 0 或 0.00%", () => {
    const cards = capitalEfficiencyCards({
      avg_capital_used: null,
      capital_utilization: null,
      idle_pct: null,
      time_in_market_pct: null,
      return_per_capital_used_pct: null,
      sample_sufficient: null,
    });
    const values = cards.map((c) => c.value);
    expect(values).not.toContain("0.00%");
    expect(values).not.toContain("0.00");
    expect(values).not.toContain("$0.00");
  });

  it("会话侧那种「真 0 + 真 null」的混合：0 照出，null 出「—」被滤掉", () => {
    // 点观测口径的实际形状：利用率 0、闲置 100、在仓 0、回报 null。
    const cards = capitalEfficiencyCards({
      avg_capital_used: "0.00000000",
      capital_utilization: 0.0,
      idle_pct: 100.0,
      time_in_market_pct: 0.0,
      return_per_capital_used_pct: null,
      sample_sufficient: false,
    });
    const byLabel = Object.fromEntries(cards.map((c) => [c.label, c.value]));
    expect(byLabel["资金利用率"]).toBe("0.00%");
    expect(byLabel["闲置比例"]).toBe("100.00%");
    expect(byLabel["平均占用资金"]).toBe("0.00");
    // 回报是 null → 整项不在（不是 0.00%）
    expect(byLabel["占用资金回报"]).toBeUndefined();
  });

  it("样本充足/不足由后端的 flag 决定，不是前端比大小", () => {
    expect(
      capitalEfficiencyCards({ ...full, sample_sufficient: true })[5].value
    ).toBe("充足");
    expect(
      capitalEfficiencyCards({ ...full, sample_sufficient: true })[5].cls
    ).toBe("text-green");
  });
});
