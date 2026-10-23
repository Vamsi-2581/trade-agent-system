"use client";

/**
 * 池化表矩阵：策略 × 阶段。
 *
 * 这一块只做**展示**，数据一律由页面传进来（`GET /api/regime/pool/`）。四件事在这里定死：
 *
 *   1. **列序由后端的 `regimes` 数组给**（`serializers.ORDERED_REGIMES`：高波动 → 下行 →
 *      上行 → 箱体）。前端按数组排，不另写一份顺序——两份顺序各自「看起来都对」时，
 *      错的那份只会在某次迭代里悄悄换位，而没人会去比对。
 *   2. **格子的中文取 `state.display` / `reason.display`**。前端不把 `fit` 翻成「适用」：
 *      那句中文的家在后端（`slice.STATE_DISPLAY`），前端再写一份就是第二个说法。
 *   3. **冷启动不是空表格**：`has_generation=false` 时整张表不渲染，改为后端那句
 *      `display`（「还没有任何一代池化表：先跑一次池化重算……」）加一条重算指引。
 *   4. **行状态与「删除策略」按钮都由页面注入**（`renderStrategyAction`）：这一块不拥有
 *      写逻辑，也不自己判 `is_active`——它只把后端给的 `strategy.lifecycle` 排版出来
 *      （已退役角标）并把页面递进来的那个按钮放进「操作」列。写入口住在页面里
 *      （`StrategyLifecycleButton`），与 `RebuildButton` 同一条分工。
 *
 * 行内展开照 `components/backtest/BacktestTreeTable.tsx` 的 `Set<string>`：一格一个
 * `key`（后端给的 `"<策略 id>|<阶段>"`），展开的内容是那一格的证据（`PoolCellDetail`）。
 */

import { useMemo, useState, type ReactNode } from "react";
import type { Labelled, RegimePool, RegimePoolCell, RegimeStrategyRef } from "@/lib/api";
import { formatMoment, formatNumber } from "@/lib/format";
import PoolCellDetail from "./PoolCellDetail";

type Tone = "green" | "red" | "amber" | "muted" | "blue";

/**
 * 五种状态各一色（`slice.STATE_DISPLAY` 的五档，一个不多一个不少）。
 *
 * 认不出来的状态落 `muted`：一条手工写坏的格子不该让整张表爆掉，也不该被染成「适用」的
 * 绿色——那会让人以为机制放行了某条策略。
 */
const STATE_TONE: Record<string, Tone> = {
  fit: "green",
  unfit: "red",
  neutral: "amber",
  unknown: "muted",
  blanket: "blue",
};

const TONE_CLASS: Record<Tone, string> = {
  green: "bg-green-dim text-green border-green/40",
  red: "bg-red-dim text-red border-red/40",
  amber: "bg-amber-dim text-amber border-amber/40",
  muted: "bg-bg3 text-text3 border-[rgba(255,255,255,0.07)]",
  blue: "bg-blue-dim text-blue border-blue/40",
};

/** 矩阵列的后备顺序（后端 `regimes` 缺失时才用；显示名仍取格子自己的 `regime.display`）。 */
const FALLBACK_REGIME_ORDER = ["high_vol", "downtrend", "uptrend", "range"];

function columnsOf(pool: RegimePool): Labelled[] {
  if (pool.regimes.length > 0) return pool.regimes;
  const seen = new Map<string, Labelled>();
  for (const cell of pool.cells) {
    if (!seen.has(cell.regime.value)) seen.set(cell.regime.value, cell.regime);
  }
  const index = (value: string) => {
    const at = FALLBACK_REGIME_ORDER.indexOf(value);
    return at === -1 ? FALLBACK_REGIME_ORDER.length : at;
  };
  return [...seen.values()].sort((a, b) => index(a.value) - index(b.value));
}

interface StrategyRow {
  id: string;
  name: string;
  /** 那一行策略的完整读面对象（格子里的 `strategy`）。行状态与按钮都从它取。 */
  ref: RegimeStrategyRef;
  cells: Map<string, RegimePoolCell>;
}

function rowsOf(pool: RegimePool): StrategyRow[] {
  const rows = new Map<string, StrategyRow>();
  for (const cell of pool.cells) {
    let row = rows.get(cell.strategy.id);
    if (!row) {
      row = {
        id: cell.strategy.id,
        name: cell.strategy.name,
        ref: cell.strategy,
        cells: new Map(),
      };
      rows.set(cell.strategy.id, row);
    }
    row.cells.set(cell.regime.value, cell);
  }
  return [...rows.values()];
}

/** 已退役的行在策略名旁边多一个角标（说的仍是后端给的 `display`）。 */
function RetiredBadge({ strategy }: { strategy: RegimeStrategyRef }) {
  if (strategy.lifecycle.value !== "retired") return null;
  const change = strategy.latest_change;
  const title = change
    ? `机制不再管它：${change.actor_kind.display}·${change.actor_name} 于 ${formatMoment(
        change.at
      )} 退役；${change.reason}`
    : "机制不再管它（is_active=False）。";
  return (
    <span
      data-testid={`retired-badge-${strategy.id}`}
      title={title}
      className="ml-1 bg-bg3 text-text3 px-1 rounded text-[9px] whitespace-nowrap"
    >
      {strategy.lifecycle.display}
    </span>
  );
}

function CellBadges({ cell }: { cell: RegimePoolCell }) {
  return (
    <>
      {cell.needs_review && (
        <span
          data-testid="needs-review-badge"
          title="本策略自己的证据与池化结论方向相反；这一格不产生停用建议"
          className="ml-1 bg-amber/20 text-amber px-1 rounded text-[9px]"
        >
          待复核
        </span>
      )}
      {cell.source.value === "archetype" && (
        <span
          data-testid="archetype-badge"
          title="本策略样本不足，结论借用同原型策略的样本；兜底不产出「适用」"
          className="ml-1 bg-purple/20 text-purple px-1 rounded text-[9px]"
        >
          原型兜底
        </span>
      )}
    </>
  );
}

export interface PoolTableProps {
  pool: RegimePool;
  /** 初始展开的格子 key（测试与深链接用；默认全收起）。 */
  initialExpanded?: Set<string>;
  /**
   * 每一行的动作槽（「删除策略」/「恢复启用」）。**由页面注入**，本组件不碰 API：
   * 给了它就多渲染一列「操作」，不给就与从前逐像素一致（所以读面与写面各自可以单独测）。
   */
  renderStrategyAction?: (strategy: RegimeStrategyRef) => ReactNode;
}

export default function PoolTable({
  pool,
  initialExpanded,
  renderStrategyAction,
}: PoolTableProps) {
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(initialExpanded ?? [])
  );

  const columns = useMemo(() => columnsOf(pool), [pool]);
  const rows = useMemo(() => rowsOf(pool), [pool]);

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  if (!pool.has_generation) {
    return (
      <div
        data-testid="pool-empty"
        className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl p-4 text-xs font-mono"
      >
        <p className="text-text">{pool.display}</p>
        <p className="text-text3 mt-2">
          重算只重建池化表；切片若已陈旧需另行跑{" "}
          <code className="text-text2">recompute_regime_slices</code>
          。重算入口是下方那个「重算池化表」按钮（需要管理员权限）。
        </p>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div
        data-testid="pool-no-cells"
        className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl p-4 text-xs font-mono text-text3"
      >
        这一代池化表没有任何格子：候选策略都没有可池化的切片。
      </div>
    );
  }

  const hasActions = renderStrategyAction !== undefined;
  // 展开行要横跨整行：策略列 + 阶段列（+ 操作列，有动作时）。
  const colSpan = columns.length + 1 + (hasActions ? 1 : 0);

  return (
    <div className="space-y-3">
      <div className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl overflow-hidden">
        <table className="w-full text-xs font-mono" data-testid="pool-table">
          <thead>
            <tr className="text-text3 border-b border-[rgba(255,255,255,0.07)]">
              <th className="text-left py-2.5 px-3 font-semibold">策略</th>
              {columns.map((regime) => (
                <th
                  key={regime.value}
                  className="text-left py-2.5 px-2 font-semibold"
                >
                  {regime.display}
                </th>
              ))}
              {hasActions && (
                <th className="text-left py-2.5 px-2 font-semibold">操作</th>
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const expandedCells = [...row.cells.values()].filter((cell) =>
                expanded.has(cell.key)
              );
              return (
                <RowGroup
                  key={row.id}
                  row={row}
                  columns={columns}
                  expandedCells={expandedCells}
                  expanded={expanded}
                  onToggle={toggle}
                  renderStrategyAction={renderStrategyAction}
                  colSpan={colSpan}
                />
              );
            })}
          </tbody>
        </table>
      </div>

      <Exclusions generation={pool.generation} />
    </div>
  );
}

function RowGroup({
  row,
  columns,
  expandedCells,
  expanded,
  onToggle,
  renderStrategyAction,
  colSpan,
}: {
  row: StrategyRow;
  columns: Labelled[];
  expandedCells: RegimePoolCell[];
  expanded: Set<string>;
  onToggle: (key: string) => void;
  renderStrategyAction?: (strategy: RegimeStrategyRef) => ReactNode;
  colSpan: number;
}) {
  return (
    <>
      <tr
        data-testid={`pool-row-${row.id}`}
        className="border-b border-[rgba(255,255,255,0.04)] align-top"
      >
        <th scope="row" className="text-left py-2 px-3 font-semibold text-text">
          {row.name}
          <span className="text-text3 font-normal"> #{row.id}</span>
          <RetiredBadge strategy={row.ref} />
        </th>
        {columns.map((regime) => {
          const cell = row.cells.get(regime.value);
          if (!cell) {
            return (
              <td key={regime.value} className="py-2 px-2 text-text3">
                —
              </td>
            );
          }
          const tone = STATE_TONE[cell.state.value] ?? "muted";
          const isOpen = expanded.has(cell.key);
          return (
            <td key={regime.value} className="py-1.5 px-1.5">
              <button
                type="button"
                data-testid={`pool-cell-${cell.key}`}
                data-state={cell.state.value}
                data-tone={tone}
                aria-expanded={isOpen}
                onClick={() => onToggle(cell.key)}
                className={`w-full text-left border rounded-md px-2 py-1.5 transition-colors ${TONE_CLASS[tone]} ${
                  isOpen ? "ring-1 ring-[rgba(255,255,255,0.18)]" : ""
                }`}
              >
                <span className="block font-semibold">{cell.state.display}</span>
                {cell.reason.display !== "" && (
                  <span className="block text-[10px] opacity-80">
                    {cell.reason.display}
                  </span>
                )}
                <CellBadges cell={cell} />
              </button>
            </td>
          );
        })}
        {renderStrategyAction !== undefined && (
          <td className="py-2 px-2" data-testid={`pool-row-action-${row.id}`}>
            {renderStrategyAction(row.ref)}
          </td>
        )}
      </tr>
      {expandedCells.map((cell) => (
        <tr
          key={`${cell.key}-detail`}
          data-testid={`pool-cell-detail-${cell.key}`}
          className="border-b border-[rgba(255,255,255,0.04)]"
        >
          <td colSpan={colSpan} className="p-0">
            <PoolCellDetail cell={cell} />
          </td>
        </tr>
      ))}
    </>
  );
}

/**
 * 「看了 N 个回测、用上 M 个，差额去哪了」。
 *
 * 三个键**恒在**（计数为 0 也在，`pool_rebuild.EXCLUSION_KEYS` 保证），所以这里逐键排出来
 * 而不是只排非零项：某个键从 0 变成 3 是「这个原因开始出现」，而只排非零项会让一个键
 * 从有到无时看起来像「它没存在过」。原因码是落库契约，前端不翻译（`no_merge_inputs` /
 * `unusable_capital` / `no_payload` 在 `CONTEXT.md` 里各有一句解释）。
 */
function Exclusions({ generation }: { generation: { exclusions: Record<string, number> } }) {
  const entries = Object.entries(generation.exclusions);
  return (
    <details
      data-testid="pool-exclusions"
      className="bg-bg1 border border-[rgba(255,255,255,0.07)] rounded-xl px-3 py-2 text-xs font-mono"
    >
      <summary className="cursor-pointer text-text2 select-none">
        排除计数（哪些回测没进池化，按原因分开）
      </summary>
      {entries.length === 0 ? (
        <p className="text-text3 mt-1.5">这一代没有排除记录。</p>
      ) : (
        <ul className="mt-1.5 space-y-0.5">
          {entries.map(([reason, total]) => (
            <li key={reason} data-testid={`pool-exclusion-${reason}`}>
              <code className="text-text2">{reason}</code>
              <span className="text-text"> {formatNumber(total, 0)}</span>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}