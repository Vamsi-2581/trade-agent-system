"use client";

/**
 * 停止层的读面（`GET /api/regime/halt/`），**60 秒轮询**。形状与纪律照 `useRegimeOverview`。
 *
 * 三处与 `useOrders` 不同，都是有理由的：
 *
 *   1. **`data` 初值是 `null` 而不是 `{}`**：这个读面只有一个答案——「此刻到底哪些层在拦」。
 *      拿 `{}` 当兜底，首帧就会把「还没读到」画成「一层都没有」，而后者在交易页上的意思是
 *      **放行**。放行是一个要给人看的结论（用户原话：「当前阶段是否放行需要在交易界面标示
 *      出来」），所以它必须来自一次真正落定的请求，不能来自一个空兜底。
 *   2. **轮询失败不清空 `data`**：上一秒还是真的东西。这一侧读的是安全面——一次超时就把
 *      「正在拦」擦成空白，比留着一份标了截止时刻的旧值危险得多。错误走 `error`，横幅把
 *      错误与旧值并排显示。
 *   3. **`refetch` 用 `useCallback` 保持引用稳定**：页面在别处（翻开关、跑完重算）要能立刻
 *      重拉一次，而不是等下一个 60 秒；引用稳定还让页面的 `useEffect` 不必把它塞进依赖里
 *      再比一遍。
 *
 * 请求序号 `seq` 那道闸与 `useRegimeOverview` 逐字同形：手动 `refetch` 与一次 60 秒轮询很
 * 可能重叠，先发的那次（读的是更早的库状态）晚到不许盖住后到的。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { regimeApi, type HaltState } from "@/lib/api";

export const HALT_STATE_POLL_MS = 60_000;

export function useHaltState(pollingInterval = HALT_STATE_POLL_MS) {
  const [data, setData] = useState<HaltState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const seq = useRef(0);

  const fetchHaltState = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const next = await regimeApi.getHaltState();
      if (mine !== seq.current) return;
      setData(next);
      setError(null);
    } catch (caught) {
      // **不清 `data`**：见文件头第 2 条。
      if (mine !== seq.current) return;
      if (caught instanceof Error) setError(caught.message);
      else setError(String(caught));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchHaltState();
    const timer = setInterval(() => {
      void fetchHaltState();
    }, pollingInterval);
    return () => clearInterval(timer);
  }, [fetchHaltState, pollingInterval]);

  return { data, loading, error, refetch: fetchHaltState };
}
