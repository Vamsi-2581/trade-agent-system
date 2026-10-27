"use client";

import DashboardShell from "@/components/layout/DashboardShell";
import { useOrders } from "@/hooks/useOrders";
import { usePositions } from "@/hooks/usePositions";
import { useAccounts } from "@/hooks/useAccounts";
import { useFrameControl } from "@/hooks/useFrameControl";
import { useLiveSessions } from "@/hooks/useLiveSessions";
import { useHaltState } from "@/hooks/useHaltState";
import { useRegimeOverview } from "@/hooks/useRegimeOverview";
import TradeRecords from "@/components/trading/TradeRecords";
import SessionDetailModal from "@/components/trading/SessionDetailModal";
import HaltBanner from "@/components/trading/HaltBanner";
import type {
  Order,
  ExchangeAccountWithBalance,
  LiveSession,
  PromoteDryRun,
} from "@/lib/api";
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
  // `balance` 为 null = **这个数没取到**（`/api/trading/accounts/` 只在 OrderExecutor
  // 跑着时才去问交易所，否则这个键就是 null）。它必须显示成「—」，**不能编成 $0**：
  // `bal?.total ?? "0"` 把「不知道」渲染成了「你的实盘资产是 0 美元」，而这恰好就是
  // 2026-10-24「binance 实盘没有同步现有资产」那张截图的样子——用户看到 $0，以为
  // 系统没去同步，实际上系统压根没问过交易所。
  const money = (v: string | undefined) =>
    typeof v === "string" && v !== "" ? `$${formatNumber(v)}` : "—";

  const indicatorColor = account.testnet
    ? "var(--color-blue)"
    : "var(--color-green)";
  const badgeText = account.testnet ? "模拟" : "实盘";
  const badgeColor = account.testnet
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
            <div className="font-mono text-sm font-bold text-text mt-0.5">{money(account.balance?.total)}</div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">可用余额</div>
            <div className="font-mono text-[11px] font-semibold text-text2 mt-0.5">
              {money(account.balance?.available)}
            </div>
          </div>
          <div>
            <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">已用保证金</div>
            <div className="font-mono text-[11px] font-semibold text-text2 mt-0.5">
              {money(account.balance?.used)}
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
          {account.exchange} · {account.label || (account.testnet ? "模拟" : "实盘")}
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
          {account.exchange} · {account.label || (account.testnet ? "模拟" : "实盘")}
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
  promoteDisabledReason,
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
  /**
   * 非空时「升级为实盘」不可点，并把原因显示成 title。
   *
   * 存在的理由：这个按钮点了要人指定**目标实盘账户**，而账户列表为空时它无路可走。
   * 旧版没有这条路，按钮点了发一次无 body 的 POST、后端回干跑摘要、前端丢掉，
   * 表现就是「点了没反应」。宁可按钮变灰并说明原因，也不要它静默无效。
   */
  promoteDisabledReason?: string | null;
}) {
  const statusInfo = sessionStatusBadge(session.status);
  const equity = session.current_equity ? formatNumber(session.current_equity) : "—";
  const initial = formatNumber(session.initial_capital);
  // 与详情弹窗的「已实现盈亏合计」同一个数（同一个接口字段、后端两种取数有测试钉着相等）。
  // **不再用 `current_equity - initial_capital` 现算**：那个差是账户权益口径，
  // 一次写库失败（`_bump_session_equity` 吞异常只记 warning）就会与订单汇总分家，
  // 同屏两个「盈亏」都看着正常、却不一样。字段为 null 时显示「—」，不退回现算。
  const pnl = session.realized_pnl !== null ? formatPnl(session.realized_pnl) : null;
  // 资金效率（spec #30 用户故事 18：指标在会话列表的统计接口里同样可得）。
  //
  // **先看 `capital_efficiency_metrics_available`，再看数字。** 后端把六个契约键摊平到
  // 列表项顶层（与回测侧逐字同名），而会话侧那组数是点观测口径：`capital_utilization`
  // 会是 `0`，但那个 0 的意思是「这一侧量不了持仓时长」，不是「没占用资金」。把它当回测
  // 指标读，就是把重仓短打读成「几乎没用钱」——方向正好是危险的那一侧。
  //
  // available 为假时后端给的是 `null`（不是 0）；这里连 `null` 都不去格式化成 `0.00%`，
  // 只出「—」。**不许 `?? 0`**：那一步会把这条口径防线整条抹掉。
  const efficiencyAvailable = session.capital_efficiency_metrics_available === true;
  const utilization =
    efficiencyAvailable && session.capital_utilization != null
      ? `${session.capital_utilization.toFixed(2)}%`
      : "—";

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
          {/* 「停 · N 层」：N 来自列表接口（后端一次查询算完全部会话，判据与详情模态逐层
              列出的是同一个 `halt._matches`），所以这个数不可能与点开后看到的东西对不上。
              `0` 与缺字段都不渲染——「没有徽章」本身就是那个答案，而横幅常显已经把「此刻
              没有停止判定命中」说过了，这里再标一次只是噪声。
              卡片上**不展开层本身**：那是详情的事。这也是「只报第一层」那条禁令的护栏——
              这里根本没有位置去只报一层。 */}
          {session.halt_layer_count ? (
            <span
              data-testid="session-halt-badge"
              data-halt-layer-count={session.halt_layer_count}
              title="该会话此刻被停止判定拦住——点「详情」看是哪几层"
              className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-red-dim/30 text-red"
            >
              停 · {session.halt_layer_count} 层
            </span>
          ) : null}
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
        {/* 资金效率一行：**只在标志为真、且确实有数时出现**。
            判不了 / 老响应 / 未启动的会话一律整行不渲染——列表卡片不是解释口径的地方，
            为此加一行「判不了（invalid_capital）」只会让卡片变吵；详细的说明与
            `capital_efficiency_notes` 在详情弹窗里（`EfficiencyBlock`）。 */}
        {efficiencyAvailable && utilization !== "—" && (
          <div
            data-testid="session-efficiency-card"
            className="flex items-center gap-3 mb-2.5 text-[9px] text-text3"
          >
            <span>
              资金利用率
              <span className="font-mono font-bold text-text2 ml-1">{utilization}</span>
            </span>
            <span className="text-amber" title="会话侧只有点观测，时间权重恒为 0">
              点观测口径
            </span>
          </div>
        )}
        {session.status === "running" && (
          <div className="flex gap-1.5">
            <button onClick={onPause} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-amber-dim/50 text-amber hover:bg-amber-dim disabled:opacity-40 disabled:cursor-not-allowed">
              暂停
            </button>
            <button onClick={onStop} disabled={actionLoading} className="px-2 py-1 rounded text-[9px] font-semibold bg-red-dim/50 text-red hover:bg-red-dim disabled:opacity-40 disabled:cursor-not-allowed">
              停止
            </button>
            {session.mode === "paper" && (
              <button
                onClick={onPromote}
                disabled={actionLoading || Boolean(promoteDisabledReason)}
                title={promoteDisabledReason ?? "把这条模拟会话升成实盘会话（需先选目标实盘账户并确认）"}
                className="px-2 py-1 rounded text-[9px] font-semibold bg-green-dim/50 text-green hover:bg-green-dim disabled:opacity-40 disabled:cursor-not-allowed ml-auto"
              >
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

// ── Promote Confirm Dialog ──

/**
 * 升级为实盘的确认层（spec #19 D11 的三步卡点：选账户 → 看干跑摘要 → 确认）。
 *
 * **三个决定都是「不要第二个口径」**：
 *
 * 1. **摘要正文逐字取后端 `dry_run.message`**：后端写好了「将停止哪条会话、用哪个账户
 *    建 live 会话、继承多少权益」，前端再拼一遍就是第二个版本，而它迟早会说错一半。
 * 2. **目标账户必须人选**：后端不接受「继承 paper 账户」（那必然是 testnet，必然被
 *    `mode_account_mismatch` 拒绝）。所以这里只列 `testnet === false` 的账户。
 * 3. **没摘要就不许确认**：`preview` 为空时按钮发的是干跑请求，拿到摘要后才变成
 *    「确认升级」。防的是「两次点击之间状态丢了，于是又发一次干跑」——那种失败
 *    看起来像「确认按钮没用」。
 */
function PromoteConfirmDialog({
  session,
  liveAccounts,
  targetId,
  onTargetChange,
  preview,
  result,
  error,
  busy,
  onCancel,
  onConfirm,
}: {
  session: LiveSession;
  liveAccounts: ExchangeAccountWithBalance[];
  targetId: string;
  onTargetChange: (id: string) => void;
  preview: PromoteDryRun | null;
  result: { id: string; mode: string; status: string; message: string } | null;
  error: string | null;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div
        data-testid="promote-confirm"
        className="bg-bg1 border border-[rgba(255,255,255,0.1)] rounded-xl w-full max-w-lg p-5 space-y-4 max-h-[85vh] overflow-y-auto"
      >
        <div>
          <h3 className="text-sm font-bold text-text">升级为实盘</h3>
          <p className="text-[11px] text-text3 mt-1">
            {session.strategy_name} · {session.symbol}
            <span className="ml-2 font-mono">#{session.id.slice(0, 8)}</span>
          </p>
        </div>

        {result === null && (
          <div className="space-y-1.5">
            <label
              htmlFor="promote-target-account"
              className="text-[11px] font-semibold text-text2 block"
            >
              目标实盘账户
            </label>
            {liveAccounts.length === 0 ? (
              <p className="text-[11px] text-amber bg-amber-dim/20 border border-amber/30 rounded-lg px-3 py-2">
                没有可用的实盘账户（testnet=false）。paper 会话绑的是模拟账户，它不能当实盘账户用
                ——请先到「配置 → 交易所」添加一个实盘账户。
              </p>
            ) : (
              <select
                id="promote-target-account"
                data-testid="promote-target-account"
                value={targetId}
                onChange={(e) => onTargetChange(e.target.value)}
                disabled={busy || preview !== null}
                className="w-full bg-bg2 border border-[rgba(255,255,255,0.1)] rounded-md px-2 py-1.5 text-xs text-text"
              >
                <option value="">请选择实盘账户…</option>
                {liveAccounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.exchange} · {acc.label}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* 后端那段「将执行什么」——逐字显示，不加工。 */}
        {preview !== null && (
          <pre
            data-testid="promote-preview"
            className="text-[11px] leading-relaxed font-mono text-text2 whitespace-pre-wrap break-all bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg p-3"
          >
            {preview.message}
          </pre>
        )}

        {/* 停止判定的预告。**升级本身不被拦**——升级建的是会话，不是订单；真下单时才走
            `pre_trade_check`。所以这里做的是预告而不是又一道闸门，它也因此**不阻塞**确认
            按钮（下面那颗照旧可点）。
            层要**逐条列出**：本仓写过禁令——同时生效时只报一层，会让人按报出来那层去
            排查，而解除它之后单还是下不出去。
            那两句话由后端渲染（`halt_notice`），前端不拼、不按 trigger/scope 自己组。 */}
        {preview !== null && (
          <div
            data-testid="promote-halt-notice"
            data-halt-layer-count={preview.halt_layers.length}
            className={`rounded-lg border px-3 py-2 text-[11px] leading-relaxed ${
              preview.halt_layers.length > 0
                ? "bg-amber-dim/20 border-amber/30 text-amber"
                : "bg-bg2 border-[rgba(255,255,255,0.07)] text-text3"
            }`}
          >
            <p data-testid="promote-halt-notice-text">{preview.halt_notice}</p>
            {preview.halt_layers.length > 0 && (
              <ul data-testid="promote-halt-layers" className="mt-1 space-y-0.5">
                {preview.halt_layers.map((layer) => (
                  <li
                    key={`${layer.text}-${layer.opened_at}`}
                    data-testid="promote-halt-layer"
                    className="text-text2"
                  >
                    {layer.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {error !== null && (
          <p
            data-testid="promote-error"
            className="bg-red-dim/20 border border-red/30 rounded-lg px-3 py-2 text-[11px] text-red whitespace-pre-wrap break-all"
          >
            {error}
          </p>
        )}

        {result !== null ? (
          <div
            data-testid="promote-result"
            className="rounded-lg border border-green/40 bg-green-dim px-3 py-2 space-y-1 text-[11px] text-green"
          >
            <p>{result.message}</p>
            <p className="text-text3">
              新会话 <span className="font-mono text-text2">{result.id.slice(0, 8)}</span>
              （{result.mode} · {result.status}）
            </p>
            <button
              type="button"
              onClick={onCancel}
              className="bg-bg2 text-text px-3 py-1 rounded-md mt-1"
            >
              关闭
            </button>
          </div>
        ) : (
          <div className="flex gap-2 justify-end">
            <button
              type="button"
              data-testid="promote-cancel"
              onClick={onCancel}
              disabled={busy}
              className="px-4 py-1.5 rounded-md text-xs font-semibold border border-[rgba(255,255,255,0.1)] text-text3 hover:text-text disabled:opacity-40"
            >
              取消
            </button>
            {preview === null ? (
              <button
                type="button"
                data-testid="promote-request"
                onClick={onConfirm}
                disabled={busy || targetId === ""}
                className="px-4 py-1.5 rounded-md text-xs font-semibold border border-green/30 bg-green-dim text-green hover:bg-green-dim/80 disabled:opacity-40"
              >
                {busy ? "读取中…" : "下一步"}
              </button>
            ) : (
              <button
                type="button"
                data-testid="promote-confirm-ok"
                onClick={onConfirm}
                disabled={busy}
                className="px-4 py-1.5 rounded-md text-xs font-semibold border border-green/30 bg-green-dim text-green hover:bg-green-dim/80 disabled:opacity-40"
              >
                {busy ? "处理中…" : "确认升级"}
              </button>
            )}
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
    remove: removeSession,
    promotion,
    requestPromotion,
    confirmPromotion,
    dismissPromotion,
  } = useLiveSessions();

  // 「当前阶段是否放行」的两半，各自一个来源：`useRegimeOverview` 给阶段（当前是哪个行情
  // 阶段），`useHaltState` 给停止判定（此刻有没有层在拦）。**刻意分成两个 hook**——它们的
  // 后端是两个端点，合成一条会让这条横幅与本页别的轮询被迫同频，而停止判定比开关档位变得
  // 快得多（解除也要立刻看见）。
  const { data: regimeOverview } = useRegimeOverview();
  const { data: haltState, loading: haltLoading, error: haltError } = useHaltState();

  const [activeSection, setActiveSection] = useState<"all" | "live" | "paper">("all");
  // 只存 id，会话对象每轮渲染从 `sessions` 里现取——列表 10s 一轮，
  // 弹窗拿到的一直是当前那份；会话从列表消失（别处删了）时弹窗也跟着关。
  const [detailSessionId, setDetailSessionId] = useState<string | null>(null);
  const detailSession = detailSessionId
    ? sessions.find((s) => s.id === detailSessionId) ?? null
    : null;

  // 升级为实盘的两步确认状态（见 useLiveSessions 的 PromotionState）。
  // 待升级的会话 id 与选中的目标实盘账户分开存：账户是用户选的，会话是点出来的。
  const [promoteSessionId, setPromoteSessionId] = useState<string | null>(null);
  const [promoteTargetId, setPromoteTargetId] = useState<string>("");

  const isLoading = accLoading || posLoading || ordersLoading;
  const hasExecutor = frameStatus.order_executor === "running";

  const liveAccounts = accounts.filter((a) => !a.testnet);
  const paperAccounts = accounts.filter((a) => a.testnet);

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

  // 待升级的会话从 `sessions` 现取（与详情弹窗同一套理由：列表 10s 一轮，
  // 弹窗里显示的一直是当前那份；会话在别处被删掉时弹窗自动消失）。
  const promoteSession = promoteSessionId
    ? sessions.find((s) => s.id === promoteSessionId) ?? null
    : null;

  // 数据截止时刻**由前端从载荷里派生**——后端不造「现在」（`apps/regime/views.py` 的
  // overview 注释立过这条纪律，`useHaltState` 的 docstring 照办）。取最近一个 `opened_at`：
  // 横幅要说的是「这份判断是什么时候的」。无层时没有时刻可说，给 `null`——横幅据此把
  // 「还没读到」与「确实没有层」分开，两者都不许冒充放行。
  const haltLayersForBanner = haltState?.halt_layers ?? [];
  const haltAsOf: string | null =
    haltLayersForBanner.length > 0
      ? haltLayersForBanner
          .map((layer) => layer.opened_at)
          .reduce((latest, at) => (Date.parse(at) > Date.parse(latest) ? at : latest))
      : null;

  return (
    <DashboardShell>
      {/* 「当前阶段是否放行」：**常显**，两半一起说（阶段 + 停止判定）。
          「否」也必须是看得见的答案，所以它不因没有层而消失。 */}
      <HaltBanner
        regimeState={regimeOverview?.regime_state?.regime ?? null}
        haltLayers={haltLayersForBanner}
        asOf={haltAsOf}
        loading={haltLoading}
        error={haltError}
      />
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

      {/* Executor status warning。
          条件里**不能**再有 `accounts.length === 0`：账户是库里的行，跟执行器跑没跑
          毫无关系。带上它以后，只要配置过账户，这条提示就永远不出现——而「余额/持仓
          为什么是空的」恰恰是那时唯一需要被回答的问题（2026-10-24 用户看到卡片上
          一排「—」却没有任何解释）。 */}
      {!hasExecutor && !posLoading && (
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
                  onPromote={() => {
                    // 打开两步确认层。**不发请求**：目标账户还没选，发出去必然
                    // 被后端以「必须显式指定目标实盘账户」拒绝，而那句长文案
                    // 在没有确认层的年代连显示的地方都没有。
                    setPromoteTargetId("");
                    setPromoteSessionId(session.id);
                  }}
                  onDelete={() => removeSession(session.id)}
                  onDetail={() => setDetailSessionId(session.id)}
                  actionLoading={sessionActionLoading}
                  promoteDisabledReason={
                    liveAccounts.length === 0
                      ? "没有可用的实盘账户（testnet=false）：paper 会话绑的模拟账户不能当实盘账户用"
                      : null
                  }
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

      {/* 升级为实盘的两步确认（选账户 → 干跑摘要 → 确认） */}
      {promoteSession !== null && (
        <PromoteConfirmDialog
          session={promoteSession}
          liveAccounts={liveAccounts}
          targetId={promoteTargetId}
          onTargetChange={setPromoteTargetId}
          preview={promotion.preview}
          result={promotion.result}
          error={promotion.error}
          busy={promotion.busy}
          onCancel={() => {
            setPromoteSessionId(null);
            setPromoteTargetId("");
            dismissPromotion();
          }}
          onConfirm={() => {
            if (promotion.preview === null) {
              void requestPromotion(promoteSession.id, promoteTargetId);
            } else {
              void confirmPromotion(promoteSession.id, promoteTargetId);
            }
          }}
        />
      )}
    </DashboardShell>
  );
}
