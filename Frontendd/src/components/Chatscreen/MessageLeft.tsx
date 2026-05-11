'use client';
import React, { useMemo, useState } from 'react';
import { AiOutlineStar, AiFillStar } from 'react-icons/ai';
import { FiCopy } from 'react-icons/fi';
import Image from 'next/image';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import styles from './MessageLeft.module.css';
import {
  QueryResultChart,
  type ChartPayload,
  type ChartRecommendation,
} from './QueryResultChart';
import { ResultTablePanel, type ResultTablePayload } from './ResultTablePanel';
import { shouldOmitResultTablePanelAsMarkdownDuplicate } from '@/utils/markdownResultTable';
import { downloadResultTableCsv } from '@/utils/resultTableCsv';
import { userRequestedDataTable, userRequestedChart, isFormatChangeOnly } from '@/utils/userRequestedDataTable';

export interface AssistantMessageMeta {
  cacheHit?: boolean;
  durationMs?: number;
  /** Last generated SQL — used client-side to anchor follow-ups when server memory is empty. */
  sql?: string;
  /** True when the API returned a logical or transport error (shown inline, no thrown error). */
  failed?: boolean;
  /** Bar/pie/line chart when the API returned a chart payload (explicit "chart" request and/or auto-chart). */
  chart?: ChartPayload;
  /** Structured chart recommendation from LLM JSON response (Task 4 format). */
  chart_recommendation?: ChartRecommendation;
  /** Tabular query result for column/row preview and CSV download. */
  result_table?: ResultTablePayload;
  /** Structured data table rows from LLM JSON response. */
  data_table?: Record<string, unknown>[] | null;
  result_table_multipart_last_part?: boolean;
  /** Pipeline row count from API — used only for summaries vs. in-memory rows. */
  row_count?: number;
  /** How many rows to show before expand (explicit N from question, else 10, or 1 for bare "top/most"). */
  result_display_preview_rows?: number;
  clarification_needed?: string | null;
}

interface MessageLeftProps {
  content: string;
  isLoading?: boolean;
  meta?: AssistantMessageMeta;
  /** Last user message before this assistant reply — used to detect "show as table" intent. */
  pairedUserQuestion?: string;
  onSubmitClarification?: (text: string) => void;
  /** Called when the user clicks the regenerate button — re-submits the paired question bypassing cache. */
  onRegenerate?: () => void;
}

function formatCell(v: unknown): string {
  if (v == null) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** Replaces a markdown data table when CSV is available: one NL line per row (no HTML grid). */
function ResultRowsNlPreview({
  columns,
  rows,
  maxRows = 30,
}: {
  columns: string[];
  rows: Record<string, unknown>[];
  maxRows?: number;
}) {
  const slice = rows.slice(0, Math.max(1, maxRows));
  if (!slice.length) return null;
  return (
    <div className="mt-3 rounded-lg border border-gray-100 bg-gray-50/60 px-3 py-2.5 text-sm text-gray-800">
      <p className="m-0 mb-2 text-xs font-medium text-black">
        Row-by-row view — full spreadsheet: use <span className="font-semibold text-black">Download CSV</span>{' '}
        below.
      </p>
      <ul className="m-0 list-none space-y-2 p-0">
        {slice.map((row, idx) => (
          <li
            key={idx}
            className="border-b border-gray-200/80 pb-2 text-[13px] leading-snug last:border-0 last:pb-0"
          >
            {columns.map((c) => `${c}: ${formatCell(row[c])}`).join(' · ')}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Build a ResultTablePayload from data_table rows (from structured LLM JSON response).
 * Enables CSV download for LLM-structured table data even without a full result_table payload.
 */
function buildResultTableFromDataTable(
  dataTable: Record<string, unknown>[] | null | undefined,
): ResultTablePayload | null {
  if (!dataTable || dataTable.length === 0) return null;
  const columns = Object.keys(dataTable[0]);
  if (!columns.length) return null;
  return { columns, rows: dataTable };
}

const MessageLeft: React.FC<MessageLeftProps> = ({
  content,
  isLoading = false,
  meta,
  pairedUserQuestion,
  onSubmitClarification,
  onRegenerate,
}) => {
  const [clarificationText, setClarificationText] = useState('');
  const hasClarification =
    typeof meta?.clarification_needed === 'string' &&
    meta.clarification_needed.trim().length > 0;
  const [rating, setRating] = useState(0);
  const [hoveredRating, setHoveredRating] = useState(0);
  const [copied, setCopied] = useState(false);
  const [thumbsFeedback, setThumbsFeedback] = useState<'up' | 'down' | null>(null);
  const [thumbsToast, setThumbsToast] = useState<string | null>(null);

  // Resolve result table: prefer backend result_table, fall back to data_table from LLM JSON
  const resultTable =
    meta?.result_table ??
    (meta?.data_table ? buildResultTableFromDataTable(meta.data_table) : undefined);

  const hasResultTablePayload =
    !!resultTable &&
    resultTable.columns.length > 0 &&
    resultTable.rows.length > 0;

  const csvDownloadRowCount = resultTable
    ? Math.max(
        resultTable.rows.length,
        typeof resultTable.total_row_count === 'number' ? resultTable.total_row_count : 0,
        typeof meta?.row_count === 'number' && meta.row_count > 0 ? meta.row_count : 0,
      )
    : 0;
  const omitResultTablePanelDuplicate =
    hasResultTablePayload &&
    shouldOmitResultTablePanelAsMarkdownDuplicate(content, resultTable);
  const omitResultTablePanel = omitResultTablePanelDuplicate;

  const wantExplicitTable = userRequestedDataTable(pairedUserQuestion) || 
    (pairedUserQuestion ? isFormatChangeOnly(pairedUserQuestion) && !userRequestedChart(pairedUserQuestion) : false);
  const wantExplicitChart = userRequestedChart(pairedUserQuestion);
  const replaceMarkdownTableWithNl =
    hasResultTablePayload && resultTable && !wantExplicitTable;

  // Determine whether to show the chart:
  // - if explicitly requested a table → no chart
  // - if explicitly requested a chart → force chart
  // - if chart_recommendation.show_chart is explicitly false → never show
  // - if we have a legacy chart payload → show it
  // - if we have chart_recommendation + data_table → let QueryResultChart resolve
  const showChart = useMemo(() => {
    if (hasClarification) return false;
    if (wantExplicitTable) return false;
    if (wantExplicitChart) return true;
    const rec = meta?.chart_recommendation;
    if (rec && rec.show_chart === false) return false;
    if (meta?.chart && Array.isArray(meta.chart.data) && meta.chart.data.length > 1) return true;
    if (rec && rec.show_chart && rec.chart_type !== 'none') return true;
    return false;
  }, [hasClarification, wantExplicitTable, wantExplicitChart, meta?.chart, meta?.chart_recommendation]);

  const markdownComponents = useMemo(() => {
    const base = {
      h3: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
        <h3 className="mt-4 mb-1 text-[15px] font-semibold text-[#0b5fa5] border-b border-[#0b5fa5]/20 pb-0.5" {...props} />
      ),
      h4: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
        <h4 className="mt-3 mb-1 text-sm font-semibold text-[#0b5fa5]" {...props} />
      ),
      p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
        <p className="mb-2 leading-6 text-gray-800" {...props} />
      ),
      strong: (props: React.HTMLAttributes<HTMLElement>) => (
        <strong className="font-semibold text-gray-900" {...props} />
      ),
    };
    if (!replaceMarkdownTableWithNl || !resultTable) return base;
    const cols = resultTable.columns;
    const rws = resultTable.rows;
    return {
      ...base,
      table: () => (
        <ResultRowsNlPreview
          columns={cols}
          rows={rws}
          maxRows={meta?.result_display_preview_rows ?? 30}
        />
      ),
    };
  }, [
    replaceMarkdownTableWithNl,
    resultTable,
    meta?.result_display_preview_rows,
  ]);

  const handleRatingClick = (star: number) => {
    setRating(star);
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const handleThumbsUp = () => {
    setThumbsFeedback('up');
    setThumbsToast('Glad you liked it! Let me know what else you\'d like to explore.');
    setTimeout(() => setThumbsToast(null), 3000);
  };

  const handleThumbsDown = () => {
    setThumbsFeedback('down');
    setThumbsToast('Sorry this wasn\'t helpful — could you tell me what you were expecting?');
    setTimeout(() => setThumbsToast(null), 4000);
  };

  // Only render loading state if there's no content
  if (isLoading && (!content || content.trim() === '')) {
    return (
      <div className="flex justify-start mb-6">
        <div className="flex-shrink-0">
          <div className="w-[44px] h-[44px] mb-2 bg-white rounded-full flex items-center justify-center mr-3 shadow-sm border border-gray-100 overflow-hidden">
            <Image
              src="/Images/BotIconInsightSphere.svg"
              alt="Assistant"
              width={40}
              height={40}
              className="h-10 w-10 object-contain p-0.5"
            />
          </div>
        </div>
        <div className="max-w-[70%]">
          <div className="flex items-start space-x-3">
            <div className="flex-1">
              <div className="bg-white rounded-[20px] rounded-tl-[4px] px-5 py-3.5 shadow-sm border border-gray-100">
                <div className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-2 h-2 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-2 h-2 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Render message with content
  return (
    <div className="flex justify-start mb-2">
      <div className="flex-shrink-0">
        <div className="w-[44px] h-[44px] mb-2 bg-white rounded-full flex items-center justify-center mr-3 shadow-sm border border-gray-100 overflow-hidden">
          <Image
            src="/Images/BotIconInsightSphere.svg"
            alt="Assistant"
            width={40}
            height={40}
            className="h-10 w-10 object-contain p-0.5"
          />
        </div>
      </div>
      <div className="max-w-[70%]">
        <div className="flex items-start space-x-3">
          <div className="flex-1">
            <div className="bg-white rounded-[20px] rounded-tl-[4px] px-6 py-4 shadow-sm border border-gray-100">
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
                <div
                  className={`${styles.messageMarkdownContent} text-content text-sm leading-[22px] font-normal w-full`}
                  style={{
                    wordWrap: 'break-word',
                    overflowWrap: 'break-word',
                  }}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                    {content}
                  </ReactMarkdown>

                  {/* Standalone CSV Download Button immediately after text */}
                  {hasResultTablePayload && resultTable ? (
                    <div className="mt-2 mb-2 flex flex-wrap justify-start gap-2 w-full">
                      <button
                        type="button"
                        onClick={() => {
                          // Always use the full API rows — never the preview-sliced subset
                          const allRows = meta?.result_table?.rows ?? resultTable.rows;
                          console.log('Total rows in CSV:', allRows.length);
                          downloadResultTableCsv(resultTable.columns, allRows);
                        }}
                        className="shrink-0 rounded-lg border border-[#0b5fa5] bg-white px-3 py-1.5 text-[13px] font-semibold text-[#0b5fa5] hover:bg-[#0b5fa5] hover:text-white transition-colors"
                      >
                        ⬇ Download Data (CSV) —{' '}
                        {csvDownloadRowCount.toLocaleString('en-US')}{' '}
                        {csvDownloadRowCount === 1 ? 'row' : 'rows'}
                      </button>
                    </div>
                  ) : null}

                  {/* Clarification prompt — light blue rounded card */}
                  {hasClarification ? (
                    <div className="mt-4 rounded-xl border border-sky-200 bg-sky-50 p-4 shadow-sm">
                      <p className="m-0 mb-3 text-sm font-medium text-sky-900">
                        {meta?.clarification_needed}
                      </p>
                      <div className="flex gap-2">
                        <input
                          type="text"
                          value={clarificationText}
                          onChange={(e) => setClarificationText(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                              const t = clarificationText.trim();
                              if (!t || !onSubmitClarification) return;
                              onSubmitClarification(t);
                              setClarificationText('');
                            }
                          }}
                          className="flex-1 rounded-lg border border-sky-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-sky-400 focus:ring-1 focus:ring-sky-200 transition-colors"
                          placeholder="Type your clarification..."
                          aria-label="Clarification input"
                        />
                        <button
                          type="button"
                          className="rounded-lg bg-[#0b5fa5] px-4 py-2 text-xs font-semibold text-white hover:bg-[#0a4e87] transition-colors disabled:opacity-50"
                          disabled={!clarificationText.trim()}
                          onClick={() => {
                            const t = clarificationText.trim();
                            if (!t || !onSubmitClarification) return;
                            onSubmitClarification(t);
                            setClarificationText('');
                          }}
                        >
                          Send
                        </button>
                      </div>
                    </div>
                  ) : null}

                  {/* Chart — only when not blocked by clarification */}
                  {showChart ? (
                    <QueryResultChart
                      chart={meta?.chart}
                      chartRecommendation={meta?.chart_recommendation}
                      dataTable={meta?.data_table || resultTable?.rows}
                    />
                  ) : null}

                  {/* Result table panel — ONLY when user explicitly asked for a table */}
                  {!hasClarification && hasResultTablePayload && !omitResultTablePanel && !wantExplicitChart && wantExplicitTable ? (
                    <ResultTablePanel
                      table={resultTable}
                      multipartLastPart={meta?.result_table_multipart_last_part}
                      previewRowLimit={meta?.result_display_preview_rows ?? 10}
                      pipelineRowCount={meta?.row_count}
                    />
                  ) : null}
                </div>
                {(meta?.cacheHit === true ||
                  meta?.cacheHit === false ||
                  meta?.durationMs != null) && (
                  <div className="w-full flex justify-end">
                    <p className="text-[11px] text-gray-400 leading-tight">
                      {meta?.cacheHit === true ? 'Loaded from cache' : 'Fresh query'}
                      {typeof meta?.durationMs === 'number' ? ` · ${meta.durationMs} ms` : ''}
                    </p>
                  </div>
                )}
                {/* Show loader next to content if still streaming */}
                {isLoading && (
                  <div className="flex items-center gap-1 pl-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                )}
              </div>
            </div>

            {/* Rating and Copy Controls */}
            <div className="relative mt-2 flex items-center px-6 gap-3">
              {/* Star rating */}
              <div className="flex space-x-1">
                {[1, 2, 3, 4, 5].map((star) => (
                  <button
                    key={star}
                    onClick={() => handleRatingClick(star)}
                    onMouseEnter={() => setHoveredRating(star)}
                    onMouseLeave={() => setHoveredRating(0)}
                    className="cursor-pointer transition-opacity text-black"
                    aria-label={`Rate ${star} star${star > 1 ? 's' : ''}`}
                  >
                    {hoveredRating ? (
                      star <= hoveredRating ? (
                        <AiFillStar size={18} color="#f2d322" />
                      ) : (
                        <AiOutlineStar size={18} />
                      )
                    ) : rating >= star ? (
                      <AiFillStar size={18} color="#f2d322" />
                    ) : (
                      <AiOutlineStar size={18} />
                    )}
                  </button>
                ))}
              </div>

              {/* Divider */}
              <span className="text-gray-200 select-none">|</span>

              {/* Thumbs up / down */}
              <div className="flex items-center gap-1">
                <button
                  onClick={handleThumbsUp}
                  title="Helpful"
                  aria-label="Thumbs up"
                  className={`text-lg transition-transform hover:scale-110 ${
                    thumbsFeedback === 'up' ? 'opacity-100' : 'opacity-50 hover:opacity-100'
                  }`}
                >
                  👍
                </button>
                <button
                  onClick={handleThumbsDown}
                  title="Not helpful"
                  aria-label="Thumbs down"
                  className={`text-lg transition-transform hover:scale-110 ${
                    thumbsFeedback === 'down' ? 'opacity-100' : 'opacity-50 hover:opacity-100'
                  }`}
                >
                  👎
                </button>
              </div>

              {/* Regenerate button — bypasses cache and re-runs the question fresh */}
              {onRegenerate && !isLoading && (
                <button
                  onClick={onRegenerate}
                  title="Regenerate response (bypass cache)"
                  aria-label="Regenerate"
                  className="flex cursor-pointer items-center gap-1 text-gray-400 hover:text-blue-600 text-xs transition-colors"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
                    <path d="M21 3v5h-5"/>
                    <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
                    <path d="M8 16H3v5"/>
                  </svg>
                </button>
              )}

              {/* Copy button */}
              <button
                onClick={handleCopy}
                className="flex cursor-pointer items-center space-x-1 text-black hover:text-black text-xs"
                aria-label="Copy message"
              >
                <FiCopy />
              </button>

              {/* Toasts */}
              {copied && (
                <div className={styles.copiedTooltip}>
                  Copied to clipboard
                </div>
              )}
              {thumbsToast && (
                <div className="absolute left-6 -top-8 bg-gray-800 text-white text-xs px-3 py-1.5 rounded-lg shadow-lg whitespace-nowrap z-10 animate-fade-in">
                  {thumbsToast}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default MessageLeft;
