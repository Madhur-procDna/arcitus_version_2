'use client';

import React, { useCallback } from 'react';
import { downloadResultTableCsv } from '@/utils/resultTableCsv';

export type ResultTablePayload = {
  columns: string[];
  rows: Record<string, unknown>[];
  total_row_count?: number;
  truncated?: boolean;
  truncated_for_storage?: boolean;
  suppress_remaining_rows?: boolean;
};

/**
 * Columns always hidden from the table preview display.
 * These are internal / low-signal fields the user never asks for.
 * The CSV download still includes ALL columns from the API payload.
 */
const ALWAYS_HIDDEN_COLS = new Set([
  'npi_id',
  'secondary_specialty',
  'hco_name',
  'base_territory',
  'region',
  'area',
  'q1_26_target_flag',
  'q1_26_decile',
]);

function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}

export interface ResultTablePanelProps {
  table: ResultTablePayload;
  multipartLastPart?: boolean;
  previewRowLimit?: number;
  pipelineRowCount?: number;
}

export function ResultTablePanel({
  table,
  pipelineRowCount,
  previewRowLimit,
}: ResultTablePanelProps) {
  const { columns, rows } = table;

  // Filter display columns — hide noise fields; CSV always gets the full unfiltered set
  const displayColumns = columns.filter((c) => !ALWAYS_HIDDEN_COLS.has(c.toLowerCase()));

  const totalRecords = Math.max(
    rows.length,
    typeof table.total_row_count === 'number' ? table.total_row_count : 0,
    typeof pipelineRowCount === 'number' && pipelineRowCount > 0 ? pipelineRowCount : 0,
  );

  // CSV always exports ALL rows from the original payload (no preview cap)
  const downloadCsv = useCallback(() => {
    console.log('Total rows in CSV (ResultTablePanel):', rows.length);
    downloadResultTableCsv(columns, rows);
  }, [columns, rows]);

  if (!columns.length || !rows.length) return null;

  const displayRows = previewRowLimit && previewRowLimit < rows.length
    ? rows.slice(0, previewRowLimit)
    : rows;

  const isPreviewTruncated = displayRows.length < rows.length;

  const isNumeric = (value: unknown): boolean => {
    if (typeof value === 'number') return Number.isFinite(value);
    if (typeof value !== 'string') return false;
    const n = Number(value.replace(/,/g, ''));
    return Number.isFinite(n);
  };

  const formatCell = (value: unknown): string => {
    if (value == null) return '—';
    const s = String(value);
    return s.length > 40 ? `${s.slice(0, 40)}…` : s;
  };

  return (
    <div className="mt-4 rounded-xl border border-gray-200 bg-white p-3 shadow-sm">
      {/* Header row: record count + Download CSV button */}
      <div className="mb-3 flex items-center justify-between gap-2">
        <p className="m-0 text-xs text-gray-600">
          {isPreviewTruncated ? (
            <>
              Showing{' '}
              <span className="font-semibold text-gray-900">{formatCount(displayRows.length)}</span>
              {' '}of{' '}
              <span className="font-semibold text-gray-900">{formatCount(totalRecords)}</span>{' '}
              {totalRecords === 1 ? 'result' : 'results'}
            </>
          ) : (
            <>
              Showing{' '}
              <span className="font-semibold text-gray-900">{formatCount(totalRecords)}</span>{' '}
              {totalRecords === 1 ? 'result' : 'results'}
            </>
          )}
        </p>
        <button
          type="button"
          id="result-table-download-csv"
          onClick={downloadCsv}
          className="rounded-lg border border-[#0b5fa5] bg-white px-3 py-1.5 text-xs font-semibold text-[#0b5fa5] hover:bg-[#0b5fa5] hover:text-white transition-colors"
        >
          ⬇ Download CSV ({formatCount(rows.length)}{' '}
          {rows.length === 1 ? 'row' : 'rows'})
        </button>
      </div>

      <div className="overflow-x-auto">
        <table className="min-w-full border-collapse text-xs text-gray-800">
          <thead>
            <tr className="bg-[#0b5fa5]">
              {displayColumns.map((col) => (
                <th
                  key={col}
                  className="border-b border-[#0a4e87] px-3 py-2 text-left font-bold text-white whitespace-nowrap"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((row, idx) => (
              <tr
                key={idx}
                className={
                  idx % 2 === 0
                    ? 'bg-white hover:bg-blue-50/40 transition-colors'
                    : 'bg-gray-50 hover:bg-blue-50/40 transition-colors'
                }
              >
                {displayColumns.map((col) => {
                  const raw = row[col];
                  const text = formatCell(raw);
                  return (
                    <td
                      key={`${idx}-${col}`}
                      title={raw == null ? '' : String(raw)}
                      className={`border-b border-gray-100 px-3 py-2 ${isNumeric(raw) ? 'text-right tabular-nums' : 'text-left'}`}
                    >
                      {text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Truncation / backend-limit notice */}
      {(table.truncated || table.truncated_for_storage || isPreviewTruncated) && (
        <p className="mt-2 text-[11px] text-gray-500">
          {isPreviewTruncated
            ? `* Showing top ${displayRows.length} rows — use `
            : '* Results may be partial — use '}
          <span className="font-medium">Download CSV</span>{isPreviewTruncated ? ` to get all ${rows.length} rows.` : ' to export all rows.'}
        </p>
      )}
    </div>
  );
}
