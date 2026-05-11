export function escapeCsvCell(v: unknown): string {
  let s = v === null || v === undefined ? '' : String(v);
  // Neutralize spreadsheet formula injection when CSV is opened in Excel/Sheets.
  if (/^[=\-+@]/.test(s)) {
    s = `'${s}`;
  }
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
  const d = new Date();
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  a.download = `arcutis_data_${iso}.csv`;
  // Some environments ignore a bare `a.click()` unless the element is attached.
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke after the browser has a chance to start the download.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
