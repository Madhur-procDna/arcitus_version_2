type ResultTablePayloadLike = {
  rows: unknown[];
  truncated?: boolean;
  total_row_count?: number;
};

/**
 * Model output often includes a full GFM pipe table under `## Result table`.
 * In that case the scrollable **Result data** panel duplicates the same rows — omit it.
 */
function markdownHintsPartialResultTablePipe(md: string): boolean {
  return /\(\s*first\s+\d+\s+of\s+\d+\s+rows?\b/i.test(md);
}

/**
 * Count GFM pipe-table **data** rows after the first `## Result table` heading
 * (excludes header + alignment row). Returns 0 if no such table.
 */
export function countPipeTableBodyRowsAfterResultTableHeading(md: string): number {
  const headingMatch = md.match(/##\s*Result table\b[^\n]*/i);
  if (!headingMatch || headingMatch.index === undefined) return 0;

  const fromHeading = md.slice(headingMatch.index + headingMatch[0].length);
  const lines = fromHeading.split(/\r?\n/);

  let i = 0;
  while (i < lines.length) {
    const t = lines[i].trim();
    if (t.startsWith('|')) break;
    if (/^#{1,6}\s/.test(t)) return 0;
    i++;
  }
  if (i >= lines.length) return 0;

  i += 1;
  if (i >= lines.length) return 0;

  const sep = lines[i].trim();
  if (!sep.startsWith('|') || !/\|\s*:?-{3,}/.test(sep)) return 0;

  i += 1;
  let body = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (/^#{1,6}\s/.test(trimmed)) break;
    if (!trimmed.startsWith('|')) break;
    if (trimmed.startsWith('|') && /\|\s*:?-{3,}/.test(trimmed)) break;
    body += 1;
    i += 1;
  }
  return body;
}

/** When true, skip {@link ResultTablePanel} — markdown already lists every row in the payload. */
export function shouldOmitResultTablePanelAsMarkdownDuplicate(
  markdown: string,
  table: ResultTablePayloadLike,
): boolean {
  const n = table.rows.length;
  if (n <= 0) return false;
  if (table.truncated === true) return false;
  if (markdownHintsPartialResultTablePipe(markdown)) return false;
  const total =
    typeof table.total_row_count === 'number' && table.total_row_count > 0
      ? table.total_row_count
      : n;
  // Markdown only shows a preview; more rows exist for CSV — keep the download panel.
  if (total > n) return false;

  const pipeBodyRows = countPipeTableBodyRowsAfterResultTableHeading(markdown);
  return pipeBodyRows >= n;
}
