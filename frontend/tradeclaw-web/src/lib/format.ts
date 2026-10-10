/**
 * 数值与盈亏的统一格式化。
 *
 * 原先这两个函数内联在 trading/page.tsx；右侧边栏当时的做法是自己"编"收益
 * （Math.random），造成两个页面口径不一致。收益显示必须只有一个口径，
 * 因此抽到这里，由交易页与侧边栏共同引用。
 */

export function formatNumber(n: string | number, decimals = 2): string {
  const num = typeof n === "string" ? parseFloat(n) : n;
  if (isNaN(num)) return "0.00";
  return num.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 未实现盈亏的统一展示口径：正数带 +，负数带 -，0/非法值中性色。 */
export function formatPnl(value: string | number): { text: string; color: string } {
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num) || num === 0) return { text: "0.00", color: "text-text3" };
  const formatted = formatNumber(Math.abs(num));
  return {
    text: `${num >= 0 ? "+" : "-"}${formatted}`,
    color: num >= 0 ? "text-green" : "text-red",
  };
}

/**
 * **订单行**的已实现盈亏展示口径：`0` 与 `null` 一视同仁，都显示「—」。
 *
 * 交易所的 `userTrades.realizedPnl` 对开仓成交就是 `0`——它说的是「这笔不产生
 * 已实现盈亏」，不是「恰好打平」。落库以交易所为准记成 0 是对的（对账要能逐字
 * 比对，见 `reconcile_realized_pnl_from_exchange`），但显示成 `+0.00` 会让每一张
 * 进场单看起来像一笔打平的平仓单。真正打平的平仓单本就罕见，且它的 0 也是
 * 「无盈亏可言」，因此这一层统一按空处理。
 *
 * 注意：**只用于订单流水**。持仓的未实现盈亏、会话合计这类「0 是真数」的地方
 * 仍走 `formatPnl`。
 */
export function formatOrderPnl(
  value: string | number | null,
): { text: string; color: string } {
  if (value === null || value === "") return { text: "—", color: "text-text3" };
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num) || num === 0) return { text: "—", color: "text-text3" };
  return formatPnl(num);
}
