"use client";

import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import DashboardShell from "@/components/layout/DashboardShell";
import {
  backtestApi,
  liveSessionApi,
  exchangeApi,
  tradingApi,
  type BacktestDetail,
  type BacktestTrade,
  type OHLCVPoint,
  type IndicatorData,
  type ExchangeAccount,
  type StrategyParamSpec,
} from "@/lib/api";
import { resampleOHLCV, computeIndicators, timeframeToMinutes } from "@/lib/resample";
import { capitalEfficiencyCards } from "@/lib/format";
import TView from "@/components/backtest/TView";
import EquityChart from "@/components/backtest/EquityChart";
import DrawdownChart from "@/components/backtest/DrawdownChart";
import TradeLog from "@/components/backtest/TradeLog";

/** Timeframes available for client-side resampling */
const RESAMPLE_TARGETS = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;

type TabKey = "kline" | "equity" | "drawdown" | "trades";

interface CachedTimeframeData {
  ohlcv: OHLCVPoint[];
  indicators: IndicatorData;
}

export default function BacktestDetailPage() {
  const params = useParams();
  const id = params.id as string;

  const [detail, setDetail] = useState<BacktestDetail | null>(null);
  const [trades, setTrades] = useState<BacktestTrade[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<TabKey>("kline");

  // ── Review ──
  const [reviewNotes, setReviewNotes] = useState("");
  const [reviewLoading, setReviewLoading] = useState(false);

  // ── Rerun ──
  const [rerunLoading, setRerunLoading] = useState(false);
  const [rerunMessage, setRerunMessage] = useState<string | null>(null);

  // ── Deploy ──
  const [deployOpen, setDeployOpen] = useState(false);
  const [deployLoading, setDeployLoading] = useState(false);
  const [deployMode, setDeployMode] = useState<"paper" | "live">("paper");
  const [deployAccountId, setDeployAccountId] = useState("");
  const [accounts, setAccounts] = useState<ExchangeAccount[]>([]);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [deploySuccess, setDeploySuccess] = useState<string | null>(null);
  /** 部署时可覆盖的策略参数（键 → 值）。只放**用户显式改过**的键：
   *  没碰过的键不进 config，让会话走策略声明默认值（与后端
   *  `{**backtest.parameters, **config}` 的继承语义一致）。 */
  const [deployParams, setDeployParams] = useState<Record<string, unknown>>({});
  const [deploySchema, setDeploySchema] = useState<StrategyParamSpec[]>([]);
  const [deploySchemaError, setDeploySchemaError] = useState<string | null>(null);

  // ── Timeframe switching with cache ──
  const [activeTf, setActiveTf] = useState<string | null>(null);
  const [tfCache, setTfCache] = useState<Record<string, CachedTimeframeData>>({});
  const [tfLoading, setTfLoading] = useState(false);

  // ── Lazy-load window management ──
  // When total bars exceed the initial window, show a subset and load more on scroll.
  const INITIAL_WINDOW = 300;
  const BATCH_SIZE = 200;

  const [windowStart, setWindowStart] = useState(0);
  const [windowEnd, setWindowEnd] = useState(INITIAL_WINDOW);
  // Track whether the window was initialised from detail
  const windowInitialised = useRef(false);

  // ── Server-fetched earlier OHLCV (before backtest start_date) ──
  const [preOhlcv, setPreOhlcv] = useState<OHLCVPoint[]>([]);
  const [hasMoreEarlierData, setHasMoreEarlierData] = useState(true);
  const fetchingEarlierRef = useRef(false);

  // ── Server-fetched later OHLCV (after backtest end_date) ──
  const [postOhlcv, setPostOhlcv] = useState<OHLCVPoint[]>([]);
  const [hasMoreLaterData, setHasMoreLaterData] = useState(true);
  const fetchingLaterRef = useRef(false);

  // Initialise window to show the RIGHTMOST bars (newest data) when detail loads
  useEffect(() => {
    if (!detail || windowInitialised.current) return;
    const total = detail.ohlcv_data.length;
    if (total > INITIAL_WINDOW) {
      setWindowStart(total - INITIAL_WINDOW);
      setWindowEnd(total);
    } else {
      setWindowStart(0);
      setWindowEnd(total);
    }
    windowInitialised.current = true;
  }, [detail]);

  /** Expand the visible window or fetch earlier data from server */
  const loadMoreOhlcv = useCallback(
    (direction: "earlier" | "later") => {
      if (!detail) return;
      const total = detail.ohlcv_data.length;

      if (direction === "earlier") {
        if (windowStart > 0) {
          // Expand window into existing backtest data
          setWindowStart((prev) => {
            const next = Math.max(0, prev - BATCH_SIZE);
            return next < prev ? next : prev;
          });
        } else if (hasMoreEarlierData && !fetchingEarlierRef.current) {
          // Fetch earlier bars from server (before backtest start_date)
          fetchingEarlierRef.current = true;
          // Use the earliest already-fetched bar's timestamp as cursor,
          // or the chart's first visible bar timestamp on first call
          const cursor = preOhlcv.length > 0
            ? preOhlcv[0].timestamp
            : detail.ohlcv_data[0]?.timestamp ?? detail.start_date;
          backtestApi
            .fetchEarlierOhlcv(id, BATCH_SIZE, cursor, detail.timeframe, detail.symbol)
            .then((res) => {
              const newBars = res.ohlcv_data ?? [];
              // Deduplicate: remove bars whose timestamp already exists in
              // backtest data or pre-fetched data
              const existingTs = new Set([
                ...detail.ohlcv_data.map((b) => b.timestamp),
                ...preOhlcv.map((b) => b.timestamp),
              ]);
              const filtered = newBars.filter((b) => !existingTs.has(b.timestamp));
              setPreOhlcv((prev) => [...filtered, ...prev]);
              if ((res.count ?? 0) < BATCH_SIZE) {
                setHasMoreEarlierData(false);
              }
            })
            .catch(() => {
              setHasMoreEarlierData(false);
            })
            .finally(() => {
              fetchingEarlierRef.current = false;
            });
        }
      } else {
        // "later" direction
        if (windowEnd < total) {
          // Expand window into existing backtest data
          setWindowEnd((prev) => {
            const next = Math.min(total, prev + BATCH_SIZE);
            return next > prev ? next : prev;
          });
        } else if (hasMoreLaterData && !fetchingLaterRef.current) {
          // Fetch later bars from server (after backtest end_date)
          fetchingLaterRef.current = true;
          // Use the latest already-fetched bar's timestamp as cursor,
          // or the chart's last visible bar timestamp on first call
          const cursor = postOhlcv.length > 0
            ? postOhlcv[postOhlcv.length - 1].timestamp
            : detail.ohlcv_data[detail.ohlcv_data.length - 1]?.timestamp ?? detail.end_date;
          backtestApi
            .fetchLaterOhlcv(id, BATCH_SIZE, cursor, detail.timeframe, detail.symbol)
            .then((res) => {
              const newBars = res.ohlcv_data ?? [];
              // Deduplicate boundary bars against both backtest data and already-fetched bars
              const existingTs = new Set([
                ...detail.ohlcv_data.map((b) => b.timestamp),
                ...postOhlcv.map((b) => b.timestamp),
              ]);
              const filtered = newBars.filter((b) => !existingTs.has(b.timestamp));
              setPostOhlcv((prev) => [...prev, ...filtered]);
              if ((res.count ?? 0) < BATCH_SIZE) {
                setHasMoreLaterData(false);
              }
            })
            .catch(() => {
              setHasMoreLaterData(false);
            })
            .finally(() => {
              fetchingLaterRef.current = false;
            });
        }
      }
    },
    [detail, windowStart, windowEnd, hasMoreEarlierData, hasMoreLaterData, id, preOhlcv, postOhlcv]
  );

  /** Ensure OHLCV data is ascending (oldest first) */
  function ensureAscending(data: OHLCVPoint[]): OHLCVPoint[] {
    if (data.length < 2) return data;
    const first = new Date(data[0].timestamp).getTime();
    const last = new Date(data[data.length - 1].timestamp).getTime();
    return first > last ? [...data].reverse() : data;
  }

  /** Prepend `pad` null elements to every indicator array */
  const padIndicators = useCallback(
    (indicators: IndicatorData, pad: number): IndicatorData => {
      if (pad <= 0) return indicators;
      const result: Record<string, unknown> = {};
      const nulls: (null)[] = Array(pad).fill(null);
      for (const [key, value] of Object.entries(indicators)) {
        if (Array.isArray(value)) {
          result[key] = [...nulls, ...value];
        } else if (typeof value === "object" && value !== null) {
          const obj: Record<string, unknown> = {};
          for (const [k2, v2] of Object.entries(value)) {
            obj[k2] = Array.isArray(v2) ? [...nulls, ...v2] : v2;
          }
          result[key] = obj;
        }
      }
      return result as IndicatorData;
    },
    []
  );

  /** Append `pad` null elements to every indicator array */
  const padIndicatorsPost = useCallback(
    (indicators: IndicatorData, pad: number): IndicatorData => {
      if (pad <= 0) return indicators;
      const result: Record<string, unknown> = {};
      const nulls: (null)[] = Array(pad).fill(null);
      for (const [key, value] of Object.entries(indicators)) {
        if (Array.isArray(value)) {
          result[key] = [...value, ...nulls];
        } else if (typeof value === "object" && value !== null) {
          const obj: Record<string, unknown> = {};
          for (const [k2, v2] of Object.entries(value)) {
            obj[k2] = Array.isArray(v2) ? [...v2, ...nulls] : v2;
          }
          result[key] = obj;
        }
      }
      return result as IndicatorData;
    },
    []
  );

  /** Slice indicator arrays to match OHLCV window */
  const sliceIndicators = useCallback(
    (indicators: IndicatorData, start: number, end: number): IndicatorData => {
      const result: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(indicators)) {
        if (Array.isArray(value)) {
          result[key] = value.slice(start, end);
        } else if (typeof value === "object" && value !== null) {
          const obj: Record<string, unknown> = {};
          for (const [k2, v2] of Object.entries(value)) {
            obj[k2] = Array.isArray(v2) ? v2.slice(start, end) : v2;
          }
          result[key] = obj;
        }
      }
      return result as IndicatorData;
    },
    []
  );

  /** Resample data for target timeframe, using cache if available */
  const switchTimeframe = useCallback(
    (targetTf: string) => {
      if (!detail) return;

      // Check cache first
      if (tfCache[targetTf]) {
        setActiveTf(targetTf);
        return;
      }

      // Resample synchronously (fast for typical dataset sizes < 10k bars)
      setTfLoading(true);
      // Use requestAnimationFrame to show loading spinner for at least one frame
      requestAnimationFrame(() => {
        const ohlcv = resampleOHLCV(detail.ohlcv_data, detail.timeframe, targetTf);
        const indicators = computeIndicators(ohlcv, detail.indicator_data);
        setTfCache((prev) => ({ ...prev, [targetTf]: { ohlcv, indicators } }));
        setActiveTf(targetTf);
        setTfLoading(false);
      });
    },
    [detail, tfCache]
  );

  // Initialize active timeframe from detail
  useEffect(() => {
    if (detail && !activeTf) {
      setActiveTf(detail.timeframe);
      // Pre-cache the base timeframe
      setTfCache((prev) => ({
        ...prev,
        [detail.timeframe]: {
          ohlcv: detail.ohlcv_data,
          indicators: detail.indicator_data,
        },
      }));
    }
  }, [detail, activeTf]);

  /** Current OHLCV + indicators for the active timeframe (windowed + pre data) */
  const currentData = useMemo((): { ohlcv: OHLCVPoint[]; indicators: IndicatorData } => {
    if (!activeTf || !detail) return { ohlcv: [], indicators: {} };
    // Base timeframe: windowed view into full data + preOhlcv
    if (activeTf === detail.timeframe) {
      const allBars = detail.ohlcv_data ?? [];
      const allIndicators = detail.indicator_data ?? {};
      const total = allBars.length;

      if (total <= INITIAL_WINDOW && preOhlcv.length === 0 && postOhlcv.length === 0) {
        return { ohlcv: allBars, indicators: allIndicators };
      }

      const start = windowStart;
      const end = Math.min(windowEnd, total);
      // Ensure ascending so preOhlcv (asc) + slicedBars + postOhlcv are monotonic
      const slicedBars = ensureAscending(allBars.slice(start, end));
      const ohlcv = [...preOhlcv, ...slicedBars, ...postOhlcv];

      // Pad indicator arrays: preOhlcv → null, windowed indicators, postOhlcv → null
      const prePad = preOhlcv.length;
      const postPad = postOhlcv.length;
      const slicedIndicators = sliceIndicators(allIndicators, start, end);
      let indicators = padIndicators(slicedIndicators, prePad);
      indicators = padIndicatorsPost(indicators, postPad);

      return { ohlcv, indicators };
    }
    // Resampled timeframe: use cache
    return tfCache[activeTf] ?? { ohlcv: [], indicators: {} };
  }, [activeTf, detail, tfCache, windowStart, windowEnd, sliceIndicators, preOhlcv, postOhlcv]);

  // Determine which timeframes to show (only >= base timeframe)
  const availableTimeframes = useMemo(() => {
    if (!detail) return RESAMPLE_TARGETS;
    const baseMin = timeframeToMinutes(detail.timeframe);
    return RESAMPLE_TARGETS.filter((tf) => timeframeToMinutes(tf) >= baseMin);
  }, [detail]);

  useEffect(() => {
    backtestApi
      .getFullDetail(id)
      .then(setDetail)
      .catch(() => setDetail(null))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    if (!detail) return;
    // Fetch all trades for markers (large page_size)
    backtestApi.getTrades(id, { page: 1, page_size: 10000 }).then((res) => {
      setTrades(res.results);
    });
  }, [detail, id]);

  // Fetch exchange accounts when deploy modal opens
  useEffect(() => {
    if (!deployOpen) return;
    exchangeApi.getAccounts().then(setAccounts);
  }, [deployOpen]);

  // 部署弹窗打开时拉该策略**声明**的参数表（可覆盖的键）。
  // 没有这一步，像 volume_filter 这种默认关闭的键就没有任何入口能打开——
  // 2026-10-12 SOL/USDT 的量能门全程关闭就是这么来的。
  useEffect(() => {
    if (!deployOpen) return;
    const name = detail?.strategy_name;
    setDeployParams({});
    setDeploySchema([]);
    setDeploySchemaError(null);
    if (!name) {
      // 回测没记策略名（strategy_name 可空）：不猜，如实说没有可覆盖的表
      setDeploySchemaError("该回测未记录策略名，无法列出可覆盖参数");
      return;
    }
    let cancelled = false;
    tradingApi
      .getStrategyParams(name)
      .then((res) => {
        if (cancelled) return;
        if (!res.resolved) {
          setDeploySchemaError(`解析不到策略 ${name} 的实现类，无法列出参数`);
          return;
        }
        setDeploySchema(res.params);
      })
      .catch((e) => {
        if (cancelled) return;
        setDeploySchemaError(
          e instanceof Error ? e.message : "加载策略参数失败"
        );
      });
    return () => {
      cancelled = true;
    };
  }, [deployOpen, detail?.strategy_name]);

  const handleReview = async (approved: boolean) => {
    setReviewLoading(true);
    try {
      await backtestApi.review(id, { approved, notes: reviewNotes });
      setDetail(await backtestApi.getFullDetail(id));
      setReviewNotes("");
    } finally {
      setReviewLoading(false);
    }
  };

  const handleRerun = async () => {
    setRerunLoading(true);
    setRerunMessage(null);
    try {
      const result = await backtestApi.rerun(id);
      setRerunMessage(result.message);
      // Refresh detail after a short delay to allow Celery to process
      setTimeout(async () => {
        setDetail(await backtestApi.getFullDetail(id));
      }, 3000);
    } catch (e) {
      setRerunMessage(e instanceof Error ? e.message : "重新回测失败");
    } finally {
      setRerunLoading(false);
    }
  };

  const handleDeploy = async () => {
    setDeployLoading(true);
    setDeployError(null);
    setDeploySuccess(null);
    try {
      if (!deployAccountId) {
        setDeployError("请选择交易所账户");
        return;
      }
      const result = await liveSessionApi.create({
        backtest_result_id: id,
        mode: deployMode,
        exchange_account_id: deployAccountId,
        // 只发用户**显式改过**的键：没碰过的键不进 config，会话走策略声明默认值。
        // 后端会把它覆盖在回测参数之上（`{**backtest.parameters, **config}`）。
        ...(Object.keys(deployParams).length > 0
          ? { config: deployParams }
          : {}),
      });
      setDeploySuccess(`会话已创建 (${deployMode === "paper" ? "模拟" : "实盘"} 模式)`);
      setTimeout(() => setDeployOpen(false), 1500);
    } catch (e) {
      setDeployError(e instanceof Error ? e.message : "创建失败");
    } finally {
      setDeployLoading(false);
    }
  };

  if (loading) {
    return (
      <DashboardShell>
        <div className="text-xs text-text3 py-8 text-center">加载中...</div>
      </DashboardShell>
    );
  }

  if (!detail) {
    return (
      <DashboardShell>
        <div className="text-xs text-text3 py-8 text-center">
          回测记录不存在
          <Link href="/backtest" className="text-green hover:underline ml-2">
            返回列表
          </Link>
        </div>
      </DashboardShell>
    );
  }

  const fmtDate = (d: string) =>
    new Date(d).toLocaleDateString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });

  const fmtNum = (v: number | null | undefined, decimals = 2) =>
    v != null ? v.toFixed(decimals) : "—";

  const tabs: { key: TabKey; label: string }[] = [
    { key: "kline", label: "K线图" },
    { key: "equity", label: "权益曲线" },
    { key: "drawdown", label: "回撤曲线" },
    { key: "trades", label: "交易日志" },
  ];

  return (
    <DashboardShell>
      {/* Back + Header */}
      <div className="mb-4">
        <Link href="/backtest" className="text-xs text-text3 hover:text-text mb-2 inline-block cursor-pointer">
          ← 返回回测列表
        </Link>
        <h1 className="text-lg font-bold text-text">
          {detail.symbol} · {detail.timeframe}
        </h1>
        <p className="text-xs text-text3 mt-0.5">
          {fmtDate(detail.start_date)} ~ {fmtDate(detail.end_date)}
        </p>
      </div>

      {/* Metrics Cards - Basic */}
      <div className="grid grid-cols-6 gap-3 mb-3">
        {[
          { label: "初始资金", value: Number(detail.initial_capital).toLocaleString() },
          { label: "最终资金", value: Number(detail.final_capital).toLocaleString() },
          {
            label: "总收益率",
            value: `${detail.total_return_pct >= 0 ? "+" : ""}${detail.total_return_pct.toFixed(2)}%`,
            color: detail.total_return_pct >= 0 ? "text-green" : "text-red",
          },
          { label: "夏普比率", value: fmtNum(detail.sharpe_ratio) },
          {
            label: "最大回撤",
            value: fmtNum(detail.max_drawdown_pct) + "%",
            color: "text-red",
          },
          {
            label: "胜率",
            value: detail.win_rate != null ? `${(detail.win_rate * 100).toFixed(1)}%` : "—",
          },
        ].map((m, i) => (
          <div
            key={i}
            className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-3"
          >
            <div className="text-[10px] text-text3 uppercase tracking-wider mb-1">
              {m.label}
            </div>
            <div className={`text-sm font-mono font-bold ${m.color ?? "text-text"}`}>
              {m.value}
            </div>
          </div>
        ))}
      </div>

      {/* Metrics Cards - Advanced */}
      {detail.metrics && Object.keys(detail.metrics).length > 0 && (
        <div className="grid grid-cols-6 gap-3 mb-4">
          {[
            { label: "Sortino", value: fmtNum(detail.metrics.sortino_ratio), cls: "text-text" },
            { label: "Calmar", value: fmtNum(detail.metrics.calmar_ratio), cls: "text-text" },
            { label: "Profit Factor", value: fmtNum(detail.metrics.profit_factor), cls: "text-text" },
            { label: "年化收益", value: fmtNum(detail.metrics.annualized_return_pct) + "%", cls: "text-text" },
            { label: "平均持仓棒数", value: fmtNum(detail.metrics.avg_trade_duration_bars), cls: "text-text" },
            { label: "最大连胜", value: fmtNum(detail.metrics.max_consecutive_losses), cls: "text-text" },
          ]
            .filter((m) => m.value !== "—")
            .map((m, i) => (
              <div
                key={i}
                className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-3"
              >
                <div className="text-[10px] text-text3 uppercase tracking-wider mb-1">
                  {m.label}
                </div>
                <div className={`text-sm font-mono font-bold ${m.cls}`}>
                  {m.value}
                </div>
              </div>
            ))}
        </div>
      )}

      {/* 资金效率（spec #30 用户故事 12，子票 #35 写 / #36 展示）
        *
        * 三段降级，缺一不可：
        * ① `detail.metrics` 整个没有 / 是空的 → 外层的 `Object.keys(...).length > 0` 挡住；
        * ② `metrics` 有别的键但**没有** `capital_efficiency`（**存量回测就是这样**，
        *    键压根不存在，不是 null 也不是 `{}`）→ 下面这层的 `ce` 守卫挡住，
        *    此时整块不渲染，与改动前的页面完全一致；
        * ③ 有这一段但数值是 `null`（`invalid_capital` / `empty_window` / `no_trades`）
        *    → 逐项渲染成「—」。金额是 8 位定点**字符串**、百分比是 float 的**百分点**，
        *    两种类型分开格式化。
        *
        * `notes`（口径边界：notional 取的是哪个本金口径、以及"它不是什么"）**必须显示**。
        * 藏起来会让这个数读起来比它本身更大——那是本仓 `caveats` 纪律专门要防的一类误读
        * （见 `session_stats._CAVEATS` 的注释：口径写在前端，将来谁改了取数没有一个地方会变红）。 */}
      {detail.metrics && Object.keys(detail.metrics).length > 0 && (() => {
        const ce = detail.metrics.capital_efficiency;
        if (!ce) return null;
        // 格式化与降级判定都在 `capitalEfficiencyCards`（纯函数，有用例）：金额是 8 位
        // 定点字符串、百分比是**百分点** float，两种口径不能混。这里只管渲染。
        const cards = capitalEfficiencyCards(ce);
        return (
          <div className="mb-4">
            {cards.length > 0 && (
              <div className="grid grid-cols-6 gap-3">
                {cards.map((m, i) => (
                  <div
                    key={i}
                    className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-3"
                  >
                    <div className="text-[10px] text-text3 uppercase tracking-wider mb-1">
                      {m.label}
                    </div>
                    <div className={`text-sm font-mono font-bold ${m.cls}`}>
                      {m.value}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {/* 六个指标可能整段判不了（都是 null，上面被 filter 掉），那时至少要说清原因。 */}
            {ce.computed === false && (
              <p className="text-[10px] text-text3 mt-2">
                资金效率判不了{ce.reason ? `（${ce.reason}）` : ""}，不是「占用为 0」。
              </p>
            )}
            {ce.notes && ce.notes.length > 0 && (
              <ul className="mt-2 space-y-0.5">
                {ce.notes.map((n: string) => (
                  <li key={n} className="text-[10px] text-text3 leading-relaxed">
                    · {n}
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })()}

      {/* Review & Deploy Section */}
      {detail && (
        <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-3 mb-4">
          {/* Top row: status badge left, buttons right */}
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              <span className="text-[10px] text-text3 uppercase tracking-wider font-semibold">审核状态</span>
              <span
                className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                  detail.review_status === "approved"
                    ? "bg-green-dim text-green"
                    : detail.review_status === "rejected"
                      ? "bg-red-dim text-red"
                      : "bg-amber-dim text-amber"
                }`}
              >
                {detail.review_status === "approved" ? "已通过" : detail.review_status === "rejected" ? "已拒绝" : "待审核"}
              </span>
              {detail.reviewed_at && (
                <span className="text-[10px] text-text3">{fmtDate(detail.reviewed_at)}</span>
              )}
            </div>

            {/* Right side: action buttons */}
            <div className="flex items-center gap-2">
              {/* Rerun button: always visible */}
              <button
                onClick={handleRerun}
                disabled={rerunLoading}
                className="px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer border border-[rgba(255,255,255,0.1)] text-text3 hover:text-text hover:border-[rgba(255,255,255,0.2)] disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {rerunLoading ? "回测中..." : "重新回测"}
              </button>

              {/* Pending: review buttons */}
              {detail.review_status === "pending" && (
                <>
                  <button
                    onClick={() => handleReview(true)}
                    disabled={reviewLoading}
                    className="px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer border border-green/20 bg-green-dim text-green hover:bg-green-dim/80 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {reviewLoading ? "审核中..." : "通过审核"}
                  </button>
                  <button
                    onClick={() => handleReview(false)}
                    disabled={reviewLoading}
                    className="px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer border border-red/20 bg-red-dim text-red hover:bg-red-dim/80 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {reviewLoading ? "审核中..." : "拒绝审核"}
                  </button>
                </>
              )}

              {/* Approved: deploy button */}
              {detail.review_status === "approved" && (
                <button
                  onClick={() => {
                    setDeployOpen(true);
                    setDeployError(null);
                    setDeploySuccess(null);
                  }}
                  className="px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer border border-green/20 bg-green-dim text-green hover:bg-green-dim/80"
                >
                  部署策略
                </button>
              )}
            </div>
          </div>

          {/* Notes display for rejected/approved */}
          {detail.review_notes && detail.review_status !== "pending" && (
            <div className="text-[10px] text-text3 mb-2">备注：{detail.review_notes}</div>
          )}

          {/* Pending: show review notes textarea */}
          {detail.review_status === "pending" && (
            <textarea
              value={reviewNotes}
              onChange={(e) => setReviewNotes(e.target.value)}
              placeholder="审核备注（可选）"
              className="bg-bg2 border border-[rgba(255,255,255,0.1)] rounded-lg px-3 py-2 text-xs text-text placeholder:text-text3 focus:outline-none focus:border-green/30 resize-none"
              rows={2}
            />
          )}

          {/* Rerun message */}
          {rerunMessage && (
            <div className="mt-2 text-[10px] text-green">{rerunMessage}</div>
          )}
        </div>
      )}

      {/* Deploy Modal */}
      {deployOpen && detail && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-bg1 border border-[rgba(255,255,255,0.1)] rounded-xl w-full max-w-md p-6">
            <h3 className="text-sm font-bold mb-4">部署策略</h3>
            <p className="text-xs text-text3 mb-4">
              {detail.symbol} · {detail.timeframe} · {detail.strategy_name ?? "未命名策略"}
            </p>

            {/* Mode selection */}
            <div className="mb-4">
              <label className="text-[10px] text-text3 uppercase tracking-wider font-semibold block mb-2">
                运行模式
              </label>
              <div className="flex gap-2">
                <button
                  onClick={() => setDeployMode("paper")}
                  className={`flex-1 py-2 rounded-lg text-xs font-semibold border cursor-pointer transition-all ${
                    deployMode === "paper"
                      ? "border-blue/30 bg-blue-dim text-blue"
                      : "border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
                  }`}
                >
                  模拟交易
                </button>
                <button
                  onClick={() => setDeployMode("live")}
                  className={`flex-1 py-2 rounded-lg text-xs font-semibold border cursor-pointer transition-all ${
                    deployMode === "live"
                      ? "border-green/30 bg-green-dim text-green"
                      : "border-[rgba(255,255,255,0.07)] text-text3 hover:text-text"
                  }`}
                >
                  实盘交易
                </button>
              </div>
            </div>

            {/* Exchange account selection */}
            <div className="mb-4">
              <label className="text-[10px] text-text3 uppercase tracking-wider font-semibold block mb-2">
                交易所账户
              </label>
              <select
                value={deployAccountId}
                onChange={(e) => setDeployAccountId(e.target.value)}
                className="w-full bg-bg2 border border-[rgba(255,255,255,0.1)] rounded-lg px-3 py-2 text-xs text-text focus:outline-none focus:border-green/30"
              >
                <option value="">请选择...</option>
                {accounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.exchange} · {acc.label || acc.id.slice(0, 8)} {acc.testnet ? "(模拟)" : ""}
                  </option>
                ))}
              </select>
            </div>

            {/* 运行参数覆盖：只列策略**声明**的键。
                不改的键不发出去，会话走策略默认值——与后端
                `{**backtest.parameters, **config}` 的继承语义一致。
                没有这块，像 volume_filter 这种默认关闭的键就没入口打开。 */}
            <div className="mb-4">
              <label className="text-[10px] text-text3 uppercase tracking-wider font-semibold block mb-2">
                运行参数（可覆盖）
              </label>
              {deploySchemaError ? (
                <div className="text-[11px] text-text3 bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg px-3 py-2">
                  {deploySchemaError}
                </div>
              ) : deploySchema.length === 0 ? (
                <div className="text-[11px] text-text3">该策略没有声明任何参数。</div>
              ) : (
                <div className="max-h-56 overflow-y-auto bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg divide-y divide-[rgba(255,255,255,0.04)]">
                  {deploySchema.map((spec) => {
                    const overridden = Object.prototype.hasOwnProperty.call(
                      deployParams,
                      spec.key
                    );
                    return (
                      <div key={spec.key} className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-[11px] text-text2 flex-1">
                            {spec.key}
                          </span>
                          <span className="text-[9px] text-text3">
                            默认 {String(spec.default)}
                          </span>
                        </div>
                        <div className="flex items-center gap-2 mt-1">
                          {spec.type === "bool" ? (
                            <label className="flex items-center gap-1.5 text-[11px] text-text2 cursor-pointer">
                              <input
                                type="checkbox"
                                checked={
                                  overridden
                                    ? Boolean(deployParams[spec.key])
                                    : Boolean(spec.default)
                                }
                                onChange={(e) =>
                                  setDeployParams((p) => ({
                                    ...p,
                                    [spec.key]: e.target.checked,
                                  }))
                                }
                                className="accent-green"
                              />
                              启用
                            </label>
                          ) : (
                            <input
                              type="number"
                              value={
                                overridden
                                  ? String(deployParams[spec.key] ?? "")
                                  : ""
                              }
                              placeholder={`留空 = 默认 ${String(spec.default)}`}
                              min={spec.min ?? undefined}
                              max={spec.max ?? undefined}
                              step="any"
                              onChange={(e) =>
                                setDeployParams((p) => {
                                  const next = { ...p };
                                  if (e.target.value === "") {
                                    delete next[spec.key];
                                  } else {
                                    next[spec.key] = Number(e.target.value);
                                  }
                                  return next;
                                })
                              }
                              className="flex-1 bg-bg1 border border-[rgba(255,255,255,0.1)] rounded px-2 py-1 text-[11px] text-text focus:outline-none focus:border-green/30"
                            />
                          )}
                          {overridden && (
                            <button
                              onClick={() =>
                                setDeployParams((p) => {
                                  const next = { ...p };
                                  delete next[spec.key];
                                  return next;
                                })
                              }
                              className="text-[10px] text-amber hover:underline cursor-pointer shrink-0"
                            >
                              改过 · 重置
                            </button>
                          )}
                        </div>
                        {spec.description && (
                          <div className="text-[10px] text-text3 mt-1 leading-snug">
                            {spec.description}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Error / Success */}
            {deployError && (
              <div className="bg-red-dim/20 border border-red/30 rounded-lg px-3 py-2 mb-4 text-xs text-red">
                {deployError}
              </div>
            )}
            {deploySuccess && (
              <div className="bg-green-dim/20 border border-green/30 rounded-lg px-3 py-2 mb-4 text-xs text-green">
                {deploySuccess}
              </div>
            )}

            {/* Actions */}
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setDeployOpen(false)}
                disabled={deployLoading}
                className="px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer border border-[rgba(255,255,255,0.1)] text-text3 hover:text-text disabled:opacity-40"
              >
                取消
              </button>
              <button
                onClick={handleDeploy}
                disabled={deployLoading}
                className="px-4 py-1.5 rounded-md text-xs font-semibold cursor-pointer border border-green/20 bg-green-dim text-green hover:bg-green-dim/80 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {deployLoading ? "创建中..." : "确认部署"}
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="flex gap-1 mb-3">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-1.5 text-xs font-medium rounded-lg cursor-pointer transition-all border ${
              activeTab === tab.key
                ? "text-green bg-green-dim border-green/20"
                : "text-text2 border-transparent hover:text-text hover:bg-bg2"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Chart / Log Area */}
      <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-4">
        {activeTab === "kline" && detail && (
          <>
            {tfLoading && activeTf && (
              <div className="text-xs text-text3 py-4 text-center animate-pulse">
                聚合 {activeTf} K 线数据...
              </div>
            )}
            {!tfLoading && (
              <TView
                ohlcv={currentData.ohlcv}
                indicators={currentData.indicators}
                trades={trades}
                timeframe={activeTf ?? undefined}
                availableTimeframes={[...availableTimeframes]}
                onTimeframeChange={switchTimeframe}
                totalBars={preOhlcv.length + detail.ohlcv_data.length + postOhlcv.length}
                loadedStart={preOhlcv.length + windowStart}
                hasMoreEarlier={hasMoreEarlierData}
                hasMoreLater={hasMoreLaterData}
                onLoadMore={activeTf === detail.timeframe ? loadMoreOhlcv : undefined}
              />
            )}
          </>
        )}
        {activeTab === "equity" && (
          <EquityChart data={detail.equity_curve} showBenchmark />
        )}
        {activeTab === "drawdown" && (
          <DrawdownChart data={detail.drawdown_curve} />
        )}
        {activeTab === "trades" && <TradeLog backtestId={id} />}
      </div>

      {/* Parameters */}
      {detail.parameters && Object.keys(detail.parameters).length > 0 && (
        <div className="mt-4 bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-4 py-3">
          <div className="text-[10px] text-text3 uppercase tracking-wider mb-2">
            策略参数
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(detail.parameters).map(([k, v]) => (
              <span
                key={k}
                className="text-xs font-mono bg-bg2 border border-[rgba(255,255,255,0.07)] rounded px-2 py-1 text-text2"
              >
                {k} = {String(v)}
              </span>
            ))}
          </div>
        </div>
      )}
    </DashboardShell>
  );
}
