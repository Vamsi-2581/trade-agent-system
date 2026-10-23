"use client";

/**
 * 机制读面的首屏（`GET /api/regime/overview/`），**60 秒轮询**。
 *
 * 形状照 `useOrders` 的 `{data, loading, error, refetch}`。两处与它不同，都是有理由的：
 *
 *   1. **`data` 初值是 `null` 而不是空对象**：`RegimeOverview` 里没有一个是「空着也说得
 *      通」的东西（三个开关 + 当前代 + 当前判定）。拿一个 {} 当兜底，页面会在首帧把
 *      「还没有数据」画成「三个开关都没有」，而后者是一个会误导人的结论。
 *   2. **`refetch` 就是那一次请求本身**（`useCallback` 引用稳定）：翻完一个开关、或者
 *      跑完一轮池化重算之后要立刻看到新档位，而不是等下一个 60 秒。引用稳定还让页面的
 *      `useEffect` 不必把它塞进依赖里再比一遍。
 *
 * 轮询失败**不清空 `data`**：上一秒还是真的东西，没有理由因为一次超时就把它抹掉；错误
 * 走 `error` 显示成一条内联提示，页面仍然读得到旧值（「数据截止」那一行正因此才要常驻）。
 */
import { useCallback, useEffect, useState } from "react";
import { regimeApi, type RegimeOverview } from "@/lib/api";

export const REGIME_OVERVIEW_POLL_MS = 60_000;

export function useRegimeOverview(pollingInterval = REGIME_OVERVIEW_POLL_MS) {
  const [data, setData] = useState<RegimeOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchOverview = useCallback(async () => {
    try {
      const next = await regimeApi.getOverview();
      setData(next);
      setError(null);
    } catch (caught) {
      if (caught instanceof Error) setError(caught.message);
      else setError(String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchOverview();
    const timer = setInterval(() => {
      void fetchOverview();
    }, pollingInterval);
    return () => clearInterval(timer);
  }, [fetchOverview, pollingInterval]);

  return { data, loading, error, refetch: fetchOverview };
}
