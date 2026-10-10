"use client";

import { useEffect, useMemo, useState } from "react";
import type { Order, ExchangeAccountWithBalance, OHLCVPoint } from "@/lib/api";
import { tradingApi } from "@/lib/api";
import { formatOrderPnl } from "@/lib/format";
import TView from "@/components/backtest/TView";
import type { TViewOrderMarker } from "@/components/backtest/TView";

// ── Formatting helpers ──

function formatNumber(n: string | number, decimals = 2): string {
  const num = typeof n === "string" ? parseFloat(n) : n;
  if (isNaN(num)) return "0.00";
  return num.toFixed(decimals).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function statusLabel(status: Order["status"]): string {
  const map: Record<Order["status"], string> = {
    pending: "待提交",
    submitted: "已提交",
    partial: "部分成交",
    filled: "已成交",
    cancelled: "已取消",
    failed: "失败",
  };
  return map[status] || status;
}

function statusBadge(status: Order["status"]): string {
  const active = ["pending", "submitted", "partial"].includes(status);
  return active
    ? "bg-green-dim text-green"
    : status === "filled"
      ? "bg-blue-dim text-blue"
      : status === "failed"
        ? "bg-red-dim text-red"
        : "bg-bg2 text-text3";
}

// 已实现盈亏的展示口径走 `lib/format`：订单行上 0 与 null 一律「—」——开仓成交
// 的已实现盈亏在交易所口径里就是 0（见 `formatOrderPnl` 的注释），显示成 `+0.00`
// 会让每张进场单看起来像打平的平仓单。

function fmtTime(value: string): string {
  return new Date(value).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

// ── Trade Detail Modal ──

function DetailRow({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-[10px] text-text3 font-medium flex-shrink-0">{label}</span>
      <span className={`text-[11px] font-semibold text-text text-right truncate ${mono ? "font-mono" : ""}`}>
        {value}
      </span>
    </div>
  );
}

function TradeDetailModal({
  order,
  accountLabel,
  allOrders,
  onClose,
}: {
  order: Order;
  accountLabel: string;
  allOrders: Order[];
  onClose: () => void;
}) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // ── K 线 + 历史订单（TView） ──
  const [interval, setInterval] = useState("1h");
  const [ohlcv, setOhlcv] = useState<OHLCVPoint[]>([]);
  const [klinesLoading, setKlinesLoading] = useState(true);
  const [klinesError, setKlinesError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setKlinesLoading(true);
    setKlinesError(null);
    setOhlcv([]);
    tradingApi
      .getKlines(order.symbol, interval, 300)
      .then((res) => {
        if (!stale) setOhlcv(res.ohlcv_data || []);
      })
      .catch((e) => {
        if (!stale) setKlinesError(e instanceof Error ? e.message : "K 线加载失败");
      })
      .finally(() => {
        if (!stale) setKlinesLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [order.symbol, interval]);

  // 同品种全部已(部分)成交订单 → 图标记
  const orderMarkers = useMemo<TViewOrderMarker[]>(() => {
    return allOrders
      .filter((o) => o.symbol === order.symbol && parseFloat(o.filled_quantity) > 0)
      .map((o) => ({
        time: o.updated_at || o.created_at,
        side: o.side,
        price: o.avg_fill_price
          ? parseFloat(o.avg_fill_price)
          : o.price
            ? parseFloat(o.price)
            : null,
        quantity: o.filled_quantity,
        filled: o.status === "filled" || o.status === "partial",
        highlighted: o.id === order.id,
      }));
  }, [allOrders, order.symbol, order.id]);

  const priceLine = useMemo(() => {
    const p = order.avg_fill_price
      ? parseFloat(order.avg_fill_price)
      : order.price
        ? parseFloat(order.price)
        : null;
    if (p == null || isNaN(p) || p <= 0) return null;
    return { price: p, title: order.avg_fill_price ? "成交均价" : "委托价" };
  }, [order]);

  const sideColor = order.side === "buy" ? "bg-green-dim text-green" : "bg-red-dim text-red";
  const typeLabel =
    order.order_type === "market"
      ? "市价"
      : order.order_type === "limit"
        ? "限价"
        : "止损";
  const pnl = formatOrderPnl(order.realized_pnl);
  const filledPct =
    parseFloat(order.quantity) > 0
      ? (parseFloat(order.filled_quantity) / parseFloat(order.quantity)) * 100
      : 0;

  const shortId = (id: string) => (id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id);

  return (
    <div
      className="fixed inset-0 bg-black/70 z-[100] flex items-center justify-center backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        className="bg-bg1 border border-[rgba(255,255,255,0.1)] rounded-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between sticky top-0 bg-bg1 z-10">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-xs font-bold text-text flex-shrink-0">交易详情</span>
            <span className="font-mono text-[11px] font-semibold text-text2 truncate">{order.symbol}</span>
            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded flex-shrink-0 ${sideColor}`}>
              {order.side === "buy" ? "买" : "卖"}
            </span>
            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded flex-shrink-0 ${statusBadge(order.status)}`}>
              {statusLabel(order.status)}
            </span>
          </div>
          <button
            onClick={onClose}
            className="w-6 h-6 flex items-center justify-center rounded-md text-text3 hover:text-text hover:bg-bg2 cursor-pointer transition-all flex-shrink-0"
            aria-label="关闭"
          >
            <svg width="12" height="12" viewBox="0 0 12 12">
              <path d="M2 2 L10 10 M10 2 L2 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <div className="px-4 py-3 flex flex-col gap-3">
          {/* K 线 · 历史订单 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg overflow-hidden">
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold px-3 pt-2 pb-1">
              K 线 · 历史订单
              {klinesError && (
                <span className="ml-2 normal-case tracking-normal text-red">{klinesError}</span>
              )}
            </div>
            <div className="px-1 pb-1">
              <TView
                ohlcv={ohlcv}
                orderMarkers={orderMarkers}
                loading={klinesLoading}
                priceLine={priceLine}
                timeframe={interval}
                onTimeframeChange={setInterval}
              />
            </div>
          </div>

          {/* 成交信息 */}
          <div className="grid grid-cols-3 gap-2">
            <div className="bg-bg2/60 border border-[rgba(255,255,255,0.05)] rounded-lg px-3 py-2.5">
              <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">委托数量</div>
              <div className="font-mono text-sm font-bold text-text mt-0.5">{formatNumber(order.quantity, 4)}</div>
            </div>
            <div className="bg-bg2/60 border border-[rgba(255,255,255,0.05)] rounded-lg px-3 py-2.5">
              <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">已成交</div>
              <div className="font-mono text-sm font-bold text-text mt-0.5">
                {formatNumber(order.filled_quantity, 4)}
                <span className="text-[10px] font-semibold text-text3 ml-1">({filledPct.toFixed(0)}%)</span>
              </div>
            </div>
            <div className="bg-bg2/60 border border-[rgba(255,255,255,0.05)] rounded-lg px-3 py-2.5">
              <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">已实现盈亏</div>
              <div className={`font-mono text-sm font-bold mt-0.5 ${pnl.color}`}>{pnl.text}</div>
            </div>
          </div>

          {/* 触发策略 */}
          <div className="bg-bg2/60 border border-[rgba(255,255,255,0.05)] rounded-lg px-3 py-2.5 flex items-center justify-between">
            <span className="text-[9px] text-text3 uppercase tracking-wider font-semibold">触发策略</span>
            {order.strategy_name ? (
              <span className="text-[11px] font-bold text-purple">{order.strategy_name}</span>
            ) : (
              <span className="text-[11px] font-semibold text-text3">手动 / 未关联策略</span>
            )}
          </div>

          {/* 价格信息 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg px-3 py-1">
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold pt-1.5 pb-0.5">价格</div>
            <DetailRow
              label="委托价格"
              value={order.price ? formatNumber(order.price) : "—"}
              mono
            />
            <DetailRow
              label="成交均价"
              value={order.avg_fill_price ? formatNumber(order.avg_fill_price) : "—"}
              mono
            />
            <DetailRow label="订单类型" value={typeLabel} />
          </div>

          {/* 订单信息 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg px-3 py-1">
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold pt-1.5 pb-0.5">订单信息</div>
            <DetailRow label="交易所账户" value={accountLabel} />
            <DetailRow label="订单 ID" value={shortId(order.id)} mono />
            <DetailRow label="请求 ID" value={shortId(order.request_id)} mono />
            <DetailRow
              label="交易所订单号"
              value={order.exchange_order_id ? shortId(order.exchange_order_id) : "—"}
              mono
            />
            <DetailRow
              label="关联会话"
              value={order.live_session_id ? shortId(order.live_session_id) : "—"}
              mono
            />
          </div>

          {/* 时间信息 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg px-3 py-1">
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold pt-1.5 pb-0.5">时间</div>
            <DetailRow label="创建时间" value={fmtTime(order.created_at)} mono />
            <DetailRow label="更新时间" value={fmtTime(order.updated_at)} mono />
          </div>

          {/* 错误信息 */}
          {order.error_message && (
            <div className="bg-red-dim/20 border border-red/25 rounded-lg px-3 py-2.5">
              <div className="text-[9px] text-red uppercase tracking-wider font-semibold mb-1">错误信息</div>
              <div className="text-[11px] text-red/90 font-mono break-all leading-relaxed">{order.error_message}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Trade Records Section ──

type RecordFilter = "all" | "filled" | "active" | "cancelled" | "failed";

const FILTERS: { key: RecordFilter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "filled", label: "已成交" },
  { key: "active", label: "进行中" },
  { key: "cancelled", label: "已取消" },
  { key: "failed", label: "失败" },
];

export default function TradeRecords({
  orders,
  accounts,
  loading,
}: {
  orders: Order[];
  accounts: ExchangeAccountWithBalance[];
  loading: boolean;
}) {
  const [filter, setFilter] = useState<RecordFilter>("all");
  const [selected, setSelected] = useState<Order | null>(null);

  const accountLabelMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const acc of accounts) {
      const label = acc.label ? `${acc.exchange} · ${acc.label}` : acc.exchange;
      map.set(String(acc.id), label);
    }
    return map;
  }, [accounts]);

  const filtered = useMemo(() => {
    if (filter === "all") return orders;
    if (filter === "filled") return orders.filter((o) => o.status === "filled");
    if (filter === "active")
      return orders.filter((o) => ["pending", "submitted", "partial"].includes(o.status));
    return orders.filter((o) => o.status === filter);
  }, [orders, filter]);

  const filledCount = orders.filter((o) => o.status === "filled").length;
  const activeCount = orders.filter((o) => ["pending", "submitted", "partial"].includes(o.status)).length;

  const selectedAccountLabel = selected
    ? accountLabelMap.get(String(selected.exchange_account)) || `账户 #${selected.exchange_account}`
    : "";

  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between">
        <div className="text-xs font-semibold flex items-center gap-2">
          <svg width="12" height="12" viewBox="0 0 12 12">
            <rect x="1" y="2.5" width="10" height="8" rx="1.5" fill="none" stroke="var(--color-purple)" strokeWidth="1.2" />
            <line x1="1" y1="5" x2="11" y2="5" stroke="var(--color-purple)" strokeWidth="1" />
            <line x1="3.5" y1="2.5" x2="3.5" y2="4" stroke="var(--color-purple)" strokeWidth="1.2" strokeLinecap="round" />
            <line x1="8.5" y1="2.5" x2="8.5" y2="4" stroke="var(--color-purple)" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
          交易记录
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider bg-purple-dim text-purple">
            {orders.length} 条
          </span>
        </div>
        {/* Filter tabs */}
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`px-2.5 py-1 rounded-md text-[10px] font-semibold cursor-pointer transition-all border ${
                filter === f.key
                  ? "bg-green-dim border-green/20 text-green"
                  : "bg-bg2 border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
              }`}
            >
              {f.label}
              {f.key === "filled" && filledCount > 0 && (
                <span className="ml-1 text-[9px] font-bold">{filledCount}</span>
              )}
              {f.key === "active" && activeCount > 0 && (
                <span className="ml-1 text-[9px] font-bold">{activeCount}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      {loading && orders.length === 0 ? (
        <div className="px-4 py-8 text-center">
          <div className="text-xs text-text3 animate-pulse">加载交易记录中...</div>
        </div>
      ) : filtered.length === 0 ? (
        <div className="px-4 py-8 text-center">
          <div className="text-xs text-text3">
            {filter === "all" ? "暂无交易记录" : "该状态下暂无交易记录"}
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[rgba(255,255,255,0.05)]">
                <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">时间</th>
                <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">品种</th>
                <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">触发策略</th>
                <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">方向</th>
                <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">类型</th>
                <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">委托价</th>
                <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">成交均价</th>
                <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">已成交</th>
                <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">已实现盈亏</th>
                <th className="text-center py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">状态</th>
                <th className="text-center py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">详情</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((order) => {
                const sideLabel = order.side === "buy" ? "买" : "卖";
                const sideColor = order.side === "buy" ? "text-green" : "text-red";
                const typeLabel =
                  order.order_type === "market"
                    ? "市价"
                    : order.order_type === "limit"
                      ? "限价"
                      : "止损";
                const pnl = formatOrderPnl(order.realized_pnl);
                return (
                  <tr
                    key={order.id}
                    onClick={() => setSelected(order)}
                    className="border-b border-[rgba(255,255,255,0.03)] hover:bg-bg2/50 cursor-pointer transition-colors"
                  >
                    <td className="py-2.5 px-4 font-mono text-text3 whitespace-nowrap">
                      {new Date(order.created_at).toLocaleTimeString("zh-CN", {
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                        hour12: false,
                      })}
                    </td>
                    <td className="py-2.5 px-4 font-mono font-semibold text-text">{order.symbol}</td>
                    <td className="py-2.5 px-4">
                      {order.strategy_name ? (
                        <span className="text-[10px] font-semibold text-purple">{order.strategy_name}</span>
                      ) : (
                        <span className="text-[10px] text-text3/60">—</span>
                      )}
                    </td>
                    <td className={`py-2.5 px-4 font-semibold ${sideColor}`}>{sideLabel}</td>
                    <td className="py-2.5 px-4 text-text2">{typeLabel}</td>
                    <td className="py-2.5 px-4 font-mono text-right text-text2">
                      {order.price ? formatNumber(order.price) : "—"}
                    </td>
                    <td className="py-2.5 px-4 font-mono text-right text-text2">
                      {order.avg_fill_price ? formatNumber(order.avg_fill_price) : "—"}
                    </td>
                    <td className="py-2.5 px-4 font-mono text-right text-text2">
                      {formatNumber(order.filled_quantity, 4)}
                      {parseFloat(order.filled_quantity) > 0 && (
                        <span className="text-[9px] text-text3 ml-0.5">/{formatNumber(order.quantity, 4)}</span>
                      )}
                    </td>
                    <td className={`py-2.5 px-4 font-mono text-right font-semibold ${pnl.color}`}>{pnl.text}</td>
                    <td className="py-2.5 px-4 text-center">
                      <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${statusBadge(order.status)}`}>
                        {statusLabel(order.status)}
                      </span>
                    </td>
                    <td className="py-2.5 px-4 text-center">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelected(order);
                        }}
                        className="inline-flex items-center gap-1 text-[10px] font-semibold text-purple hover:text-text cursor-pointer transition-colors"
                      >
                        查看
                        <svg width="10" height="10" viewBox="0 0 12 12">
                          <path d="M4 2 L8 6 L4 10" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <TradeDetailModal
          order={selected}
          accountLabel={selectedAccountLabel}
          allOrders={orders}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}