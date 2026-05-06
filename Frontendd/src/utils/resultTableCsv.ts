export function escapeCsvCell(v: unknown): string {
  const s = v === null || v === undefined ? '' : String(v);
  if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

export function resultTableToCsv(columns: string[], rows: Record<string, unknown>[]): string {
  const header = columns.map(escapeCsvCell).join(',');
  const lines = rows.map((row) => columns.map((c) => escapeCsvCell(row[c])).join(','));
  return [header, ...lines].join('\r\n');
}

export function downloadResultTableCsv(columns: string[], rows: Record<string, unknown>[]): void {
  const csv = resultTableToCsv(columns, rows);
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `query-result-${Date.now()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
