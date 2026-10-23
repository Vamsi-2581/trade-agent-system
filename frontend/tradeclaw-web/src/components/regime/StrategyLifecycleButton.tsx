"use client";

/**
 * 一条策略的「删除策略」/「恢复启用」按钮 + 确认弹窗（`/regime` 池化表的行内动作）。
 *
 * 三件事在这里定死，每一件都是「同一件事的第二个说法」的防线：
 *
 *   1. **「删除」到底做了什么，由后端的确认页正文说**：这一块**不写**任何解释性文案。
 *      「不删数据」「不停会话」「解除声明之后可以重新开仓」这三句的家在
 *      `strategy_lifecycle._retire_body`——那是唯一一处能把「删除」与用户直觉相反的那一面
 *      讲全的地方，前端再写一遍就是第二个版本（而它迟早会说错一半）。
 *   2. **按钮说哪个方向，取自后端的 `lifecycle.value`**：`retired` 时这一格是「恢复启用」，
 *      否则是「删除策略」。前端不拿 `is_active` 自己判——那个布尔在这条链路上没有出现过。
 *   3. **一次动作两次请求**（与 `SwitchCard` 同形）：先 GET 确认页（后端渲染的正文 +
 *      将落库的那句话），人点头之后才 POST。**「本来就是这一档」不是错误**（200 +
 *      `changed:false`），走同一条渲染路径、不染红。
 *
 * 权限是 `isStaff`（ADR 0005：界面纪律，不是安全边界）：非 staff 连按钮都不渲染，
 * 服务端同样会挡，而 slash / 管理命令那条通路没有跟着收紧。
 */

import { useState } from "react";
import {
  regimeApi,
  type RegimeLifecycleAction,
  type RegimeStrategyFlipResult,
  type RegimeStrategyLifecyclePage,
  type RegimeStrategyRef,
} from "@/lib/api";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

type Phase = "idle" | "loading" | "confirm" | "working" | "result";

/** 这一格该按哪个方向：`retired` ⇒ 恢复启用，其余（含认不出的脏值）⇒ 删除策略。 */
function sideOf(strategy: RegimeStrategyRef): "retire" | "restore" {
  return strategy.lifecycle.value === "retired" ? "restore" : "retire";
}

const BUTTON_LABEL: Record<"retire" | "restore", string> = {
  retire: "删除策略",
  restore: "恢复启用",
};

export interface StrategyLifecycleButtonProps {
  /** 池化表格子里的那一行（`serializers.strategy_payload`）。 */
  strategy: RegimeStrategyRef;
  /** 写权限（ADR 0005）。`false` 时整块不渲染。 */
  isStaff: boolean;
  /** 翻转成功之后回调，让页面立刻重拉池化表（而不是等下一次重算/刷新）。 */
  onChanged?: () => void;
}

export default function StrategyLifecycleButton({
  strategy,
  isStaff,
  onChanged,
}: StrategyLifecycleButtonProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [page, setPage] = useState<RegimeStrategyLifecyclePage | null>(null);
  const [result, setResult] = useState<RegimeStrategyFlipResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!isStaff) return null;

  const side = sideOf(strategy);
  const action: RegimeLifecycleAction | null = page === null ? null : page[side];

  const openConfirm = async () => {
    setPhase("loading");
    setError(null);
    setResult(null);
    try {
      // 重请求、按需拉：它带的是「此刻」的四个数与后端渲染的正文，缓存一次就可能拿一份
      // 与按下时刻不符的说明。
      const next = await regimeApi.getStrategyLifecycle(strategy.id);
      setPage(next);
      setPhase("confirm");
    } catch (caught) {
      setError(messageOf(caught));
      setPhase("idle");
    }
  };

  const confirm = async () => {
    if (action === null) return;
    setPhase("working");
    setError(null);
    try {
      const payload =
        side === "restore"
          ? await regimeApi.restoreStrategy(strategy.id)
          : await regimeApi.retireStrategy(strategy.id);
      setResult(payload);
      setPhase("result");
      if (payload.changed) onChanged?.();
    } catch (caught) {
      // 后端原文（`ApiError.message`）：「此操作需要管理员权限。」「没有 id=… 的这一条
      // 策略」这类话是给人照着改的，不加工成「操作失败」。
      setError(messageOf(caught));
      setPhase("confirm");
    }
  };

  const close = () => {
    setPhase("idle");
    setPage(null);
    setResult(null);
    setError(null);
  };

  const dialogOpen =
    page !== null && (phase === "confirm" || phase === "working" || phase === "result");

  return (
    <>
      <button
        type="button"
        data-testid={`strategy-action-${strategy.id}`}
        data-side={side}
        onClick={() => void openConfirm()}
        disabled={phase === "loading"}
        title={
          side === "retire"
            ? "从机制里删除这条策略（退役：不删数据、不停会话）"
            : "把它放回机制管辖（不产生豁免）"
        }
        className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors disabled:opacity-40 ${
          side === "retire"
            ? "border-red/30 text-red hover:bg-red-dim/40"
            : "border-[rgba(255,255,255,0.12)] text-text2 hover:bg-bg2"
        }`}
      >
        {phase === "loading" ? "读取…" : BUTTON_LABEL[side]}
      </button>

      {error !== null && !dialogOpen && (
        <p
          data-testid={`strategy-error-${strategy.id}`}
          className="text-[10px] text-red whitespace-pre-wrap break-all"
        >
          {error}
        </p>
      )}

      {dialogOpen && action !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div
            data-testid="strategy-confirm"
            data-side={side}
            className="bg-bg1 border border-[rgba(255,255,255,0.1)] rounded-xl w-full max-w-lg p-6 space-y-4 max-h-[85vh] overflow-y-auto"
          >
            <h3 className="text-sm font-bold text-text">
              {strategy.name}
              <span className="text-text3 font-normal"> #{strategy.id}</span>
              {"："}
              {action.action.display}
            </h3>

            {/* 后端渲染的那一整页：做与不做的清单、四个数、以及三个后果。 */}
            <pre
              data-testid="strategy-confirm-body"
              className="text-[11px] leading-relaxed font-mono text-text2 whitespace-pre-wrap break-all bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg p-3"
            >
              {action.body}
            </pre>

            {action.available && (
              <div className="text-[11px] text-text3 space-y-1">
                <p>落库的启停理由由系统写入（与这一页同源，不手写）：</p>
                <p data-testid="strategy-confirm-summary" className="text-text2 break-all">
                  {action.summary}
                </p>
              </div>
            )}

            {phase === "confirm" && error !== null && (
              <p
                data-testid="strategy-confirm-error"
                className="bg-red-dim/20 border border-red/30 rounded-lg px-3 py-2 text-xs text-red whitespace-pre-wrap"
              >
                {error}
              </p>
            )}

            {result !== null ? (
              <div
                data-testid="strategy-result"
                data-changed={result.changed ? "true" : "false"}
                className={`rounded-lg border px-3 py-2 space-y-2 text-[11px] ${
                  result.changed
                    ? "border-green/40 bg-green-dim"
                    : "border-[rgba(255,255,255,0.12)] bg-bg3"
                }`}
              >
                {/* 回执原文：`changed:false` 时它自己就是「本来就是这一档」那句话。
                    不因为没变化就染红——它是一次成功但没有变化的动作。 */}
                <p
                  data-testid="strategy-result-display"
                  className={result.changed ? "text-green" : "text-text2"}
                >
                  {result.display}
                </p>
                <p className="text-text3">
                  落库的启停理由：
                  <span
                    data-testid="strategy-result-reason"
                    className="text-text2 break-all"
                  >
                    {result.reason}
                  </span>
                </p>
                <button
                  type="button"
                  data-testid="strategy-result-close"
                  onClick={close}
                  className="bg-bg2 text-text px-3 py-1 rounded-md"
                >
                  关闭
                </button>
              </div>
            ) : (
              <div className="flex gap-2 justify-end">
                <button
                  type="button"
                  data-testid="strategy-confirm-cancel"
                  onClick={close}
                  disabled={phase === "working"}
                  className="px-4 py-1.5 rounded-md text-xs font-semibold border border-[rgba(255,255,255,0.1)] text-text3 hover:text-text disabled:opacity-40"
                >
                  取消
                </button>
                <button
                  type="button"
                  data-testid="strategy-confirm-ok"
                  onClick={() => void confirm()}
                  disabled={phase === "working" || !action.available}
                  className={`px-4 py-1.5 rounded-md text-xs font-semibold border disabled:opacity-40 ${
                    side === "retire"
                      ? "border-red/30 bg-red-dim text-red hover:bg-red-dim/80"
                      : "border-[rgba(255,255,255,0.12)] bg-bg2 text-text hover:bg-bg3"
                  }`}
                >
                  {phase === "working" ? "处理中…" : action.action.display}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}