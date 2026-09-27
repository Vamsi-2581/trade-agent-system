"use client";

import DashboardShell from "@/components/layout/DashboardShell";
import { useOrders } from "@/hooks/useOrders";
import { usePositions } from "@/hooks/usePositions";
import { useAccounts } from "@/hooks/useAccounts";
import { useFrameControl } from "@/hooks/useFrameControl";
import { useLiveSessions } from "@/hooks/useLiveSessions";
import TradeRecords from "@/components/trading/TradeRecords";
import SessionDetailModal from "@/components/trading/SessionDetailModal";
import type { Order, ExchangeAccountWithBalance, LiveSession } from "@/lib/api";
import { formatNumber, formatPnl } from "@/lib/format";
import { useState } from "react";

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
    : status === "failed"
      ? "bg-red-dim text-red"
      : "bg-bg2 text-text3";
}

// ── Frame Control Button ──

function FrameControlButton({
  running,
  loading,
  onStart,
  onStop,
}: {
  running: boolean;
  loading: boolean;
  onStart: () => void;
  onStop: () => void;
}) {
  const disabled = loading;

  if (running) {
    return (
      <button
        onClick={onStop}
        disabled={disabled}
        className="px-3 py-1.5 rounded-md text-[10px] font-semibold cursor-pointer transition-all border bg-red-dim/50 border-red/20 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {loading ? "停止中..." : "停止框架"}
      </button>
    );
  }

  return (
    <button
      onClick={onStart}
      disabled={disabled}
      className="px-3 py-1.5 rounded-md text-[10px] font-semibold cursor-pointer transition-all border border-green/20 bg-green-dim text-green hover:bg-green-dim/80 disabled:opacity-40 disabled:cursor-not-allowed"
    >
      {loading ? "启动中..." : "启动框架"}
    </button>
  );
}

// ── Single Account Card ──

function AccountMiniCard({
  account,
  connected,
}: {
  account: ExchangeAccountWithBalance;
  connected: boolean;
}) {
  const bal = account.balance;
  const total = bal?.total ?? "0";
  const available = bal?.available ?? "—";
  const used = bal?.used ?? "—";

  const indicatorColor = account.testnet_status
    ? "var(--color-blue)"
    : "var(--color-green)";
  const badgeText = account.testnet_status ? "模拟" : "实盘";
  const badgeColor = account.testnet_status
    ? "bg-blue-dim text-blue"
    : "bg-green-dim text-green";

  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
      <div className="px-3 py-2.5 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div
            className="w-2 h-2 rounded-full flex-shrink-0"
            style={{ background: indicatorColor, boxShadow: `0 0 6px ${indicatorColor}` }}
          />
          <span className="text-xs font-semibold text-text capitalize">{account.exchange}</span>
          <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${badgeColor}`}>
            {badgeText}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {connected ? (
            <div className="w-1.5 h-1.5 rounded-full bg-green animate-pulse-slow" />
          ) : (
            <div className="w-1.5 h-1.5 rounded-full bg-text3" />
          )}
        </div>
      </div>
      <div className="p-3">
        {account.label && (
          <div className="text-[10px] text-text3 mb-2 truncate">{account.label}</div>
        )}
        <div className="grid grid-cols-2 gap-y-2 gap-x-3">
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">总资产</div>
            <div className="font-mono text-sm font-bold text-text mt-0.5">${formatNumber(total)}</div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">可用余额</div>
            <div className="font-mono text-[11px] font-semibold text-text2 mt-0.5">
              ${typeof available === "string" ? formatNumber(available) : "—"}
            </div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">已用保证金</div>
            <div className="font-mono text-[11px] font-semibold text-text2 mt-0.5">
              ${typeof used === "string" ? formatNumber(used) : "—"}
            </div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">状态</div>
            <div className={`text-[11px] font-semibold mt-0.5 ${connected ? "text-green" : "text-text3"}`}>
              {connected ? "已连接" : "未连接"}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Positions Section for a single account ──

function AccountPositions({
  account,
  positions,
}: {
  account: ExchangeAccountWithBalance;
  positions: { symbol: string; side: string; quantity: string; entry_price: string; mark_price: string; unrealized_pnl: string }[];
}) {
  if (positions.length === 0) return null;

  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between">
        <div className="text-xs font-semibold flex items-center gap-2">
          <svg width="12" height="12" viewBox="0 0 12 12">
            <rect x="1" y="3" width="10" height="6" rx="1" fill="none" stroke="var(--color-teal)" strokeWidth="1.2" />
            <line x1="4" y1="3" x2="4" y2="9" stroke="var(--color-teal)" strokeWidth="1" />
            <line x1="8" y1="3" x2="8" y2="9" stroke="var(--color-teal)" strokeWidth="1" />
          </svg>
          {account.exchange} · {account.label || (account.testnet_status ? "模拟" : "实盘")}
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider bg-teal-dim text-teal">
            {positions.length} 个持仓
          </span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-[rgba(255,255,255,0.05)]">
              <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">品种</th>
              <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">方向</th>
              <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">数量</th>
              <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">开仓价</th>
              <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">标记价</th>
              <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">未实现盈亏</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((pos, i) => {
              const pnl = formatPnl(pos.unrealized_pnl);
              return (
                <tr key={`${pos.symbol}-${i}`} className="border-b border-[rgba(255,255,255,0.03)] hover:bg-bg2/50">
                  <td className="py-2.5 px-4 font-mono font-semibold text-text">{pos.symbol}</td>
                  <td className="py-2.5 px-4">
                    <span
                      className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${
                        pos.side === "long" ? "bg-green-dim text-green" : "bg-red-dim text-red"
                      }`}
                    >
                      {pos.side === "long" ? "多" : "空"}
                    </span>
                  </td>
                  <td className="py-2.5 px-4 font-mono text-right text-text2">{formatNumber(pos.quantity, 4)}</td>
                  <td className="py-2.5 px-4 font-mono text-right text-text2">{formatNumber(pos.entry_price)}</td>
                  <td className="py-2.5 px-4 font-mono text-right text-text2">{formatNumber(pos.mark_price)}</td>
                  <td className={`py-2.5 px-4 font-mono text-right font-semibold ${pnl.color}`}>{pnl.text}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Orders Section for a single account ──

function AccountOrders({
  account,
  orders,
}: {
  account: ExchangeAccountWithBalance;
  orders: Order[];
}) {
  if (orders.length === 0) return null;

  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between">
        <div className="text-xs font-semibold flex items-center gap-2">
          <svg width="12" height="12" viewBox="0 0 12 12">
            <circle cx="6" cy="6" r="5" fill="none" stroke="var(--color-amber)" strokeWidth="1.2" />
            <line x1="6" y1="3" x2="6" y2="6.5" stroke="var(--color-amber)" strokeWidth="1.2" strokeLinecap="round" />
            <line x1="6" y1="6.5" x2="8.5" y2="8" stroke="var(--color-amber)" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
          {account.exchange} · {account.label || (account.testnet_status ? "模拟" : "实盘")}
          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider bg-amber-dim text-amber">
            {orders.length} 个活跃订单
          </span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-[rgba(255,255,255,0.05)]">
              <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">时间</th>
              <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">品种</th>
              <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">方向</th>
              <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">类型</th>
              <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">价格</th>
              <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">已成交</th>
              <th className="text-center py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">状态</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => {
              const sideLabel = order.side === "buy" ? "买" : "卖";
              const sideColor = order.side === "buy" ? "text-green" : "text-red";
              const typeLabel = order.order_type === "market" ? "市价" : "限价";
              return (
                <tr key={order.id} className="border-b border-[rgba(255,255,255,0.03)] hover:bg-bg2/50">
                  <td className="py-2.5 px-4 font-mono text-text3">
                    {new Date(order.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                  </td>
                  <td className="py-2.5 px-4 font-mono font-semibold text-text">{order.symbol}</td>
                  <td className={`py-2.5 px-4 font-semibold ${sideColor}`}>{sideLabel}</td>
                  <td className="py-2.5 px-4 text-text2">{typeLabel}</td>
                  <td className="py-2.5 px-4 font-mono text-right text-text2">
                    {order.price ? formatNumber(order.price) : "—"}
                  </td>
                  <td className="py-2.5 px-4 font-mono text-right text-text2">
                    {formatNumber(order.filled_quantity, 4)} / {formatNumber(order.quantity, 4)}
                  </td>
                  <td className="py-2.5 px-4 text-center">
                    <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${statusBadge(order.status)}`}>
                      {statusLabel(order.status)}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Session Status Badge ──

function sessionStatusBadge(sessionStatus: LiveSession["status"]): { text: string; color: string } {
  const map: Record<LiveSession["status"], { text: string; color: string }> = {
    pending: { text: "待启动", color: "bg-amber-dim text-amber" },
    running: { text: "运行中", color: "bg-green-dim text-green" },
    paused: { text: "已暂停", color: "bg-text3/20 text-text3" },
    stopped: { text: "已停止", color: "bg-bg2 text-text3" },
    error: { text: "异常", color: "bg-red-dim text-red" },
  };
  return map[sessionStatus] || { text: sessionStatus, color: "bg-bg2 text-text3" };
}

function sessionModeBadge(mode: LiveSession["mode"]): string {
  return mode === "live" ? "实盘" : "模拟";
}

function sessionModeColor(mode: LiveSession["mode"]): string {
  return mode === "live" ? "bg-green-dim text-green" : "bg-blue-dim text-blue";
}

// ── Live Session Mini Card ──

function SessionMiniCard({
  session,
  onStart,
  onPause,
  onResume,
  onStop,
  onPromote,
  onDelete,
  onDetail,
  actionLoading,
}: {
  session: LiveSession;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onPromote: () => void;
  onDelete: () => void;
  onDetail: () => void;
  actionLoading: boolean;
}) {
  const statusInfo = sessionStatusBadge(session.status);
  const equity = session.current_equity ? formatNumber(session.current_equity) : "—";
  const initial = formatNumber(session.initial_capital);
  // 与详情弹窗的「已实现盈亏合计」同一个数（同一个接口字段、后端两种取数有测试钉着相等）。
  // **不再用 `current_equity - initial_capital` 现算**：那个差是账户权益口径，
  // 一次写库失败（`_bump_session_equity` 吞异常只记 warning）就会与订单汇总分家，
  // 同屏两个「盈亏」都看着正常、却不一样。字段为 null 时显示「—」，不退回现算。
  const pnl = session.realized_pnl !== null ? formatPnl(session.realized_pnl) : null;

  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
      <div className="px-3 py-2.5 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-text">{session.strategy_name}</span>
          <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider ${sessionModeColor(session.mode)}`}>
            {sessionModeBadge(session.mode)}
          </span>
          <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${statusInfo.color}`}>
            {statusInfo.text}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] font-mono text-text3">{session.symbol}</span>
          <button
            onClick={onDetail}
            aria-label="会话详情"
            className="px-2 py-0.5 rounded text-[9px] font-semibold cursor-pointer transition-all border border-[rgba(255,255,255,0.07)] bg-bg2 text-text3 hover:text-text"
          >
            详情
          </button>
        </div>
      </div>
      <div className="p-3">
        <div className="grid grid-cols-4 gap-3 mb-2.5">
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">初始资金</div>
            <div className="font-mono text-xs font-bold text-text mt-0.5">${initial}</div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">当前权益</div>
            <div className="font-mono text-xs font-bold text-text mt-0.5">${equity}</div>
          </div>
          <div>
            {/* 与弹窗同一口径（会话归因已实现盈亏），所以标签也叫同一个名字：
                它不含未实现部分，而「盈亏」二字容易被读成「权益变化了多少」 */}
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">已实现盈亏</div>
            <div className={`font-mono text-xs font-bold mt-0.5 ${pnl?.color ?? "text-text3"}`}>
              {pnl ? pnl.text : "—"}
            </div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">交易所</div>
            <div className="text-[11px] font-semibold text-text2 mt-0.5 capitalize">
              {session.exchange_account_name || "—"}
            </div>
          </div>
        </div>
        {session.status === "running" && (
          <div className="flex gap-1.5">
            <button onClick={onPause} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-amber-dim/50 text-amber hover:bg-amber-dim disabled:opacity-40 disabled:cursor-not-allowed">
              暂停
            </button>
            <button onClick={onStop} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-red-dim/50 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed">
              停止
            </button>
            {session.mode === "paper" && (
              <button onClick={onPromote} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-green-dim/50 text-green hover:bg-green-dim disabled:opacity-40 disabled:cursor-not-allowed ml-auto">
                升级为实盘
              </button>
            )}
          </div>
        )}
        {session.status === "paused" && (
          <div className="flex gap-1.5">
            <button onClick={onResume} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-green-dim/50 text-green hover:bg-green-dim disabled:opacity-40 disabled:cursor-not-allowed">
              恢复
            </button>
            <button onClick={onStop} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-red-dim/50 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed">
              停止
            </button>
          </div>
        )}
        {session.status === "pending" && (
          <div className="flex gap-1.5">
            <button onClick={onStart} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-green-dim/50 text-green hover:bg-green-dim disabled:opacity-40 disabled:cursor-not-allowed">
              启动
            </button>
            <button onClick={onDelete} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-red-dim/50 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed ml-auto">
              删除
            </button>
          </div>
        )}
        {session.status === "error" && (
          <div className="flex gap-1.5">
            <button onClick={onStart} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-green-dim/50 text-green hover:bg-green-dim disabled:opacity-40 disabled:cursor-not-allowed">
              重试
            </button>
            <button onClick={onDelete} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-red-dim/50 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed ml-auto">
              删除
            </button>
          </div>
        )}
        {session.status === "stopped" && (
          <div className="flex gap-1.5">
            <button onClick={onStart} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-green-dim/50 text-green hover:bg-green-dim disabled:opacity-40 disabled:cursor-not-allowed">
              重新启动
            </button>
            <button onClick={onDelete} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-red-dim/50 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed ml-auto">
              删除
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main Page ──

export default function TradingPage() {
  const { accounts, loading: accLoading } = useAccounts();
  const { positions, loading: posLoading } = usePositions();
  const { orders, loading: ordersLoading } = useOrders();
  const {
    status: frameStatus,
    loading: frameLoading,
    actionLoading,
    start: startFrame,
    stop: stopFrame,
  } = useFrameControl();
  const {
    sessions,
    loading: sessionsLoading,
    actionLoading: sessionActionLoading,
    start: startSession,
    pause: pauseSession,
    resume: resumeSession,
    stop: stopSession,
    promote: promoteSession,
    remove: removeSession,
  } = useLiveSessions();

  const [activeSection, setActiveSection] = useState<"all" | "live" | "paper">("all");
  // 只存 id，会话对象每轮渲染从 `sessions` 里现取——列表 10s 一轮，
  // 弹窗拿到的一直是当前那份；会话从列表消失（别处删了）时弹窗也跟着关。
  const [detailSessionId, setDetailSessionId] = useState<string | null>(null);
  const detailSession = detailSessionId
    ? sessions.find((s) => s.id === detailSessionId) ?? null
    : null;

  const isLoading = accLoading || posLoading || ordersLoading;
  const hasExecutor = frameStatus.order_executor === "running";

  const liveAccounts = accounts.filter((a) => !a.testnet_status);
  const paperAccounts = accounts.filter((a) => a.testnet_status);

  // Group positions by exchange_account_id (using exchange name as fallback)
  const positionsByAccount = new Map<string, typeof positions.positions>();
  for (const pos of positions.positions) {
    const key = pos.exchange_account_id || pos.exchange;
    if (!positionsByAccount.has(key)) positionsByAccount.set(key, []);
    positionsByAccount.get(key)!.push(pos);
  }

  // Group active orders by exchange_account
  const activeOrders = orders.filter((o) =>
    ["pending", "submitted", "partial"].includes(o.status)
  );
  const ordersByAccount = new Map<string, Order[]>();
  for (const order of activeOrders) {
    const key = String(order.exchange_account);
    if (!ordersByAccount.has(key)) ordersByAccount.set(key, []);
    ordersByAccount.get(key)!.push(order);
  }

  // Determine visible accounts
  const visibleAccounts =
    activeSection === "live"
      ? liveAccounts
      : activeSection === "paper"
        ? paperAccounts
        : [...liveAccounts, ...paperAccounts];

  const totalPositions = positions.positions.length;
  const totalActiveOrders = activeOrders.length;

  return (
    <DashboardShell>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-bold">交易中心</h1>
          {!isLoading && (
            <span className="text-[10px] text-text3">
              {accounts.length} 个账户 · {totalPositions} 个持仓 · {totalActiveOrders} 活跃订单
            </span>
          )}
          {/* Debug: raw frame status */}
          <span className="text-[10px] font-mono text-purple">
            [{frameStatus.trading}/{frameStatus.order_executor}/{frameStatus.data_feed}]
          </span>
        </div>
        <div className="flex items-center gap-2">
          {isLoading && (
            <div className="text-[10px] text-text3 animate-pulse">加载数据中...</div>
          )}
          {/* Frame control */}
          <FrameControlButton
            running={hasExecutor}
            loading={actionLoading}
            onStart={startFrame}
            onStop={stopFrame}
          />
          {/* Section filter */}
          <div className="flex gap-1">
            {(["all", "live", "paper"] as const).map((key) => {
              const labels = { all: "全部", live: "实盘", paper: "模拟" };
              return (
                <button
                  key={key}
                  onClick={() => setActiveSection(key)}
                  className={`px-2.5 py-1 rounded-md text-[10px] font-semibold cursor-pointer transition-all border ${
                    activeSection === key
                      ? "bg-green-dim border-green/20 text-green"
                      : "bg-bg2 border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
                  }`}
                >
                  {labels[key]}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Executor status warning */}
      {!hasExecutor && !posLoading && accounts.length === 0 && (
        <div className="bg-amber-dim/20 border border-amber/30 rounded-lg px-4 py-3 flex items-center gap-3">
          <div className="w-2 h-2 rounded-full bg-amber" />
          <span className="text-xs text-amber">
            交易框架未启动，持仓和余额数据暂不可用。请点击上方「启动框架」按钮。
          </span>
        </div>
      )}

      {/* Live Sessions Section */}
      {!sessionsLoading && sessions.length > 0 && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <svg width="12" height="12" viewBox="0 0 12 12">
              <circle cx="6" cy="6" r="5" fill="none" stroke="var(--color-green)" strokeWidth="1.2" />
              <path d="M4 6 L5.5 7.5 L8 4.5" fill="none" stroke="var(--color-green)" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="text-xs font-semibold text-text">交易会话</span>
            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-green-dim text-green">
              {sessions.filter((s) => s.status === "running").length} 运行中
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            {sessions
              .filter((s) => {
                if (activeSection === "live") return s.mode === "live";
                if (activeSection === "paper") return s.mode === "paper";
                return true;
              })
              .map((session) => (
                <SessionMiniCard
                  key={session.id}
                  session={session}
                  onStart={() => startSession(session.id)}
                  onPause={() => pauseSession(session.id)}
                  onResume={() => resumeSession(session.id)}
                  onStop={() => stopSession(session.id)}
                  onPromote={() => promoteSession(session.id)}
                  onDelete={() => removeSession(session.id)}
                  onDetail={() => setDetailSessionId(session.id)}
                  actionLoading={sessionActionLoading}
                />
              ))}
          </div>
        </div>
      )}

      {/* Account Cards Grid */}
      {visibleAccounts.length > 0 ? (
        <div className="grid grid-cols-2 gap-3">
          {visibleAccounts.map((acc) => (
            <AccountMiniCard key={acc.id} account={acc} connected={hasExecutor} />
          ))}
        </div>
      ) : (
        !isLoading && (
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-8 text-center">
            <div className="text-xs text-text3">
              {activeSection === "all"
                ? "暂无交易所账户，请前往 配置 → 交易所 添加"
                : activeSection === "live"
                  ? "暂无实盘账户"
                  : "暂无模拟账户"}
            </div>
          </div>
        )
      )}

      {/* Positions & Orders per account */}
      {visibleAccounts.map((acc) => {
        const accPositions = positionsByAccount.get(acc.id) || positionsByAccount.get(acc.exchange) || [];
        const accOrders = ordersByAccount.get(acc.id) || [];
        return (
          <div key={acc.id} className="flex flex-col gap-3">
            <AccountPositions account={acc} positions={accPositions} />
            <AccountOrders account={acc} orders={accOrders} />
          </div>
        );
      })}

      {/* Fallback: show all positions/orders if no accounts yet */}
      {accounts.length === 0 && hasExecutor && totalPositions > 0 && (
        <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
          <div className="px-4 py-3 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between">
            <div className="text-xs font-semibold flex items-center gap-2">
              <svg width="12" height="12" viewBox="0 0 12 12">
                <rect x="1" y="3" width="10" height="6" rx="1" fill="none" stroke="var(--color-teal)" strokeWidth="1.2" />
                <line x1="4" y1="3" x2="4" y2="9" stroke="var(--color-teal)" strokeWidth="1" />
                <line x1="8" y1="3" x2="8" y2="9" stroke="var(--color-teal)" strokeWidth="1" />
              </svg>
              当前持仓
            </div>
            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wider bg-teal-dim text-teal">
              {totalPositions} 个
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-[rgba(255,255,255,0.05)]">
                  <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">交易所</th>
                  <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">品种</th>
                  <th className="text-left py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">方向</th>
                  <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">数量</th>
                  <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">开仓价</th>
                  <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">标记价</th>
                  <th className="text-right py-2.5 px-4 text-[9px] text-text3 uppercase tracking-wider font-semibold">未实现盈亏</th>
                </tr>
              </thead>
              <tbody>
                {positions.positions.map((pos, i) => {
                  const pnl = formatPnl(pos.unrealized_pnl);
                  return (
                    <tr key={`${pos.symbol}-${i}`} className="border-b border-[rgba(255,255,255,0.03)] hover:bg-bg2/50">
                      <td className="py-2.5 px-4 font-mono text-text2 capitalize">{pos.exchange}</td>
                      <td className="py-2.5 px-4 font-mono font-semibold text-text">{pos.symbol}</td>
                      <td className="py-2.5 px-4">
                        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${pos.side === "long" ? "bg-green-dim text-green" : "bg-red-dim text-red"}`}>
                          {pos.side === "long" ? "多" : "空"}
                        </span>
                      </td>
                      <td className="py-2.5 px-4 font-mono text-right text-text2">{formatNumber(pos.quantity, 4)}</td>
                      <td className="py-2.5 px-4 font-mono text-right text-text2">{formatNumber(pos.entry_price)}</td>
                      <td className="py-2.5 px-4 font-mono text-right text-text2">{formatNumber(pos.mark_price)}</td>
                      <td className={`py-2.5 px-4 font-mono text-right font-semibold ${pnl.color}`}>{pnl.text}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Trade Records (history + detail) */}
      <TradeRecords orders={orders} accounts={accounts} loading={ordersLoading} />

      {/* 会话详情（收益统计 / 生效参数 / 熔断层 / 该会话订单流水） */}
      {detailSession && (
        <SessionDetailModal
          session={detailSession}
          onClose={() => setDetailSessionId(null)}
        />
      )}
    </DashboardShell>
  );
}
