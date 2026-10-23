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

/** 一张展示卡片：标签 + 已格式化的值（`"—"` 表示这一项没有数）。 */
export interface MetricCard {
  label: string;
  value: string;
  cls: string;
}

/**
 * **资金效率那一段的两条格式化口径**（spec #30 用户故事 12，子票 #36）。
 *
 * 抽出来的理由不是复用，而是**这条链路上唯一的两种类型混淆点必须被用例钉住**：
 *
 * - `formatEfficiencyMoney` 收的是**8 位定点字符串**（`"10000.00000000"`）——后端把金额
 *   出成字符串，因为这条链路上的钱从不经过二进制浮点（与 `session_stats.money()` 一致）。
 * - `formatEfficiencyPct` 收的是 **float 的百分点**（`100.0` 就是 100%）——不是比值，
 *   所以**不再乘 100**。乘了的话 25% 会显示成 2500%。
 *
 * 两者对 `null` / `undefined` 一律出 `"—"`，**绝不退回 `0`**：那几个数是
 * `invalid_capital` / `empty_window` / `no_trades` 三种判不了的情形，`0%` 会被读成
 * 「完全没占用资金」——而真相是「这一段没有资金效率证据」。把 `0` 当兜底值就是把
 * 「量不了」显示成「效率很高」，方向正是最危险的那一侧。
 */
export function formatEfficiencyMoney(
  value: string | number | null | undefined,
): string {
  if (value === null || value === undefined || value === "") return "—";
  const num = typeof value === "string" ? parseFloat(value) : value;
  if (isNaN(num)) return "—";
  return num.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatEfficiencyPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number" || isNaN(value)) return "—";
  return `${value.toFixed(2)}%`;
}

/**
 * 回测 `metrics.capital_efficiency` → 要渲染的卡片数组（**纯函数**）。
 *
 * 返回空数组 = 这一段不渲染。三种降级都由这里决定，页面组件不再自己判：
 *
 * 1. `metrics` 整个缺失 / 空 → 外层的 `Object.keys(...).length > 0` 已挡（见页面），
 *    但仍在这里再判一次 `segment == null`；
 * 2. **`capital_efficiency` 键压根不存在**（存量回测就是这样，不是 null 也不是 `{}`）
 *    → 空数组，整块不出现，与改动前的页面完全一致；
 * 3. 有这一段但个别值是 `null` → 那一项出 `"—"` 并被 `.filter` 掉，其余照常显示。
 *
 * 这一层刻意不判「`computed === false` 就整块不显示」：判不了时`reason` 与 `notes`
 * 仍然要说给用户看（「为什么没有这个数」），所以那一支由页面渲染一块说明文字。
 */
export function capitalEfficiencyCards(
  segment:
    | {
        avg_capital_used?: string | null;
        capital_utilization?: number | null;
        idle_pct?: number | null;
        time_in_market_pct?: number | null;
        return_per_capital_used_pct?: number | null;
        sample_sufficient?: boolean | null;
      }
    | null
    | undefined,
): MetricCard[] {
  if (segment === null || segment === undefined) return [];
  const cards: MetricCard[] = [
    {
      label: "平均占用资金",
      value: formatEfficiencyMoney(segment.avg_capital_used),
      cls: "text-text",
    },
    {
      label: "资金利用率",
      value: formatEfficiencyPct(segment.capital_utilization),
      cls: "text-text",
    },
    {
      label: "闲置比例",
      value: formatEfficiencyPct(segment.idle_pct),
      cls: "text-text",
    },
    {
      label: "在仓时间占比",
      value: formatEfficiencyPct(segment.time_in_market_pct),
      cls: "text-text",
    },
    {
      label: "占用资金回报",
      value: formatEfficiencyPct(segment.return_per_capital_used_pct),
      cls: "text-text",
    },
    {
      label: "效率样本",
      value:
        segment.sample_sufficient === null ||
        segment.sample_sufficient === undefined
          ? "—"
          : segment.sample_sufficient
            ? "充足"
            : "不足",
      cls: segment.sample_sufficient ? "text-green" : "text-amber",
    },
  ];
  return cards.filter((m) => m.value !== "—");
}

/**
 * **机制读面的时刻格式**（P5 #49）：后端给 ISO 串；没有就是「—」。
 *
 * `—` 不是空串也不是「刚刚」：池化表可能一代都没有（冷启动）、判定可能还没生效过——
 * 那两件事要求人做的动作完全不同，而「刚刚」会把「从来没有」说成一个新鲜的事实。
 * 与 `components/regime/GenerationHistory` 的本地实现同一个展示口径（月-日 时:分，24h）。
 */
export function formatMoment(moment: string | null | undefined): string {
  if (!moment) return "—";
  const parsed = new Date(moment);
  if (Number.isNaN(parsed.getTime())) return moment;
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
