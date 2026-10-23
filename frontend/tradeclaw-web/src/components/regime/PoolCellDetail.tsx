"use client";

/**
 * 池化表格子的依据（行内展开的内容）。
 *
 * 这一块的全部风险是**把「依据」重新解释一遍**：`evidence` 是后端**原样透出**的 JSON
 * （`serializers.cell_payload`：一个键都不改），它是这张表存在的理由——「依据哪几个回测、
 * 多少笔、什么时间区间」以及原型归一化留痕。所以这里的纪律只有两条：
 *
 *   1. **只排版已知的键，不推导、不四舍五入成新结论**。数字一律走 `lib/format.ts`；把
 *      `trades`（合并成交数）当成「每份回测的笔数」就是一次凭空发明——那份数不在证据里，
 *      这里也不编。
 *   2. **认不出来的内容不吞掉**。`evidence` 里将来加键时，页面不该悄悄少显示一段：
 *      末尾的「原始证据」把整份 JSON 原样摊开，审计要逐字比对时有一个地方可看。
 *
 * `state` / `reason` / `source` 的中文一律取后端给的 `display`，前端不写第二份。
 */

import type { RegimePoolCell } from "@/lib/api";
import { formatNumber } from "@/lib/format";

/** JSON 列的读取小件：证据是不可信的 `unknown`（手工补录的行也走这条路）。 */
function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asText(value: unknown): string | null {
  if (typeof value === "string" && value !== "") return value;
  if (typeof value === "number") return String(value);
  return null;
}

function asCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

const count = (value: number | null): string =>
  value === null ? "—" : formatNumber(value, 0);

interface EvidenceResult {
  result_id: string;
  symbol: string | null;
  start: string | null;
  end: string | null;
}

/** `evidence.results` 的每一份切片摘要（`pool._summarise` 的产物）。 */
function evidenceResults(value: unknown): EvidenceResult[] {
  return asList(value).flatMap((entry) => {
    const row = asRecord(entry);
    if (!row) return [];
    const id = asText(row.result_id);
    if (id === null) return [];
    const window = asRecord(row.window);
    return [
      {
        result_id: id,
        symbol: asText(row.symbol),
        start: asText(window?.start),
        end: asText(window?.end),
      },
    ];
  });
}

const windowText = (row: EvidenceResult): string =>
  row.start && row.end ? `${row.start} ~ ${row.end}` : "区间未知";

function Section({
  title,
  testId,
  children,
}: {
  title: string;
  testId?: string;
  children: React.ReactNode;
}) {
  return (
    <section data-testid={testId} className="min-w-0">
      <h4 className="text-[10px] font-bold text-text3 mb-1.5 font-mono">{title}</h4>
      {children}
    </section>
  );
}

export default function PoolCellDetail({ cell }: { cell: RegimePoolCell }) {
  const evidence = asRecord(cell.evidence) ?? {};
  const results = evidenceResults(evidence.results);
  const threshold = asRecord(evidence.threshold);
  const archetype = asRecord(evidence.archetype);
  const ownSample = asRecord(evidence.own_sample);
  const conflict = asRecord(evidence.conflict);

  const trades = asCount(evidence.trades);
  const months = asCount(evidence.months);
  const regimeDays = asCount(evidence.regime_days);
  const symbols = asList(evidence.symbols).flatMap((s) => {
    const name = asText(s);
    return name === null ? [] : [name];
  });

  const minTrades = asCount(threshold?.min_trades);
  const minMonths = asCount(threshold?.min_months);
  const met = typeof threshold?.met === "boolean" ? threshold.met : null;

  const ownSampleResults = ownSample
    ? evidenceResults(ownSample.results)
    : [];
  const ownTrades = ownSample ? asCount(ownSample.trades) : null;
  const ownMonths = ownSample ? asCount(ownSample.months) : null;
  const ownRegimeDays = ownSample ? asCount(ownSample.regime_days) : null;

  return (
    <div
      data-testid="pool-cell-evidence"
      className="bg-bg2/50 border border-[rgba(255,255,255,0.07)] rounded-md m-1 p-3 text-[11px] font-mono"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-3">
        <span className="text-text font-semibold">
          {cell.strategy.name}
          <span className="text-text3 font-normal"> #{cell.strategy.id}</span>
        </span>
        <span className="text-text2">{cell.regime.display}</span>
        <span className="text-text3">·</span>
        <span className="text-text2">{cell.source.display}</span>
        <span className="text-text3">·</span>
        <span className="text-text">{cell.state.display}</span>
        {cell.reason.display !== "" && (
          <span className="text-text2">{cell.reason.display}</span>
        )}
      </div>

      {cell.needs_review && (
        <p data-testid="pool-cell-review-note" className="text-amber mb-3">
          待人工复核：本策略自己的证据与池化结论方向相反，这一格不产生停用建议。
          {conflict && (
            <>
              {" "}
              冲突品种{" "}
              <span className="text-text">
                {asText(conflict.symbol) ?? "—"}
              </span>
              （品种自身结论{" "}
              <span className="text-text">{asText(conflict.symbol_state) ?? "—"}</span>
              ，池化结论{" "}
              <span className="text-text">{asText(conflict.pooled_state) ?? "—"}</span>
              <span className="text-text3">
                {" "}
                · {count(asCount(conflict.symbol_trades))} 笔 /{" "}
                {count(asCount(conflict.symbol_months))} 个月
              </span>
              ）
            </>
          )}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3">
        <Section title="依据的回测结果" testId="pool-evidence-results">
          {results.length === 0 ? (
            <p className="text-text3">证据里没有回测结果。</p>
          ) : (
            <ul className="space-y-0.5">
              {results.map((row) => (
                <li key={row.result_id} className="flex flex-wrap gap-x-2">
                  <span className="text-text">#{row.result_id}</span>
                  <span className="text-text2">{row.symbol ?? "—"}</span>
                  <span className="text-text3">{windowText(row)}</span>
                </li>
              ))}
            </ul>
          )}
        </Section>

        <Section title="合并样本" testId="pool-evidence-sample">
          <ul className="space-y-0.5">
            <li>
              <span className="text-text3">合并成交数</span>{" "}
              <span className="text-text">{count(trades)}</span> 笔
            </li>
            <li>
              <span className="text-text3">覆盖</span>{" "}
              <span className="text-text">{count(months)}</span> 个月
              <span className="text-text3"> · 阶段出现</span>{" "}
              <span className="text-text">{count(regimeDays)}</span> 天
            </li>
            <li>
              <span className="text-text3">品种</span>{" "}
              <span className="text-text">
                {symbols.length === 0 ? "—" : symbols.join("、")}
              </span>
            </li>
            <li>
              <span className="text-text3">门槛</span>{" "}
              <span className="text-text">
                成交 ≥ {count(minTrades)} 且覆盖 ≥ {count(minMonths)} 个月
              </span>
              {met !== null && (
                <span
                  data-testid="pool-evidence-threshold"
                  className={met ? "text-green" : "text-amber"}
                >
                  {" "}
                  · {met ? "已达标" : "未达标"}
                </span>
              )}
            </li>
          </ul>
        </Section>
      </div>

      {archetype && (
        <div className="mt-3" data-testid="pool-evidence-archetype">
          <Section title="原型兜底留痕">
            <ul className="space-y-0.5">
              <li>
                <span className="text-text3">原型</span>{" "}
                <span className="text-text">{asText(archetype.name) ?? "—"}</span>
                <span className="text-text3">
                  {" "}
                  （来源 {asText(archetype.source) ?? "—"}）
                </span>
              </li>
              <li>
                <span className="text-text3">原文字</span>{" "}
                <span data-testid="pool-evidence-raw-text" className="text-text">
                  {asText(archetype.raw_text) ?? "—"}
                </span>
              </li>
              <li>
                <span className="text-text3">命中词</span>{" "}
                <span data-testid="pool-evidence-matched-word" className="text-text">
                  {asText(archetype.matched_word) ?? "—"}
                </span>
              </li>
            </ul>
          </Section>
        </div>
      )}

      {ownSample && (
        <div className="mt-3" data-testid="pool-evidence-own-sample">
          <Section title="本策略自己的样本（不足以单独下结论）">
            <p>
              <span className="text-text3">成交</span>{" "}
              <span className="text-text">{count(ownTrades)}</span> 笔
              <span className="text-text3"> · 覆盖</span>{" "}
              <span className="text-text">{count(ownMonths)}</span> 个月
              <span className="text-text3"> · 阶段出现</span>{" "}
              <span className="text-text">{count(ownRegimeDays)}</span> 天
              <span className="text-text3"> · 回测</span>{" "}
              <span className="text-text">
                {ownSampleResults.length === 0
                  ? "—"
                  : ownSampleResults.map((r) => `#${r.result_id}`).join(" ")}
              </span>
            </p>
          </Section>
        </div>
      )}

      <details className="mt-3" data-testid="pool-evidence-raw">
        <summary className="cursor-pointer text-text3 select-none">
          原始证据（后端原样透出）
        </summary>
        <pre className="mt-1.5 overflow-x-auto text-[10px] text-text2 whitespace-pre-wrap break-all">
          {JSON.stringify(evidence, null, 2)}
        </pre>
      </details>
    </div>
  );
}
