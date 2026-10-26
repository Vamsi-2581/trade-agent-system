"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  liveSessionApi,
  type LiveSession,
  type CreateSessionPayload,
  type PromoteDryRun,
  type PromoteResult,
} from "@/lib/api";

export type SessionAction = "start" | "pause" | "resume" | "stop" | "delete";

/**
 * 「升级为实盘」的两拍状态（spec #19 D11 的三步人工卡点）。
 *
 * **它不能挤进 `doAction` 那一族**：其余五个动作是「一次 POST 改一个状态」，
 * 而 promote 的后端契约是**默认干跑**——不带 `confirmed` 只回一段摘要、什么都不改。
 * 2026-10-26 之前前端正是把它当普通动作发了一次无 body 的 POST：后端每次都回
 * 200 + 干跑摘要，`request()` 顺利返回，`doAction` 把它丢掉再重拉列表，
 * 于是**按钮点下去屏幕上什么都不变**——这就是用户报的「点升级为实盘没有反应」。
 *
 * 所以这一族必须自己拿住三样东西：干跑摘要（要给人看）、失败原文（后端那句
 * 「必须显式指定目标实盘账户」）、以及「正在确认」的中间态。
 */
export interface PromotionState {
  /** 非空时前端要显示「将执行什么」的确认层；内容逐字取自后端。 */
  preview: PromoteDryRun | null;
  /** 真升成功之后的回执。 */
  result: PromoteResult | null;
  /** 后端原文，原样显示（`ApiError.message`）。 */
  error: string | null;
  /** 正在请求干跑 / 正在确认。 */
  busy: boolean;
}

const IDLE_PROMOTION: PromotionState = {
  preview: null,
  result: null,
  error: null,
  busy: false,
};

export function useLiveSessions(pollingInterval = 10000) {
  const [sessions, setSessions] = useState<LiveSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [promotion, setPromotion] = useState<PromotionState>(IDLE_PROMOTION);
  const pollingRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  const fetchSessions = useCallback(async () => {
    try {
      const data = await liveSessionApi.list();
      setSessions(data);
      setError(null);
    } catch {
      // Ignore errors — API may not be available yet
    } finally {
      setLoading(false);
    }
  }, []);

  const doAction = useCallback(
    async (id: string, action: SessionAction) => {
      setActionLoading(true);
      setError(null);
      try {
        switch (action) {
          case "start":
            await liveSessionApi.start(id);
            break;
          case "pause":
            await liveSessionApi.pause(id);
            break;
          case "resume":
            await liveSessionApi.resume(id);
            break;
          case "stop":
            await liveSessionApi.stop(id);
            break;
          case "delete":
            await liveSessionApi.delete(id);
            break;
        }
        // Refresh after action
        await fetchSessions();
      } catch (e) {
        setError(e instanceof Error ? e.message : "操作失败");
      } finally {
        setActionLoading(false);
      }
    },
    [fetchSessions]
  );

  /**
   * 第一拍：拿干跑摘要。**目标账户必须由人指定**，所以这是 `promote(id, accountId)`。
   *
   * 失败时把后端原文放进 `promotion.error`（而不是上面那个通用的 `error`）：
   * 这句话是长句（「升实盘必须显式指定目标实盘账户（exchange_account_id…）」），
   * 只适合放在升级流程自己的那一块里读，塞进页头一行小字里等于没说。
   */
  const requestPromotion = useCallback(async (id: string, exchangeAccountId: string) => {
    setPromotion({ ...IDLE_PROMOTION, busy: true });
    try {
      const payload = await liveSessionApi.promote(id, {
        exchange_account_id: exchangeAccountId,
      });
      if ("dry_run" in payload && payload.dry_run) {
        setPromotion({ ...IDLE_PROMOTION, preview: payload });
      } else {
        // 后端在没带 confirmed 时永远回干跑；真收到成品说明契约变了，
        // 这里按成品处理并把列表刷新掉，而不是假装还在确认。
        setPromotion({ ...IDLE_PROMOTION, result: payload as PromoteResult });
        await fetchSessions();
      }
    } catch (e) {
      setPromotion({
        ...IDLE_PROMOTION,
        error: e instanceof Error ? e.message : "升级为实盘失败",
      });
    }
  }, [fetchSessions]);

  /** 第二拍：人点了确认，带 `confirmed: true` 真正建 live 会话。 */
  const confirmPromotion = useCallback(
    async (id: string, exchangeAccountId: string) => {
      setPromotion((prev) => ({ ...prev, busy: true, error: null }));
      try {
        const payload = await liveSessionApi.promote(id, {
          exchange_account_id: exchangeAccountId,
          confirmed: true,
        });
        setPromotion({
          preview: null,
          result: payload as PromoteResult,
          error: null,
          busy: false,
        });
        await fetchSessions();
      } catch (e) {
        setPromotion((prev) => ({
          ...prev,
          busy: false,
          error: e instanceof Error ? e.message : "升级为实盘失败",
        }));
      }
    },
    [fetchSessions]
  );

  /** 关掉确认层 / 回执，回到空闲。 */
  const dismissPromotion = useCallback(() => {
    setPromotion(IDLE_PROMOTION);
  }, []);

  const createSession = useCallback(
    async (data: CreateSessionPayload) => {
      setActionLoading(true);
      setError(null);
      try {
        const result = await liveSessionApi.create(data);
        await fetchSessions();
        return result;
      } catch (e) {
        setError(e instanceof Error ? e.message : "创建失败");
        throw e;
      } finally {
        setActionLoading(false);
      }
    },
    [fetchSessions]
  );

  useEffect(() => {
    fetchSessions();
    pollingRef.current = setInterval(fetchSessions, pollingInterval);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [fetchSessions, pollingInterval]);

  return {
    sessions,
    loading,
    actionLoading,
    error,
    refetch: fetchSessions,
    start: (id: string) => void doAction(id, "start"),
    pause: (id: string) => void doAction(id, "pause"),
    resume: (id: string) => void doAction(id, "resume"),
    stop: (id: string) => void doAction(id, "stop"),
    remove: (id: string) => void doAction(id, "delete"),
    create: createSession,
    promotion,
    requestPromotion,
    confirmPromotion,
    dismissPromotion,
  };
}
