/**
 * True when the user clearly asked for a tabular / grid view.
 */
export function userRequestedDataTable(question: string | undefined): boolean {
  if (!question?.trim()) return false;
  const s = question.toLowerCase();
  return (
    /\b(show|display|give|print|export)\s+(me\s+)?(the\s+)?(a\s+)?table\b/.test(s) ||
    /\b(as|in)\s+(a\s+)?(the\s+)?table\b/.test(s) ||
    /\btable\s+(format|view|output|layout)\b/.test(s) ||
    /\bdata\s+table\b/.test(s) ||
    /\btabular\b/.test(s) ||
    /\bin\s+columns?\s+and\s+rows?\b/.test(s) ||
    /\bmarkdown\s+table\b/.test(s) ||
    /\bgrid\s+(view|format)\b/.test(s)
  );
}

/**
 * True when the user explicitly asked for a specific chart type.
 */
export function userRequestedChart(question: string | undefined): boolean {
  if (!question?.trim()) return false;
  const s = question.toLowerCase();
  return (
    /\b(pie|bar|line|scatter)\s+chart\b/.test(s) ||
    /\bshow\s+(as\s+a?\s*)?(pie|bar|line|scatter)?\s*chart\b/.test(s) ||
    /\bas\s+a?\s*(pie|bar|line|scatter)?\s*chart\b/.test(s)
  );
}

/**
 * True when the message is ONLY a display-format request with no new data question.
 * Uses presence-of-keywords + absence-of-data-words approach.
 */
export function isFormatChangeOnly(question: string | undefined): boolean {
  if (!question?.trim()) return false;
  const s = question.toLowerCase().trim();

  // Must contain at least one format keyword
  const hasFormatKw =
    /\btable\b/.test(s) ||
    /\btabular\b/.test(s) ||
    /\bgrid\b/.test(s) ||
    /\bchart\b/.test(s) ||
    /\bpie\b/.test(s) ||
    /\bbar\b/.test(s) ||
    /\bline\b/.test(s) ||
    /\bscatter\b/.test(s);

  if (!hasFormatKw) return false;

  // Must NOT contain data/entity words that indicate a new question
  const hasDataWords =
    /\bhcp\b/.test(s) ||
    /\btrx\b/.test(s) ||
    /\bprescri/.test(s) ||
    /\bdecile\b/.test(s) ||
    /\bterritor/.test(s) ||
    /\bregion\b/.test(s) ||
    /\bstate\b/.test(s) ||
    /\bdrug\b/.test(s) ||
    /\bzoryve\b/.test(s) ||
    /\bmarket\b/.test(s) ||
    /\bsales\b/.test(s) ||
    /\bspecialt/.test(s) ||
    /\bwho\b/.test(s) ||
    /\bwhat\b/.test(s) ||
    /\bhow\b/.test(s) ||
    /\bwhere\b/.test(s);

  if (hasDataWords) return false;

  // Short message with no data words = format-only
  const wordCount = s.split(/\s+/).filter(Boolean).length;
  return wordCount <= 12 || userRequestedDataTable(s) || userRequestedChart(s);
}

/**
 * True when the message references the previous query's results via "these", "this data", etc.
 * E.g., "top 5 of these as pie chart", "show these as table", "these as bar chart"
 */
export function isContextReferenceRequest(question: string | undefined): boolean {
  if (!question?.trim()) return false;
  const s = question.toLowerCase().trim();
  return (
    /\b(of|from|with)\s+these\b/.test(s) ||
    /\bthese\s+as\b/.test(s) ||
    /\bshow\s+these\b/.test(s) ||
    /\bthis\s+data\b/.test(s) ||
    /\bfrom\s+this\b/.test(s) ||
    /\b(top|first|last)\s+\d+\s+of\s+these\b/.test(s) ||
    /\bfrom\s+the\s+(above|previous|last|prior)\b/.test(s) ||
    /\b(above|previous|last|prior)\s+(data|results?|list)\b/.test(s)
  );
}

/**
 * Extract "top N" number from a message like "top 5 of these".
 */
export function extractTopNFromText(question: string | undefined): number | null {
  if (!question?.trim()) return null;
  const m = question.match(/\btop\s*(\d{1,3})\b/i) || question.match(/\bfirst\s*(\d{1,3})\b/i);
  if (m) return Math.max(1, Math.min(500, parseInt(m[1], 10)));
  return null;
}

/**
 * Extract chart type keyword from a message.
 */
export function extractChartTypeFromText(question: string | undefined): 'bar' | 'line' | 'pie' | 'scatter' | null {
  if (!question?.trim()) return null;
  const s = question.toLowerCase();
  if (/\bpie\b/.test(s)) return 'pie';
  if (/\bbar\b/.test(s)) return 'bar';
  if (/\bline\b/.test(s)) return 'line';
  if (/\bscatter\b/.test(s)) return 'scatter';
  return null;
}
