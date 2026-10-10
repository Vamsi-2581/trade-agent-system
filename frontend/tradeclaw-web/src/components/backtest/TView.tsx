"use client";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type UTCTimestamp,
  type Time,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
} from "lightweight-charts";
import type { OHLCVPoint, IndicatorData, BacktestTrade } from "@/lib/api";
import { HoverInfo } from "./HoverInfo";
import type { SeriesMarkerPosition, SeriesMarkerShape } from "lightweight-charts";

// ─── Types ───────────────────────────────────────────────────────────────────

interface TViewProps {
  ohlcv: OHLCVPoint[];
  indicators?: IndicatorData;
  trades?: BacktestTrade[];
  /** 实盘历史订单标记（交易记录详情页） */
  orderMarkers?: TViewOrderMarker[];
  /** K 线加载中（优先于"暂无 K 线数据"占位） */
  loading?: boolean;
  /** 水平价格线（如成交均价 / 委托价） */
  priceLine?: { price: number; title: string; color?: string } | null;
  timeframe?: string;
  availableTimeframes?: string[];
  onTimeframeChange?: (tf: string) => void;
  totalBars?: number;
  /** First bar index (in the full dataset) of the currently loaded ohlcv data */
  loadedStart?: number;
  /** Whether more earlier bars may be available from the server */
  hasMoreEarlier?: boolean;
  /** Whether more later bars may be available from the server */
  hasMoreLater?: boolean;
  onLoadMore?: (direction: "earlier" | "later") => void;
}

/** 实盘订单标记（由 Order 转换而来） */
export interface TViewOrderMarker {
  time: string;
  side: "buy" | "sell";
  price: number | null;
  quantity: string;
  filled: boolean;
  /** 当前查看的订单，用醒目颜色高亮 */
  highlighted?: boolean;
}

/** Convert ISO timestamp to UTCTimestamp (seconds).
 *
 *  后端返回的 K 线时间戳是「无时区后缀的 UTC 字符串」（如 "2026-09-14T02:15:00"，见
 *  backend/apps/backtest/tasks.py 的 datetime.fromtimestamp(...).isoformat()），
 *  而订单 created_at/updated_at 是带 Z 的 aware UTC 串。
 *  JS 会把无时区串按「浏览器本地时区」解析（CST=+8h），导致 K 线与订单标记
 *  相差一个时区偏移、标记被画到 8 小时之后。
 *  这里统一按 UTC 解析：两者落到同一时间基准，再由 localization.timeFormatter
 *  转换到系统时区显示。
 */
function toUTCTime(ts: string): UTCTimestamp {
  if (!ts) return 0 as UTCTimestamp;
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(ts);
  const ms = new Date(hasTimezone ? ts : `${ts}Z`).getTime();
  return Math.floor((Number.isFinite(ms) ? ms : 0) / 1000) as UTCTimestamp;
}

/** Ensure OHLCV data is ascending (oldest-first).
 *  Deduplication is handled upstream in currentData to keep indicators in sync.
 */
function ensureAscending(data: OHLCVPoint[]): OHLCVPoint[] {
  if (data.length < 2) return data;
  const firstTs = toUTCTime(data[0].timestamp);
  const lastTs = toUTCTime(data[data.length - 1].timestamp);
  return firstTs > lastTs ? data.toReversed() : data;
}

/** Binary search for closest data point by UTC timestamp */
function binarySearchTime(data: OHLCVPoint[], targetTs: UTCTimestamp): OHLCVPoint | null {
  if (!data.length) return null;
  let left = 0;
  let right = data.length - 1;
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    if (toUTCTime(data[mid].timestamp) < targetTs) {
      left = mid + 1;
    } else {
      right = mid;
    }
  }
  if (left > 0) {
    const diffLeft = Math.abs(toUTCTime(data[left].timestamp) - targetTs);
    const diffPrev = Math.abs(toUTCTime(data[left - 1].timestamp) - targetTs);
    return diffPrev < diffLeft ? data[left - 1] : data[left];
  }
  return data[left];
}

/** Reverse all indicator arrays to match reversed OHLCV */
function reverseIndicators(data: IndicatorData): IndicatorData {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(data)) {
    if (Array.isArray(value)) {
      result[key] = value.toReversed();
    } else if (typeof value === "object" && value !== null) {
      const obj: Record<string, unknown> = {};
      for (const [k2, v2] of Object.entries(value)) {
        obj[k2] = Array.isArray(v2) ? v2.toReversed() : v2;
      }
      result[key] = obj;
    }
  }
  return result as IndicatorData;
}

/** Build trade markers for both entry and exit points */
function buildTradeMarkers(
  trades: BacktestTrade[],
  ohlcv: OHLCVPoint[]
): SeriesMarker<Time>[] {
  if (!ohlcv.length || !trades.length) return [];

  const times = ohlcv.map((p) => toUTCTime(p.timestamp));
  const minTime = times[0];
  const maxTime = times[times.length - 1];

  // Find closest OHLCV bar for a given timestamp
  const findClosestTime = (targetTs: UTCTimestamp): Time => {
    let minDist = Infinity;
    let closestIdx = 0;
    for (let i = 0; i < times.length; i++) {
      const dist = Math.abs(times[i] - targetTs);
      if (dist < minDist) {
        minDist = dist;
        closestIdx = i;
      }
    }
    return times[closestIdx];
  };

  const markers: SeriesMarker<Time>[] = [];
  const seenEntry = new Set<string>();
  const seenExit = new Set<string>();

  for (const t of trades) {
    const isLong = t.side === "long";
    const isEntry = t.trade_type === "open" || t.trade_type === "add";

    // Entry marker: only for open/add trades, within OHLCV time range
    if (isEntry) {
      const entryTime = toUTCTime(t.entry_time);
      if (entryTime >= minTime && entryTime <= maxTime) {
        const entryKey = `${t.id}|entry`;
        if (!seenEntry.has(entryKey)) {
          seenEntry.add(entryKey);
          markers.push({
            time: findClosestTime(entryTime),
            position: (isLong ? "belowBar" : "aboveBar") as SeriesMarkerPosition,
            color: isLong ? "#00e676" : "#ff5252",
            shape: (isLong ? "arrowUp" : "arrowDown") as SeriesMarkerShape,
            text: `入场 ${isLong ? "多" : "空"}`,
          });
        }
      }
    }

    // Exit marker: only if within OHLCV time range
    if (t.exit_time) {
      const exitTime = toUTCTime(t.exit_time);
      if (exitTime >= minTime && exitTime <= maxTime) {
        const exitKey = `${t.id}|exit`;
        if (!seenExit.has(exitKey)) {
          seenExit.add(exitKey);
          const pnl = t.pnl ? Number(t.pnl) : 0;
          markers.push({
            time: findClosestTime(exitTime),
            position: (isLong ? "aboveBar" : "belowBar") as SeriesMarkerPosition,
            color: "#ff5252", // 出场标示显示为红色
            shape: (isLong ? "arrowDown" : "arrowUp") as SeriesMarkerShape,
            text: pnl >= 0 ? `出场 +${pnl.toFixed(0)}` : `出场 ${pnl.toFixed(0)}`,
          });
        }
      }
    }
  }

  // Sort by time
  return markers.sort((a, b) => {
    const ta = a.time as number;
    const tb = b.time as number;
    return ta - tb;
  });
}

/** Build markers for live (real) orders — buy below bar / sell above bar */
function buildOrderMarkers(
  orders: TViewOrderMarker[],
  ohlcv: OHLCVPoint[]
): SeriesMarker<Time>[] {
  if (!ohlcv.length || !orders.length) return [];

  const times = ohlcv.map((p) => toUTCTime(p.timestamp));
  const minTime = times[0];
  const maxTime = times[times.length - 1];

  const findClosestTime = (targetTs: UTCTimestamp): Time => {
    let minDist = Infinity;
    let closestIdx = 0;
    for (let i = 0; i < times.length; i++) {
      const dist = Math.abs(times[i] - targetTs);
      if (dist < minDist) {
        minDist = dist;
        closestIdx = i;
      }
    }
    return times[closestIdx];
  };

  const markers: SeriesMarker<Time>[] = [];
  for (const o of orders) {
    const ts = toUTCTime(o.time);
    if (ts < minTime || ts > maxTime) continue;
    const isBuy = o.side === "buy";
    markers.push({
      time: findClosestTime(ts),
      position: (isBuy ? "belowBar" : "aboveBar") as SeriesMarkerPosition,
      color: o.highlighted ? "#ffd600" : isBuy ? "#00e676" : "#ff5252",
      shape: (isBuy ? "arrowUp" : "arrowDown") as SeriesMarkerShape,
      text: o.highlighted
        ? `当前 ${isBuy ? "买" : "卖"} ${o.quantity}`
        : `${isBuy ? "买" : "卖"} ${o.quantity}`,
    });
  }
  return markers.sort((a, b) => {
    const ta = a.time as number;
    const tb = b.time as number;
    return ta - tb;
  });
}

// ─── Component ───────────────────────────────────────────────────────────────

export default function TView({
  ohlcv,
  indicators,
  trades,
  orderMarkers,
  loading = false,
  priceLine,
  timeframe,
  availableTimeframes = ["1m", "5m", "15m", "1h", "4h", "1d"],
  onTimeframeChange,
  totalBars: totalBarsProp,
  loadedStart: loadedStartProp = 0,
  hasMoreEarlier = false,
  hasMoreLater = false,
  onLoadMore,
}: TViewProps) {
  const chartContainerRef = useRef<HTMLDivElement | null>(null);
  const chartWrapperRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  // Prevent concurrent lazy-load requests
  const isLoadingRef = useRef(false);
  const normalizedDataRef = useRef<OHLCVPoint[]>([]);
  const hoverInfoRef = useRef<any>(null);

  const totalBars = totalBarsProp ?? ohlcv.length;

  const [isFullscreen, setIsFullscreen] = useState(false);

  const timeframeRef = useRef(timeframe);
  const visibleRangeRef = useRef<{ from: Time; to: Time } | null>(null);
  const ohlcvRef = useRef(ohlcv);
  const onLoadMoreRef = useRef(onLoadMore);
  const totalBarsRef = useRef(totalBars);
  const loadedStartRef = useRef(loadedStartProp);
  const hasMoreEarlierRef = useRef(hasMoreEarlier);
  const hasMoreLaterRef = useRef(hasMoreLater);

  useEffect(() => {
    timeframeRef.current = timeframe;
  }, [timeframe]);
  useEffect(() => {
    ohlcvRef.current = ohlcv;
  }, [ohlcv]);
  useEffect(() => {
    onLoadMoreRef.current = onLoadMore;
  }, [onLoadMore]);
  useEffect(() => {
    totalBarsRef.current = totalBars;
  }, [totalBars]);
  useEffect(() => {
    loadedStartRef.current = loadedStartProp;
  }, [loadedStartProp]);
  useEffect(() => {
    hasMoreEarlierRef.current = hasMoreEarlier;
  }, [hasMoreEarlier]);
  useEffect(() => {
    hasMoreLaterRef.current = hasMoreLater;
  }, [hasMoreLater]);

  // ── Crosshair handler for HoverInfo ───────────────────────────────────

  const handleCrosshairMove = useCallback((param: any) => {
    if (!param || !param.time || !hoverInfoRef.current) {
      hoverInfoRef.current?.setHoverData(null);
      return;
    }
    const d = normalizedDataRef.current;
    if (!d.length) {
      hoverInfoRef.current.setHoverData(null);
      return;
    }
    const targetTs = typeof param.time === "number"
      ? (param.time as UTCTimestamp)
      : toUTCTime(param.time);
    const closest = binarySearchTime(d, targetTs);
    if (!closest) {
      hoverInfoRef.current.setHoverData(null);
      return;
    }
    const change = closest.close - closest.open;
    const changePercent = (change / closest.open) * 100;
    hoverInfoRef.current.setHoverData({
      timestamp: toUTCTime(closest.timestamp),
      open: closest.open,
      high: closest.high,
      low: closest.low,
      close: closest.close,
      volume: closest.volume,
      change,
      changePercent,
      time: new Date(toUTCTime(closest.timestamp) * 1000).toLocaleString(),
    });
  }, []);

  // ── Create chart ─────────────────────────────────────────────────────────

  useEffect(() => {
    if (!chartContainerRef.current || !ohlcv?.length) return;

    // Reset loading lock on chart rebuild
    isLoadingRef.current = false;

    // Destroy previous chart
    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const container = chartContainerRef.current;
    const width = container.clientWidth;
    const height = isFullscreen
      ? window.innerHeight - 64
      : Math.max(container.clientHeight - 64, 400);

    // Normalize to ascending order for the charting library
    const data = ensureAscending(ohlcv);
    const wasDescending = ohlcv.length >= 2 && toUTCTime(ohlcv[0].timestamp) > toUTCTime(ohlcv[ohlcv.length - 1].timestamp);
    const ind = wasDescending ? reverseIndicators(indicators ?? {}) : indicators;

    const chart = createChart(container, {
      width,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#0a0e27" },
        textColor: "#a0b0d0",
      },
      grid: {
        vertLines: { color: "rgba(0, 212, 255, 0.06)" },
        horzLines: { color: "rgba(0, 212, 255, 0.06)" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: "rgba(0, 212, 255, 0.5)",
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: "rgba(0, 212, 255, 0.8)",
        },
        horzLine: {
          color: "rgba(0, 212, 255, 0.5)",
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: "rgba(0, 212, 255, 0.8)",
        },
      },
      timeScale: {
        borderColor: "rgba(0, 212, 255, 0.2)",
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: {
        borderColor: "rgba(0, 212, 255, 0.2)",
        scaleMargins: { top: 0.1, bottom: 0.2 },
      },
      localization: {
        timeFormatter: (time: number) => {
          const d = new Date(time * 1000);
          const mm = String(d.getMonth() + 1).padStart(2, "0");
          const dd = String(d.getDate()).padStart(2, "0");
          const hh = String(d.getHours()).padStart(2, "0");
          const min = String(d.getMinutes()).padStart(2, "0");
          return `${mm}-${dd} ${hh}:${min}`;
        },
      },
    });

    // ── Main pane: Candlestick + MA lines ──
    const candles = chart.addCandlestickSeries({
      upColor: "#00d4ff",
      downColor: "#ef5350",
      borderUpColor: "#00d4ff",
      borderDownColor: "#ef5350",
      wickUpColor: "#00d4ff",
      wickDownColor: "#ef5350",
    });

    const candleData = data.map((p) => ({
      time: toUTCTime(p.timestamp),
      open: p.open,
      high: p.high,
      low: p.low,
      close: p.close,
    }));
    candles.setData(candleData);

    // Trade markers (backtest trades + live orders, merged & sorted by time)
    const markers = [
      ...buildTradeMarkers(trades ?? [], data),
      ...buildOrderMarkers(orderMarkers ?? [], data),
    ].sort((a, b) => (a.time as number) - (b.time as number));
    if (markers.length) {
      candles.setMarkers(markers);
    }

    // Horizontal price line (e.g. avg fill price / order price)
    if (priceLine && Number.isFinite(priceLine.price) && priceLine.price > 0) {
      candles.createPriceLine({
        price: priceLine.price,
        title: priceLine.title,
        color: priceLine.color || "#ffd600",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
      });
    }

    candleSeriesRef.current = candles;

    // ── HoverInfo plugin ──
    normalizedDataRef.current = data;
    const hoverInfo = new (HoverInfo as any)(chart, candles);
    candles.attachPrimitive(hoverInfo);
    hoverInfoRef.current = hoverInfo;

    // ── Sub-pane 1: Volume ──
    const volSeries = chart.addHistogramSeries({
      priceScaleId: "vol",
      priceFormat: { type: "volume" },
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });
    volSeries.setData(
      data.map((p) => ({
        time: toUTCTime(p.timestamp),
        value: p.volume,
        color: p.close >= p.open ? "rgba(0,230,118,0.3)" : "rgba(255,82,82,0.3)",
      }))
    );
    volumeSeriesRef.current = volSeries;

    // ── Sub-pane 2: MACD or RSI ──
    // (MACD and RSI sub-panes removed)

    // ── Lazy loading on scroll to edge (logical range) ─────────────────────
    // Logical range reports floating-point bar indices into the loaded data,
    // so `from < N` / `to > count - N` directly measures bar-distance from the
    // edge — independent of bar interval and timestamp parsing.

    const handleLogicalRangeChange = (
      logicalRange: { from: number; to: number } | null
    ) => {
      if (!logicalRange) return;

      console.log(
        "[DEBUG-lz1] range=", JSON.stringify(logicalRange),
        "loadedCount=", normalizedDataRef.current.length,
        "loadedStart=", loadedStartRef.current,
        "total=", totalBarsRef.current,
        "moreEarlier=", hasMoreEarlierRef.current,
        "moreLater=", hasMoreLaterRef.current,
        "hasCb=", !!onLoadMoreRef.current,
        "locked=", isLoadingRef.current,
      );

      // Save the current time range so the viewport can be restored after the
      // chart rebuilds (new bars are prepended/appended off-screen).
      const tr = chart.timeScale().getVisibleRange();
      if (tr) visibleRangeRef.current = tr;

      const total = totalBarsRef.current;
      const loadCb = onLoadMoreRef.current;
      if (!total || !loadCb) return;
      if (isLoadingRef.current) return;

      const loadedCount = normalizedDataRef.current.length;
      const moreEarlier = hasMoreEarlierRef.current;

      const moreLater = hasMoreLaterRef.current;

      // Don't block when server may have earlier/later data even if in-memory bars are all loaded
      if (!loadedCount || (loadedCount >= total && !moreEarlier && !moreLater)) return;

      const loadedStart = loadedStartRef.current;
      const loadedEnd = loadedStart + loadedCount;
      const EDGE_BARS = 10; // trigger when within ~10 bars of an edge

      // Left edge: earlier bars exist in-memory (loadedStart>0) or via server (moreEarlier)
      if ((loadedStart > 0 || moreEarlier) && logicalRange.from < EDGE_BARS) {
        isLoadingRef.current = true;
        loadCb("earlier");
        setTimeout(() => {
          isLoadingRef.current = false;
        }, 10000);
        return;
      }

      // Right edge: later bars exist in-memory (loadedEnd<total) or via server (moreLater)
      if ((loadedEnd < total || moreLater) && logicalRange.to > loadedCount - EDGE_BARS) {
        isLoadingRef.current = true;
        loadCb("later");
        setTimeout(() => {
          isLoadingRef.current = false;
        }, 10000);
      }
    };

    chart
      .timeScale()
      .subscribeVisibleLogicalRangeChange(handleLogicalRangeChange);

    // Hover info crosshair subscription
    chart.subscribeCrosshairMove(handleCrosshairMove);

    // Restore visible range after lazy-load, or set initial view
    const prevRange = visibleRangeRef.current;
    if (prevRange) {
      chart.timeScale().setVisibleRange(prevRange);
    } else {
      const total = totalBarsRef.current;
      if (!total || data.length >= total) {
        chart.timeScale().fitContent();
      } else {
        // Show the rightmost bars so the user can scroll both ways
        const visibleBars = Math.min(100, data.length);
        const fromIdx = data.length - visibleBars;
        chart.timeScale().setVisibleRange({
          from: toUTCTime(data[fromIdx].timestamp),
          to: toUTCTime(data[data.length - 1].timestamp),
        });
      }
    }

    // Auto-resize
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        chart.applyOptions({ width: entry.contentRect.width });
      }
    });
    ro.observe(container);

    chartRef.current = chart;

    return () => {
      ro.disconnect();
      try {
        chart.unsubscribeCrosshairMove(handleCrosshairMove);
      } catch {}
      try {
        chart
          .timeScale()
          .unsubscribeVisibleLogicalRangeChange(handleLogicalRangeChange);
      } catch {}
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      hoverInfoRef.current = null;
    };
  }, [ohlcv, indicators, trades, orderMarkers, priceLine, isFullscreen]);

  // ── Fullscreen toggle ────────────────────────────────────────────────────

  const toggleFullscreen = useCallback(async () => {
    if (!document.fullscreenElement) {
      try {
        await chartWrapperRef.current?.requestFullscreen();
        setIsFullscreen(true);
      } catch (err) {
        console.error("Enter fullscreen failed:", err);
      }
    } else {
      try {
        await document.exitFullscreen();
        setIsFullscreen(false);
      } catch (err) {
        console.error("Exit fullscreen failed:", err);
      }
    }
  }, []);

  useEffect(() => {
    const onChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  // ── Render ───────────────────────────────────────────────────────────────

  if (!ohlcv?.length) {
    return (
      <div className="h-40 bg-bg2 rounded-lg animate-pulse flex items-center justify-center text-xs text-text3">
        {loading ? "K 线加载中..." : "暂无 K 线数据"}
      </div>
    );
  }

  return (
    <div
      ref={chartWrapperRef}
      className="relative flex flex-col w-full overflow-hidden bg-[#0a0e27]"
      style={{ height: isFullscreen ? "100vh" : "auto" }}
    >
      {/* Control bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 px-2 py-1 text-[10px]">
        {/* Timeframe switcher */}
        <div className="flex gap-1">
          {availableTimeframes.map((tf) => {
            const isActive = timeframe === tf;
            return (
              <button
                key={tf}
                onClick={() => onTimeframeChange?.(tf)}
                className={`px-2 py-0.5 font-medium rounded cursor-pointer transition-all border ${
                  isActive
                    ? "text-green bg-green-dim border-green/20"
                    : "text-text3 border-transparent hover:text-text hover:bg-bg2"
                }`}
              >
                {tf}
              </button>
            );
          })}
        </div>

        {/* Fullscreen button */}
        <button
          className="px-2 py-0.5 text-[10px] font-medium rounded border border-[#00d4ff]/30 text-[#00d4ff] cursor-pointer hover:bg-[#00d4ff]/10 transition-all"
          onClick={toggleFullscreen}
        >
          {isFullscreen ? "退出全屏" : "全屏"}
        </button>
      </div>

      {/* Chart container */}
      <div ref={chartContainerRef} className="w-full" />
    </div>
  );
}
