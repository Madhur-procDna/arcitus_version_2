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
}: ResultTablePanelProps) {
  const { columns, rows } = table;

  const totalRecords = Math.max(
    rows.length,
    typeof table.total_row_count === 'number' ? table.total_row_count : 0,
    typeof pipelineRowCount === 'number' && pipelineRowCount > 0 ? pipelineRowCount : 0,
  );

  const downloadCsv = useCallback(() => {
    downloadResultTableCsv(columns, rows);
  }, [columns, rows]);

  if (!columns.length || !rows.length) return null;

  return (
    <div className="mt-4 flex items-center justify-between rounded-xl border border-gray-200 bg-gray-50/80 px-4 py-3 text-xs text-gray-700">
      <p className="m-0">
        <span className="font-semibold text-gray-800">{formatCount(totalRecords)}</span> record
        {totalRecords === 1 ? '' : 's'} available.{' '}
        <span className="text-gray-500">Download the file to view all results.</span>
      </p>
      <button
        type="button"
        onClick={downloadCsv}
        className="ml-4 shrink-0 rounded-lg border border-gray-300 bg-white px-4 py-2 text-xs font-semibold text-gray-800 hover:bg-gray-50"
      >
        [Download full dataset ({formatCount(totalRecords)} rows) as CSV]
      </button>
    </div>
  );
}
