"use client";

/**
 * 代际历史：每一代池化表「谁、何时、算的什么」，以及某一代与上一代的差异。
 *
 * 两处口径刻意不在这里重算：
 *
 *   1. **「当前是哪一代」由页面传 `currentGenerationId`**（它来自 `overview` 的
 *      `pool.generation.id`）。本端点 `pool/generations/` **不标**当前代：两处各查一次
 *      `current_generation()` 之间翻了一代，页面上就会出现两条都自称当前的行。
 *   2. **「变了多少格」有两个数**：记录里那一刻算出来的 `recorded_cells_changed`，以及
 *      逐格差异的 `counts`。它们**必须相等**，所以这里把两个都摆出来、并在不相等时当场
 *      说明——不相等是口径分叉，比「数据脏了」严重得多，不能只在某一处显示一个看起来
 *      正常的数。
 *
 * 差异是**按需拉**的：`getGenerations({ generation })` 才附 `diff`。选中某一代只是多一次
 * 读，不影响上面那张列表。
 */

import { useRef, useState } from "react";
import {
  regimeApi,
  type RegimeGeneration,
  type RegimeGenerations,
} from "@/lib/api";
import { formatNumber } from "@/lib/format";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 时刻：后端给 ISO 串；没有就是「—」（不是空串，也不是「刚刚」）。 */
function formatMoment(moment: string | null): string {
  if (moment === null) return "—";
  const parsed = new Date(moment);
  if (Number.isNaN(parsed.getTime())) return moment;
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

const diffKindLabel: Record<string, string> = {
  changed: "改动",
  added: "新增",
  removed: "移除",
};

const diffKindClass: Record<string, string> = {
  changed: "bg-amber/20 text-amber",
  added: "bg-green/20 text-green",
  removed: "bg-red/20 text-red",
};

/** 差异的一侧：`(状态, 原因, 来源)` 三元组；空表示这一代没有这一格。 */
function tripleText(
  triple: { state: { display: string }; reason: { display: string }; source: { display: string } } | null
): string {
  if (triple === null) return "—";
  const reason = triple.reason.display === "" ? "" : `：${triple.reason.display}`;
  return `${triple.state.display}${reason}`;
}

export interface GenerationHistoryProps {
  /** 代际历史（`GET /api/regime/pool/generations/` 的 `results`）。 */
  generations: RegimeGeneration[];
  /** 当前代的 id（由 `overview` 给）；只用来打一个「当前代」标记。 */
  currentGenerationId?: number | null;
}

export default function GenerationHistory({
  generations,
  currentGenerationId = null,
}: GenerationHistoryProps) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [diff, setDiff] = useState<RegimeGenerations["diff"] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 连点两代会各自发一次请求，先到的旧响应不许盖住后到的：只有最后一次亮出去。
  const requestSeq = useRef(0);

  const showDiff = async (id: number) => {
    const seq = ++requestSeq.current;
    setSelectedId(id);
    setLoading(true);
    setError(null);
    setDiff(null);
    try {
      const payload = await regimeApi.getGenerations({ generation: id });
      if (seq !== requestSeq.current) return;
      setDiff(payload.diff ?? null);
    } catch (caught) {
      if (seq !== requestSeq.current) return;
      setError(messageOf(caught));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  };

  if (generations.length === 0) {
    return (
      <div
        data-testid="generations-empty"
        className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl p-4 text-xs font-mono text-text3"
      >
        还没有任何一代池化表：先跑一次池化重算。
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-x-auto">
        <table className="w-full text-xs font-mono" data-testid="generation-history">
          <thead>
            <tr className="text-text3 border-b border-[rgba(255,255,255,0.07)]">
              <th className="text-left py-2.5 px-3 font-semibold">代</th>
              <th className="text-left py-2.5 px-2 font-semibold">状态</th>
              <th className="text-left py-2.5 px-2 font-semibold">触发方</th>
              <th className="text-left py-2.5 px-2 font-semibold">开始</th>
              <th className="text-left py-2.5 px-2 font-semibold">结束</th>
              <th className="text-right py-2.5 px-2 font-semibold">候选</th>
              <th className="text-right py-2.5 px-2 font-semibold">用上</th>
              <th className="text-right py-2.5 px-2 font-semibold">格子</th>
              <th className="text-right py-2.5 px-2 font-semibold">变化</th>
              <th className="text-left py-2.5 px-2 font-semibold">收场</th>
              <th className="text-right py-2.5 px-3 font-semibold">差异</th>
            </tr>
          </thead>
          <tbody>
            {generations.map((generation) => {
              const isCurrent = currentGenerationId === generation.id;
              const isSelected = selectedId === generation.id;
              return (
                <tr
                  key={generation.id}
                  data-testid={`generation-row-${generation.id}`}
                  className={`border-b border-[rgba(255,255,255,0.04)] ${
                    isSelected ? "bg-bg2/60" : ""
                  }`}
                >
                  <td className="py-2 px-3 text-text whitespace-nowrap">
                    #{generation.id}
                    {isCurrent && (
                      <span
                        data-testid="current-generation-badge"
                        className="ml-1.5 bg-green/20 text-green px-1 rounded text-[9px]"
                      >
                        当前代
                      </span>
                    )}
                  </td>
                  <td className="py-2 px-2 text-text2">{generation.status.display}</td>
                  <td className="py-2 px-2 text-text2 whitespace-nowrap">
                    {generation.actor_kind.display} · {generation.actor_name}
                  </td>
                  <td className="py-2 px-2 text-text3 whitespace-nowrap">
                    {formatMoment(generation.started_at)}
                  </td>
                  <td className="py-2 px-2 text-text3 whitespace-nowrap">
                    {formatMoment(generation.finished_at)}
                  </td>
                  <td className="py-2 px-2 text-right text-text2">
                    {formatNumber(generation.candidates, 0)}
                  </td>
                  <td className="py-2 px-2 text-right text-text2">
                    {formatNumber(generation.results_used, 0)}
                  </td>
                  <td className="py-2 px-2 text-right text-text2">
                    {formatNumber(generation.cells_total, 0)}
                  </td>
                  <td className="py-2 px-2 text-right text-text">
                    {formatNumber(generation.cells_changed, 0)}
                  </td>
                  <td className="py-2 px-2 text-text2">
                    {generation.outcome.display}
                    {generation.failure !== "" && (
                      <span className="block text-red text-[10px]">
                        {generation.failure}
                      </span>
                    )}
                    {generation.stale_building.stale && (
                      <span
                        data-testid={`generation-stale-${generation.id}`}
                        className="block text-amber text-[10px]"
                      >
                        超过 {generation.stale_building.threshold_minutes} 分钟仍在重算中，检查
                        worker
                      </span>
                    )}
                  </td>
                  <td className="py-2 px-3 text-right">
                    <button
                      type="button"
                      data-testid={`generation-diff-${generation.id}`}
                      aria-expanded={isSelected}
                      onClick={() => void showDiff(generation.id)}
                      className="text-green hover:underline cursor-pointer whitespace-nowrap"
                    >
                      看差异
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {selectedId !== null && (
        <div
          data-testid="generation-diff"
          className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl p-3 text-xs font-mono"
        >
          {loading && <p className="text-text3">正在读取第 #{selectedId} 代的差异…</p>}
          {error !== null && (
            <p data-testid="generation-diff-error" className="text-red">
              差异取数失败：{error}
            </p>
          )}
          {!loading && error === null && diff && (
            <DiffBody diff={diff} />
          )}
          {!loading && error === null && diff === null && (
            <p className="text-text3">
              第 #{selectedId} 代的差异为空（后端没有给出 `diff`）。
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function DiffBody({ diff }: { diff: NonNullable<RegimeGenerations["diff"]> }) {
  const sum = diff.counts.changed + diff.counts.added + diff.counts.removed;
  const diverged = sum !== diff.recorded_cells_changed;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-text font-semibold">第 #{diff.generation} 代</span>
        <span className="text-text3">
          对比{" "}
          {diff.previous_generation === null
            ? "（没有上一代）"
            : `第 #${diff.previous_generation} 代`}
        </span>
        <span className="text-text2">记录的变化格数</span>
        <span data-testid="diff-recorded" className="text-text">
          {formatNumber(diff.recorded_cells_changed, 0)}
        </span>
        <span className="text-text2">逐格合计</span>
        <span data-testid="diff-counts" className="text-text">
          {formatNumber(sum, 0)}
        </span>
        <span className="text-text2">
          改动 {formatNumber(diff.counts.changed, 0)} · 新增{" "}
          {formatNumber(diff.counts.added, 0)} · 移除{" "}
          {formatNumber(diff.counts.removed, 0)}
        </span>
      </div>

      {diverged && (
        <p data-testid="diff-diverged" className="text-amber">
          记录的变化格数与逐格合计不一致（{formatNumber(diff.recorded_cells_changed, 0)} vs{" "}
          {formatNumber(sum, 0)}）：这不是数据脏，是差异口径分叉了。
        </p>
      )}

      {diff.previous_generation === null && (
        <p data-testid="diff-first-generation" className="text-text3">
          这是第一代，没有可比的上一代：列出的每一格都是新增。
        </p>
      )}

      {diff.cells.length === 0 ? (
        <p data-testid="diff-empty" className="text-text3">
          与上一代相比没有变化。
        </p>
      ) : (
        <table className="w-full text-[11px]" data-testid="diff-table">
          <thead>
            <tr className="text-text3 border-b border-[rgba(255,255,255,0.07)]">
              <th className="text-left py-1.5 px-2 font-semibold">策略</th>
              <th className="text-left py-1.5 px-2 font-semibold">阶段</th>
              <th className="text-left py-1.5 px-2 font-semibold">种类</th>
              <th className="text-left py-1.5 px-2 font-semibold">上一代</th>
              <th className="text-left py-1.5 px-2 font-semibold">这一代</th>
            </tr>
          </thead>
          <tbody>
            {diff.cells.map((cell) => (
              <tr
                key={cell.key}
                data-testid={`diff-cell-${cell.key}`}
                className="border-b border-[rgba(255,255,255,0.04)]"
              >
                <td className="py-1.5 px-2 text-text">
                  {cell.strategy.name}
                  <span className="text-text3 font-normal"> #{cell.strategy.id}</span>
                </td>
                <td className="py-1.5 px-2 text-text2">{cell.regime.display}</td>
                <td className="py-1.5 px-2">
                  <span
                    className={`px-1 rounded text-[10px] ${
                      diffKindClass[cell.kind] ?? "bg-bg3 text-text3"
                    }`}
                  >
                    {diffKindLabel[cell.kind] ?? cell.kind}
                  </span>
                </td>
                <td
                  className="py-1.5 px-2 text-text2"
                  title={cell.before?.source.display ?? ""}
                >
                  {tripleText(cell.before)}
                </td>
                <td
                  className="py-1.5 px-2 text-text"
                  title={cell.after?.source.display ?? ""}
                >
                  {tripleText(cell.after)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
