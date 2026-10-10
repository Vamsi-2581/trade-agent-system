"use client";

/**
 * 会话详情弹窗（卡片头部「详情」按钮打开）。
 *
 * 七块：①会话概要 ②收益统计 ③运行参数 ④来源回测摘要 ⑤策略本体 ⑥熔断层 ⑦该会话订单流水。
 *
 * **数字一律来自接口，前端不算**。会话统计（合计/笔数/胜率/极值）与「样本不足」的判定
 * 都在后端（`session_stats.py`）：门槛放在前端写死，改口径时这里不会变红。同理，
 * 熔断层标题逐字用后端给的 `text`（与下单通路、`start_notice` 同一个换算口），不自己按
 * label/scope 拼一遍。
 *
 * 卡片上的「盈亏」与本弹窗的「已实现盈亏合计」是**同一个数**（后端两种取数，有测试钉着
 * 相等）。这里不显示任何收益率：会话的资金归属无法切分（全仓无持仓表、会话停止不关仓），
 * 一个「收益率」会凭一个不存在的分母说话。
 *
 * ④⑤ 走既有接口（`backtestApi.getDetail` / `tradingApi.getStrategies`）：这两块的取数今天
 * 就在会话响应的外键上（`backtest_result_id`、`strategy`），后端没有为它们加字段。
 */

import { useEffect, useState } from "react";
import {
  backtestApi,
  liveSessionApi,
  tradingApi,
  type BacktestResult,
  type LiveSession,
  type LiveSessionDetail,
  type Order,
  type Strategy,
} from "@/lib/api";
import { formatNumber, formatOrderPnl, formatPnl } from "@/lib/format";

/** 打开时取一次；`running`/`paused` 的数字还在变，跟随页面同样的节奏刷新。
 *  其余状态（`stopped`/`error`/`pending`）不轮询——那些数字不会再变了。 */
const POLL_MS = 10000;
const POLLING_STATUSES: LiveSession["status"][] = ["running", "paused"];

// ── 展示用小工具（与 trading/page.tsx 的同名徽章文案保持一致） ──

function sessionStatusBadge(status: LiveSession["status"]): { text: string; color: string } {
  const map: Record<LiveSession["status"], { text: string; color: string }> = {
    pending: { text: "待启动", color: "bg-amber-dim text-amber" },
    running: { text: "运行中", color: "bg-green-dim text-green" },
    paused: { text: "已暂停", color: "bg-text3/20 text-text3" },
    stopped: { text: "已停止", color: "bg-bg2 text-text3" },
    error: { text: "异常", color: "bg-red-dim text-red" },
  };
  return map[status] || { text: status, color: "bg-bg2 text-text3" };
}

function orderStatusLabel(status: Order["status"]): string {
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

/**
 * 金额：null 一律「—」（不是 0）。正负号与配色走 `lib/format` 的统一口径。
 *
 * `order` 用于订单流水：开仓成交的已实现盈亏在交易所口径里就是 0，显示成
 * `+0.00` 会让每张进场单看起来像打平的平仓单，因此那一栏走 `formatOrderPnl`
 * （0 也当空）。会话合计、持仓盈亏这类「0 是真数」的地方不带 `order`。
 */
function Amount({
  value,
  size = "sm",
  order = false,
}: {
  value: string | null;
  size?: "sm" | "lg";
  order?: boolean;
}) {
  if (value === null) return <span className="font-mono text-text3">—</span>;
  const pnl = order ? formatOrderPnl(value) : formatPnl(value);
  return (
    <span className={`font-mono font-semibold ${size === "lg" ? "text-sm" : "text-[11px]"} ${pnl.color}`}>
      {pnl.text}
    </span>
  );
}

function StatCell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-bg2/60 border border-[rgba(255,255,255,0.05)] rounded-lg px-3 py-2.5">
      <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold">{label}</div>
      <div className="mt-0.5">{children}</div>
    </div>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="text-[10px] text-text3 font-medium flex-shrink-0">{label}</span>
      <span
        className={`text-[11px] font-semibold text-text text-right truncate ${mono ? "font-mono" : ""}`}
      >
        {value}
      </span>
    </div>
  );
}

function BlockHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[9px] text-text3 uppercase tracking-wider font-semibold pt-1.5 pb-1 px-3">
      {children}
    </div>
  );
}

function paramValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// ── 单块 ──

function StatsBlock({ stats }: { stats: LiveSessionDetail["stats"] }) {
  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg pb-2.5">
      <BlockHeader>收益统计 · 会话归因已实现盈亏</BlockHeader>
      <div data-testid="session-stats" className="px-3">
        {stats.closed_orders === 0 ? (
          <div className="text-[11px] text-text3 py-1.5">
            该会话尚无成交（统计只计入有已实现盈亏的平仓成交）
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-2">
            <StatCell label="已实现盈亏合计">
              <Amount value={stats.realized_pnl_total} size="lg" />
            </StatCell>
            <StatCell label="成交笔数">
              <span className="font-mono text-sm font-bold text-text">{stats.closed_orders}</span>
            </StatCell>
            <StatCell label="胜率">
              <span className="font-mono text-sm font-bold text-text">
                {stats.win_rate === null ? "—" : `${(stats.win_rate * 100).toFixed(1)}%`}
              </span>
              <span className="font-mono text-[10px] text-text3 ml-1">
                {stats.winning_orders}/{stats.closed_orders}
              </span>
              {!stats.sample_sufficient && (
                <span className="ml-1 text-[9px] font-bold px-1.5 py-0.5 rounded bg-amber-dim text-amber">
                  样本不足
                </span>
              )}
            </StatCell>
            <StatCell label="平均每笔">
              <Amount value={stats.avg_pnl} />
            </StatCell>
            <StatCell label="最大单笔盈利">
              <Amount value={stats.best_pnl} />
            </StatCell>
            <StatCell label="最大单笔亏损">
              <Amount value={stats.worst_pnl} />
            </StatCell>
          </div>
        )}
        {stats.caveats.length > 0 && (
          <ul className="mt-2 space-y-0.5">
            {stats.caveats.map((c) => (
              <li key={c} className="text-[10px] text-text3 leading-relaxed">
                · {c}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function ParamsBlock({ params }: { params: LiveSessionDetail["params"] }) {
  return (
    <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg pb-2">
      <BlockHeader>运行参数 · 实际传给策略的那一份</BlockHeader>
      <div className="px-3">
        {!params.resolved ? (
          <div className="text-[11px] text-text3 leading-relaxed py-1">
            解析不到策略实现类（{params.strategy_name ?? "未知策略"}），如实回 config 原文：
            <pre className="mt-1 text-[10px] font-mono text-text2 whitespace-pre-wrap break-all">
              {JSON.stringify(params.config, null, 2)}
            </pre>
          </div>
        ) : params.rows.length === 0 ? (
          <div className="text-[11px] text-text3 py-1">该策略没有声明任何参数，会话也没有覆盖。</div>
        ) : (
          <table className="w-full text-[11px]">
            <thead>
              <tr className="border-b border-[rgba(255,255,255,0.05)]">
                <th className="text-left py-1.5 text-[9px] text-text3 uppercase tracking-wider font-semibold">参数</th>
                <th className="text-right py-1.5 text-[9px] text-text3 uppercase tracking-wider font-semibold">策略默认</th>
                <th className="text-right py-1.5 text-[9px] text-text3 uppercase tracking-wider font-semibold">本会话覆盖</th>
                <th className="text-right py-1.5 text-[9px] text-text3 uppercase tracking-wider font-semibold">实际生效</th>
              </tr>
            </thead>
            <tbody>
              {params.rows.map((row) => (
                <tr key={row.key} className="border-b border-[rgba(255,255,255,0.03)]">
                  <td className="py-1.5 font-mono text-text2">
                    {row.key}
                    {!row.declared && (
                      <span className="ml-1.5 text-[9px] font-bold px-1 py-0.5 rounded bg-amber-dim text-amber">
                        策略未声明
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 font-mono text-right text-text3">{paramValue(row.default)}</td>
                  <td className="py-1.5 font-mono text-right text-text3">{paramValue(row.override)}</td>
                  <td className="py-1.5 font-mono text-right font-semibold text-text">
                    {paramValue(row.effective)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── 主组件 ──

export default function SessionDetailModal({
  session,
  onClose,
}: {
  session: LiveSession;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<LiveSessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [backtestFailed, setBacktestFailed] = useState(false);
  const [strategy, setStrategy] = useState<Strategy | null>(null);

  // Esc 关闭（照 TradeDetailModal 的形态）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // 详情：打开取一次 + 按状态轮询
  useEffect(() => {
    let stale = false;
    const load = () => {
      liveSessionApi
        .getDetail(session.id)
        .then((d) => {
          if (stale) return;
          setDetail(d);
          setError(null);
        })
        .catch((e) => {
          // 接口挂了就说接口挂了——不拿列表里那份旧数据假装是真的
          if (!stale) setError(e instanceof Error ? e.message : "详情加载失败");
        })
        .finally(() => {
          if (!stale) setLoading(false);
        });
    };
    load();
    const livish = POLLING_STATUSES.includes(session.status);
    const timer = livish ? setInterval(load, POLL_MS) : undefined;
    return () => {
      stale = true;
      if (timer) clearInterval(timer);
    };
  }, [session.id, session.status]);

  // ④ 来源回测摘要：取数在既有接口上，不改后端
  useEffect(() => {
    const id = session.backtest_result_id;
    if (!id) return;
    let stale = false;
    backtestApi
      .getDetail(id)
      .then((b) => {
        if (!stale) setBacktest(b);
      })
      .catch(() => {
        if (!stale) setBacktestFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [session.backtest_result_id]);

  // ⑤ 策略本体：没有按 id 取的接口，只有列表（`strategy_list`），在本地挑出这一条
  useEffect(() => {
    let stale = false;
    tradingApi
      .getStrategies()
      .then((list) => {
        if (stale) return;
        setStrategy(list.find((s) => String(s.id) === String(session.strategy)) ?? null);
      })
      .catch(() => {
        /* 挑不到就如实显示「不可用」，不编 */
      });
    return () => {
      stale = true;
    };
  }, [session.strategy]);

  // 详情到手后以它为准（列表那份可能落后一轮），未到手先渲染列表那份
  const s: LiveSession = detail ?? session;
  const statusInfo = sessionStatusBadge(s.status);
  const modeText = s.mode === "live" ? "实盘" : "模拟";
  const modeColor = s.mode === "live" ? "bg-green-dim text-green" : "bg-blue-dim text-blue";

  return (
    <div
      className="fixed inset-0 bg-black/70 z-[100] flex items-center justify-center backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="会话详情"
        className="bg-bg1 border border-[rgba(255,255,255,0.1)] rounded-xl w-full max-w-3xl max-h-[90vh] overflow-y-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header —— ①会话概要里也有一份品种，断言要能指名道姓说「头部这一份」 */}
        <div
          data-testid="session-detail-header"
          className="px-4 py-3 border-b border-[rgba(255,255,255,0.07)] flex items-center justify-between sticky top-0 bg-bg1 z-10"
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-xs font-bold text-text flex-shrink-0">会话详情</span>
            <span className="text-[11px] font-semibold text-text2 truncate">{s.strategy_name}</span>
            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded flex-shrink-0 ${modeColor}`}>
              {modeText}
            </span>
            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded flex-shrink-0 ${statusInfo.color}`}>
              {statusInfo.text}
            </span>
            <span className="font-mono text-[11px] font-semibold text-text2 truncate">{s.symbol}</span>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            {loading && <span className="text-[10px] text-text3 animate-pulse">加载中...</span>}
            <button
              onClick={onClose}
              className="w-6 h-6 flex items-center justify-center rounded-md text-text3 hover:text-text hover:bg-bg2 cursor-pointer transition-all"
              aria-label="关闭"
            >
              <svg width="12" height="12" viewBox="0 0 12 12">
                <path d="M2 2 L10 10 M10 2 L2 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        </div>

        <div className="px-4 py-3 flex flex-col gap-3">
          {error && (
            <div className="bg-red-dim/20 border border-red/25 rounded-lg px-3 py-2.5">
              <div className="text-[9px] text-red uppercase tracking-wider font-semibold mb-1">详情取数失败</div>
              <div className="text-[11px] text-red/90 font-mono break-all leading-relaxed">{error}</div>
              <div className="text-[10px] text-text3 mt-1">
                {detail
                  ? "下面的统计与订单流水是最后一次成功取到的那份，可能已过期。"
                  : "下面只显示列表里已有的会话信息，统计与订单流水暂缺。"}
              </div>
            </div>
          )}

          {/* ① 会话概要 */}
          <div
            data-testid="session-overview"
            className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg pb-1"
          >
            <BlockHeader>会话概要</BlockHeader>
            <div className="grid grid-cols-2 gap-x-4 px-3 pb-1">
              <Row label="策略" value={s.strategy_name} />
              <Row label="品种" value={s.symbol} />
              <Row label="定位" value={modeText} />
              <Row label="状态" value={statusInfo.text} />
              <Row label="交易所账户" value={s.exchange_account_name || "—"} />
              <Row label="初始资金" value={`$${formatNumber(s.initial_capital)}`} />
              <Row label="当前权益" value={s.current_equity ? `$${formatNumber(s.current_equity)}` : "—"} />
              {/* 详情没到手时是「—」，**不是** `当前权益 - 初始资金` 现算一个 */}
              <Row label="已实现盈亏" value={<Amount value={detail?.realized_pnl ?? null} size="lg" />} />
              <Row label="启动时间" value={s.started_at ? fmtTime(s.started_at) : "—"} mono />
              <Row label="创建时间" value={fmtTime(s.created_at)} mono />
            </div>
          </div>

          {/* ② 收益统计 */}
          {detail ? (
            <StatsBlock stats={detail.stats} />
          ) : (
            !error && (
              <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg px-3 py-4 text-center">
                <div className="text-[11px] text-text3 animate-pulse">收益统计加载中...</div>
              </div>
            )
          )}

          {/* ③ 运行参数 */}
          {detail && <ParamsBlock params={detail.params} />}

          {/* ④ 来源回测摘要 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg pb-2">
            <BlockHeader>来源回测</BlockHeader>
            <div className="px-3">
              {backtest ? (
                <div className="flex items-center justify-between gap-3 py-1.5">
                  <div className="flex flex-col gap-0.5 min-w-0">
                    <span className="text-[11px] font-semibold text-text">
                      {backtest.symbol} · {backtest.timeframe} · {backtest.start_date} ~ {backtest.end_date}
                    </span>
                    <span className="text-[10px] text-text3">审核：{backtest.review_status}</span>
                  </div>
                  <a
                    href={`/backtest/${backtest.id}`}
                    className="text-[10px] font-semibold text-purple hover:text-text transition-colors flex-shrink-0"
                  >
                    查看回测 →
                  </a>
                </div>
              ) : (
                <div className="text-[11px] text-text3 py-1.5">
                  {session.backtest_result_id
                    ? backtestFailed
                      ? "回测摘要取不到（接口失败），可到回测页查看。"
                      : "回测摘要加载中..."
                    : "这条会话没有来源回测（手动创建的会话）。"}
                </div>
              )}
            </div>
          </div>

          {/* ⑤ 策略本体 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg pb-1">
            <BlockHeader>策略本体</BlockHeader>
            <div className="px-3">
              {strategy ? (
                <>
                  <div className="flex items-center justify-between gap-3 py-1.5">
                    <span className="text-[10px] text-text3 font-medium">名称</span>
                    <span className="text-[11px] font-semibold text-text">{strategy.name}</span>
                  </div>
                  <div className="flex items-center justify-between gap-3 py-1.5">
                    <span className="text-[10px] text-text3 font-medium">代码版本</span>
                    <span className="text-[11px] font-mono text-text2 truncate">
                      {strategy.git_commit_hash || "—"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3 py-1.5">
                    <span className="text-[10px] text-text3 font-medium">启用状态</span>
                    <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${strategy.is_active ? "bg-green-dim text-green" : "bg-bg2 text-text3"}`}>
                      {strategy.is_active ? "已启用" : "已停用"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3 py-1.5">
                    <span className="text-[10px] text-text3 font-medium">代码路径</span>
                    <span className="text-[11px] font-mono text-text2 truncate">{strategy.code_path || "—"}</span>
                  </div>
                </>
              ) : (
                <div className="text-[11px] text-text3 py-1.5">
                  策略行不可用（策略列表里找不到这条会话引用的策略）。
                </div>
              )}
            </div>
          </div>

          {/* ⑥ 熔断层 —— 只在真的有层拦着这条会话时出现（照 start_notice 口径：
              不命中就一个字都不说） */}
          {detail && detail.halt_layers.length > 0 && (
            <div className="bg-red-dim/20 border border-red/25 rounded-lg pb-2">
              <div className="text-[9px] text-red uppercase tracking-wider font-semibold pt-2 pb-1 px-3">
                正在拦这条会话
              </div>
              <div className="px-3 flex flex-col gap-1.5">
                {detail.halt_layers.map((layer) => (
                  <div key={`${layer.text}-${layer.opened_at}`} className="text-[11px] leading-relaxed">
                    <div className="font-semibold text-red/90">{layer.text}</div>
                    <div className="text-text3">
                      {layer.reason} · 起 {fmtTime(layer.opened_at)} ·{" "}
                      {layer.expires_at ? `至 ${fmtTime(layer.expires_at)}` : "无到期"}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ⑦ 该会话订单流水 */}
          <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-lg overflow-hidden">
            <div className="flex items-center justify-between gap-3 px-3">
              <BlockHeader>该会话订单流水</BlockHeader>
              {detail && detail.orders_total > 0 && (
                <span className="text-[10px] text-text3 flex-shrink-0">
                  {detail.orders.length < detail.orders_total
                    ? `最近 ${detail.orders.length} 条（共 ${detail.orders_total} 条）`
                    : `共 ${detail.orders_total} 条`}
                </span>
              )}
            </div>
            {!detail ? (
              <div className="px-3 py-4 text-center">
                <div className="text-[11px] text-text3 animate-pulse">订单流水加载中...</div>
              </div>
            ) : detail.orders.length === 0 ? (
              <div className="px-3 pb-3 text-[11px] text-text3">该会话尚无订单。</div>
            ) : (
              <div className="overflow-x-auto" data-testid="session-orders">
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="border-b border-[rgba(255,255,255,0.05)]">
                      <th className="text-left py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">时间</th>
                      <th className="text-left py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">方向</th>
                      <th className="text-left py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">类型</th>
                      <th className="text-right py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">成交均价</th>
                      <th className="text-right py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">已成交</th>
                      <th className="text-right py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">已实现盈亏</th>
                      <th className="text-center py-2 px-3 text-[9px] text-text3 uppercase tracking-wider font-semibold">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.orders.map((o) => (
                      <tr key={o.id} className="border-b border-[rgba(255,255,255,0.03)]">
                        <td className="py-2 px-3 font-mono text-text3 whitespace-nowrap">
                          {new Date(o.created_at).toLocaleTimeString("zh-CN", {
                            hour: "2-digit",
                            minute: "2-digit",
                            second: "2-digit",
                            hour12: false,
                          })}
                        </td>
                        <td className={`py-2 px-3 font-semibold ${o.side === "buy" ? "text-green" : "text-red"}`}>
                          {o.side === "buy" ? "买" : "卖"}
                        </td>
                        <td className="py-2 px-3 text-text2">
                          {o.order_type === "market" ? "市价" : o.order_type === "limit" ? "限价" : "止损"}
                        </td>
                        <td className="py-2 px-3 font-mono text-right text-text2">
                          {o.avg_fill_price ? formatNumber(o.avg_fill_price) : "—"}
                        </td>
                        <td className="py-2 px-3 font-mono text-right text-text2">
                          {formatNumber(o.filled_quantity, 4)}
                        </td>
                        <td className="py-2 px-3 text-right">
                          <Amount value={o.realized_pnl} order />
                        </td>
                        <td className="py-2 px-3 text-center">
                          <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-bg2 text-text3">
                            {orderStatusLabel(o.status)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
