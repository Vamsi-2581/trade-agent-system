"use client";

/**
 * 交易页顶部的停止横幅（ADR 0001 的 halt 读面）：**常显**，一次说清两件事——「当前行情
 * 阶段」与「此刻是否被停止判定拦住」。
 *
 * 五条纪律，每条都对应这个仓库里已经记过的一笔：
 *
 *   1. **不许隐藏**。这里最要紧的答案之一是**放行**（用户原话：「当前阶段是否放行需要在
 *      交易界面标示出来」）。只在下单被拦时才冒出来的提示，会让人把「没提示」读成「放行」；
 *      而「没提示」至少还有另外两种成因——**还没取到**、**取数失败**。所以横幅常驻，三种
 *      情形各说各的话，谁也不冒充谁。
 *   2. **halt 是集合，不是布尔**。可能同时有多层在拦，**必须全部列出**。本仓明确写过这条
 *      禁令：「同时生效时只报一层，会让人按报出来的那层去排查，而解除它之后单还是下不
 *      出去」。因此这里对 `haltLayers` 只做 `map`，没有任何「取第一条」的分支。
 *   3. **`layer.text` 原样渲染**。它是后端「行 → 层」的唯一换算口（`halt.HaltLayer.text`），
 *      前端不按 label/trigger/scope 再拼一遍，也不给 trigger/scope 做任何中文映射——那会
 *      给同一句话造出第二个版本。同理，当前阶段渲染的是后端给的 `regimeState.display`，
 *      不是把 `value` 在前端翻译一遍。
 *   4. **取不到就不下结论**。「还没读到」与「取数失败」都不许渲染那句「当前无停止判定
 *      命中」：它是**放行**，而拿「不知道」冒充放行，方向恰好是最危险的那一侧。
 *   5. **状态变化给一次视觉强化**。从「无层」到「有层」（以及反向）时闪一下，让正在盯盘
 *      的人不必逐字重读就知道刚刚变了——但仍然**不靠它**传达结论：结论永远在那句话里。
 *
 * 本组件是**受控的纯展示件，自己不发请求**（数据由页面从 `useHaltState` / `useRegimeOverview`
 * 传进来），所以下面每一条分支都能用一次 props 单测覆盖。
 */

import { useEffect, useRef, useState } from "react";
import type { HaltLayerPayload, Labelled } from "@/lib/api";
import { formatMoment } from "@/lib/format";

/**
 * 状态变化后，强调样式保留的时长。**只是视觉**：横幅的结论不依赖它，褪去之后信息一个字
 * 都没少。用例断言的是「变化后那一帧带标记」，不依赖这个计时器。
 */
export const HALT_FLASH_MS = 2400;

/** 一次状态翻转的方向：`engaged` = 无层 → 有层，`cleared` = 有层 → 无层。 */
export type HaltFlash = "engaged" | "cleared";

export interface HaltBannerProps {
  /** 当前行情阶段（`regime_state.regime`）。`null` = 还没有数据：那一段整段不渲染。 */
  regimeState: Labelled | null;
  /** 此刻真的在拦的层（`UseHaltState().data?.halt_layers ?? []`）。**可能不止一层。** */
  haltLayers: HaltLayerPayload[];
  /**
   * 数据截止时刻（页面从 `halt_layers` 里派生的最近一个 `opened_at`；无层时传 `null`）。
   *
   * 后端**不造「现在」**（overview 立过的那条纪律：后端说「现在」的那一刻它就已经是过去的
   * 了），所以这个值只能由调用方从载荷里取。
   */
  asOf: string | null;
  /** 首次请求还没落定。**只用来决定占位**，不参与「有没有层」的判断。 */
  loading: boolean;
  /** 取数错误原文。非空时内联显示，**但上面的旧数据照旧渲染**（配套 `useHaltState` 不清
   *  `data` 的纪律）。 */
  error: string | null;
}

export default function HaltBanner({
  regimeState,
  haltLayers,
  asOf,
  loading,
  error,
}: HaltBannerProps) {
  const hasLayers = haltLayers.length > 0;

  // 上一次「是否有层」。挂载时按当前值起算，所以**首帧不算一次变化**（页面一打开就闪，
  // 闪得多了人就不看了）；此后每次翻转给一次短暂的强调。
  const previousHadLayers = useRef(hasLayers);
  const [flash, setFlash] = useState<HaltFlash | null>(null);

  useEffect(() => {
    if (previousHadLayers.current === hasLayers) return;
    previousHadLayers.current = hasLayers;
    setFlash(hasLayers ? "engaged" : "cleared");
    const timer = setTimeout(() => setFlash(null), HALT_FLASH_MS);
    return () => clearTimeout(timer);
  }, [hasLayers]);

  // ①「还没读到」：首次请求未落定，且手上什么都没有。此时不许说「无层」——那是拿还没读到
  //   的东西冒充放行。
  const pending = loading && !hasLayers && asOf === null;
  // ②「取不到」：请求已经落定过一次并失败，手里仍然一层都没有（首次就失败，或成功过但那次
  //   本来就没有层→那会走下面的 error === null 分支）。同样不许说「无层」。
  const unknown = !pending && !hasLayers && error !== null;

  const emphasis =
    flash === "engaged"
      ? "border-amber/60"
      : flash === "cleared"
        ? "border-[rgba(255,255,255,0.3)]"
        : hasLayers
          ? "border-red/25"
          : "border-[rgba(255,255,255,0.07)]";

  // 横幅此刻「说的是哪一回事」，机器可读。四种互斥，谁也冒充不了谁：
  //   `halted`  有层在拦（放行的反面）
  //   `clear`   读到过、且没有层 —— 唯一的「放行」
  //   `pending` 还没读到
  //   `unknown` 读不到
  const state: "halted" | "clear" | "pending" | "unknown" = hasLayers
    ? "halted"
    : pending
      ? "pending"
      : unknown
        ? "unknown"
        : "clear";

  return (
    <section
      data-testid="halt-banner"
      // 「这一刻有没有层在拦」的机器可读答案（集合的规模，不是布尔）。断言不必去数文本。
      data-halt-layers={haltLayers.length}
      data-halt-state={state}
      // 状态翻转的标记：只在翻转后的那一段强调里存在，其余时候连属性都没有。
      data-halt-transition={flash ?? undefined}
      className={`border rounded-xl px-3 py-2.5 ${
        hasLayers ? "bg-red-dim/20" : "bg-bg1"
      } ${emphasis} ${flash === null ? "" : `halt-flash halt-flash-${flash} animate-pulse-slow`}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-xs font-bold text-text flex-shrink-0">停止判定</span>

        {/* 当前行情阶段：`null` 时**整段不渲染**（不写「未知阶段」——那是一句结论，而这里
            只是还没有数据）。显示的是后端给的 `display`，不是 `value` 的翻译。 */}
        {regimeState !== null && (
          <span data-testid="halt-regime-state" className="text-[11px] text-text2">
            当前行情阶段：
            <span className="font-semibold text-text">{regimeState.display}</span>
          </span>
        )}

        {/* 「这一句」是横幅的结论，全篇只此一处。下面只补它需要的细节（层、或为什么判不了），
            绝不把同一句话写第二遍——同一句话在界面上出现两次，改动时就会只改一处。 */}
        <span
          data-testid="halt-summary"
          className={`text-[11px] font-semibold ${
            hasLayers ? "text-red" : unknown ? "text-amber" : "text-text3"
          }`}
        >
          {hasLayers
            ? `此刻被停止判定拦住（${haltLayers.length} 层）`
            : pending
              ? "正在读取停止状态…"
              : unknown
                ? "停止状态读取失败"
                : "当前无停止判定命中"}
        </span>

        {/* 「数据截止」常驻（旧值可以显示，但必须带着它的时刻）。无层时页面传 `null`，
            没有可靠的时刻可说，那一段就干脆不渲染。 */}
        {asOf !== null && (
          <span data-testid="halt-as-of" className="text-[10px] text-text3 ml-auto">
            数据截止 {formatMoment(asOf)}
          </span>
        )}
      </div>

      {hasLayers ? (
        // **全部列出**：有几层说几层，一个都不合并、一个都不省略。
        <ul data-testid="halt-layers" className="mt-1.5 flex flex-col gap-1.5">
          {haltLayers.map((layer) => (
            <li
              key={`${layer.text}-${layer.opened_at}`}
              data-testid="halt-layer"
              className="text-[11px] leading-relaxed"
            >
              {/* 后端拼好的整句，原样渲染。 */}
              <div data-testid="halt-layer-text" className="font-semibold text-red/90">
                {layer.text}
              </div>
              <div className="text-text3">
                {layer.reason} · 起 {formatMoment(layer.opened_at)} ·{" "}
                {layer.expires_at ? `至 ${formatMoment(layer.expires_at)}` : "无到期"}
              </div>
            </li>
          ))}
        </ul>
      ) : pending ? (
        <p data-testid="halt-pending" className="mt-1.5 text-[11px] text-text3">
          还没读到停止状态——在那之前这里不写成「无」。
        </p>
      ) : unknown ? (
        <p data-testid="halt-unknown" className="mt-1.5 text-[11px] text-text3 leading-relaxed">
          此刻是否有层在拦未知——取不到就不按「无」处理。
        </p>
      ) : null}

      {/* 取数失败：如实显示后端原文，**同时保留上面那份旧数据**（可能已过期，所以要说）。 */}
      {error !== null && (
        <div
          data-testid="halt-error"
          className="mt-1.5 bg-red-dim/20 border border-red/25 rounded-lg px-3 py-2"
        >
          <div className="text-[9px] text-red uppercase tracking-wider font-semibold mb-0.5">
            停止状态取数失败
          </div>
          <div className="text-[11px] text-red/90 font-mono break-all leading-relaxed">
            {error}
          </div>
          <div className="text-[10px] text-text3 mt-1">
            {hasLayers
              ? "上面列的层是最后一次成功取到的那份，可能已过期。"
              : "停止状态还没成功读到过。"}
          </div>
        </div>
      )}
    </section>
  );
}
