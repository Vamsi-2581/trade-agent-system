"use client";

/**
 * 一个开关的档位卡片 + 切换确认弹窗（P5 #49，map #40 的上半页）。
 *
 * 三件事在这里定死，每一件都是「同一件事的第二个说法」的防线：
 *
 *   1. **状态词一律取后端字段**：当前档位是 `sw.mode.display`，目标是
 *      `briefing.to_mode.display`，回执是 `result.display`。前端不把 `shadow` 翻成
 *      「Shadow」、不判断「从执行态退回」——那些句子的家在后端（`MechanismMode` 与
 *      `views._switch_display`），前端再写一份就是第二个说法。
 *   2. **切换理由不由人写**：弹窗只显示后端这一页的 `summary`（人点头之后落库的就是
 *      它），没有输入框。手写理由会让同一条流水里出现两种性质不同的字符串。
 *   3. **「这一档不是执行开关」说三次**：卡片常驻、弹窗里、结果里各一次，三次都渲染
 *      `sw.note` 这一个后端字段。前端**不写这句解释**——`MechanismKind.MECHANISM` 在
 *      全仓只有读者，真正拦人的是另外两个开关；这句话说错或者被前端改写，人就会按完
 *      以为机制上线了，而它什么都没打开。`sw.note` 为 `null`（另外两个开关）时三处都
 *      不渲染，不补一句「无说明」。
 *
 * 交互：点按钮先拉一次 `getBriefing`（重请求，不进轮询），拿到那一页再问「确不确定」；
 * 确认后 `switchMode`，回执在同一个弹窗里展开成结果。「本来就是这一档」是 200 +
 * `changed:false`，走的是同一条渲染路径——它不是错误，所以不染红。
 */

import { useState } from "react";
import { formatMoment } from "@/lib/format";
import {
  regimeApi,
  type RegimeBriefing,
  type RegimeKind,
  type RegimeMode,
  type RegimeSwitch,
  type RegimeSwitchResult,
} from "@/lib/api";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export interface SwitchCardProps {
  /** `overview` 里的一行（`switch_payload`）：当前档、最近一次切换、常驻说明。 */
  sw: RegimeSwitch;
  /** 写权限（ADR 0005：界面纪律，不是安全边界）。`false` 时按钮不渲染。 */
  isStaff: boolean;
  /** 切换成功之后回调，让页面立刻重拉 `overview`（而不是等下一个 60 秒）。 */
  onSwitched?: () => void;
}

type Phase = "idle" | "loading" | "confirm" | "working" | "result";

export default function SwitchCard({ sw, isStaff, onSwitched }: SwitchCardProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [briefing, setBriefing] = useState<RegimeBriefing | null>(null);
  const [result, setResult] = useState<RegimeSwitchResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const kind = sw.kind.value;
  const latest = sw.latest;

  /** 点开确认弹窗：**此刻**拉一次确认页（重请求，不缓存、不轮询）。 */
  const askForConfirmation = async () => {
    setPhase("loading");
    setError(null);
    setResult(null);
    try {
      const page = await regimeApi.getBriefing(sw.kind.value as RegimeKind);
      setBriefing(page);
      setPhase("confirm");
    } catch (caught) {
      setError(messageOf(caught));
      setPhase("idle");
    }
  };

  const confirmSwitch = async () => {
    if (briefing === null) return;
    setPhase("working");
    setError(null);
    try {
      // 目标档位取**后端这一页**给的方向（`briefing.to_mode`），不由前端按当前档反推：
      // 「本来就是这一档」的请求上，反推给出的方向恰好是错的那个。
      const payload = await regimeApi.switchMode(
        briefing.kind.value as RegimeKind,
        briefing.to_mode.value as RegimeMode
      );
      setResult(payload);
      setPhase("result");
      onSwitched?.();
    } catch (caught) {
      // 后端原文（`ApiError.message`）：「切换原因不能为空」「此操作需要管理员权限。」
      // 这类话是给人照着改的，不加工。
      setError(messageOf(caught));
      setPhase("confirm");
    }
  };

  const closeDialog = () => {
    setPhase("idle");
    setBriefing(null);
    setResult(null);
    setError(null);
  };

  const dialogOpen = phase === "confirm" || phase === "working" || phase === "result";

  return (
    <section
      data-testid={`switch-card-${kind}`}
      className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl p-4 space-y-3"
    >
      <header className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-text">{sw.kind.display}</h3>
        <span
          data-testid="switch-mode"
          className="text-xs font-mono px-2 py-0.5 rounded bg-bg3 text-text2 shrink-0"
        >
          {sw.mode.display}
        </span>
      </header>

      <div data-testid="switch-latest" className="text-[11px] text-text3 space-y-1">
        {latest === null ? (
          <p>从未切换过（当前档是这个开关的默认值）。</p>
        ) : (
          <>
            <p data-testid="switch-latest-head">
              {formatMoment(latest.at)} · {latest.actor_kind.display}·{latest.actor_name}
              {"："}
              {latest.from_mode.display} → {latest.to_mode.display}
            </p>
            <p data-testid="switch-latest-reason" className="whitespace-pre-wrap break-all">
              {latest.reason}
            </p>
          </>
        )}
      </div>

      {/* 第 1 次说：卡片常驻的那一句，原样显示后端的 `note`。 */}
      {sw.note !== null && (
        <p
          data-testid="switch-note"
          className="text-[11px] text-amber bg-amber-dim/40 border border-amber/25 rounded-lg px-3 py-2 whitespace-pre-wrap"
        >
          {sw.note}
        </p>
      )}

      {isStaff ? (
        <button
          type="button"
          data-testid="switch-action"
          onClick={() => void askForConfirmation()}
          disabled={phase === "loading"}
          className="bg-bg3 hover:bg-bg2 disabled:opacity-40 text-text text-xs px-3 py-1.5 rounded-md"
        >
          {phase === "loading" ? "正在取确认页…" : "切换档位"}
        </button>
      ) : (
        <p data-testid="switch-staff-hint" className="text-[11px] text-text3">
          需要管理员权限。
        </p>
      )}

      {error !== null && !dialogOpen && (
        <div
          data-testid="switch-error"
          className="bg-red-dim/20 border border-red/30 rounded-lg px-3 py-2 text-xs text-red whitespace-pre-wrap"
        >
          {error}
        </div>
      )}

      {dialogOpen && briefing !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div
            data-testid="switch-confirm"
            className="bg-bg1 border border-[rgba(255,255,255,0.1)] rounded-xl w-full max-w-lg p-6 space-y-4 max-h-[85vh] overflow-y-auto"
          >
            <h3 className="text-sm font-bold text-text">
              {briefing.kind.display}：确认切换
            </h3>
            <p data-testid="switch-confirm-target" className="text-xs text-text2">
              当前 {briefing.mode.display} → 目标 {briefing.to_mode.display}
            </p>

            {/* 后端渲染的那一整页：数与人点头之后落进 `reason` 的是同一次快照。 */}
            <pre
              data-testid="switch-confirm-body"
              className="text-[11px] leading-relaxed font-mono text-text2 whitespace-pre-wrap break-all bg-bg2 border border-[rgba(255,255,255,0.07)] rounded-lg p-3"
            >
              {briefing.body}
            </pre>

            {/* 第 2 次说：弹窗里再念一遍，仍然是那个后端字段。 */}
            {sw.note !== null && (
              <p
                data-testid="switch-confirm-note"
                className="text-[11px] text-amber bg-amber-dim/40 border border-amber/25 rounded-lg px-3 py-2 whitespace-pre-wrap"
              >
                {sw.note}
              </p>
            )}

            <div className="text-[11px] text-text3 space-y-1">
              <p>切换理由由系统写入（与这一页同源，不手写）：</p>
              <p data-testid="switch-confirm-summary" className="text-text2 break-all">
                {briefing.summary}
              </p>
            </div>

            {phase === "confirm" && error !== null && (
              <div
                data-testid="switch-error"
                className="bg-red-dim/20 border border-red/30 rounded-lg px-3 py-2 text-xs text-red whitespace-pre-wrap"
              >
                {error}
              </div>
            )}

            {result !== null ? (
              <div
                data-testid="switch-result"
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
                  data-testid="switch-result-display"
                  className={result.changed ? "text-green" : "text-text2"}
                >
                  {result.display}
                </p>
                <p className="text-text3">
                  落库的切换理由：
                  <span data-testid="switch-result-reason" className="text-text2 break-all">
                    {result.reason}
                  </span>
                </p>
                {/* 第 3 次说：切完之后的结果里再说一遍。 */}
                {sw.note !== null && (
                  <p data-testid="switch-result-note" className="text-amber whitespace-pre-wrap">
                    {sw.note}
                  </p>
                )}
                <button
                  type="button"
                  data-testid="switch-result-close"
                  onClick={closeDialog}
                  className="bg-bg2 text-text px-3 py-1 rounded-md"
                >
                  关闭
                </button>
              </div>
            ) : (
              <div className="flex gap-2 justify-end">
                <button
                  type="button"
                  data-testid="switch-confirm-cancel"
                  onClick={closeDialog}
                  disabled={phase === "working"}
                  className="px-4 py-1.5 rounded-md text-xs font-semibold border border-[rgba(255,255,255,0.1)] text-text3 hover:text-text disabled:opacity-40"
                >
                  取消
                </button>
                <button
                  type="button"
                  data-testid="switch-confirm-ok"
                  onClick={() => void confirmSwitch()}
                  disabled={phase === "working"}
                  className="px-4 py-1.5 rounded-md text-xs font-semibold border border-amber/20 bg-amber-dim text-amber hover:bg-amber-dim/80 disabled:opacity-40"
                >
                  {phase === "working" ? "切换中…" : "确认切换"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
