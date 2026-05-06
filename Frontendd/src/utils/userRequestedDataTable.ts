/**
 * True when the user clearly asked for a tabular / grid view (show HTML/markdown table).
 * Otherwise we prefer NL bullets + chart + CSV download.
 */
export function userRequestedDataTable(question: string | undefined): boolean {
  if (!question?.trim()) return false;
  const s = question.toLowerCase();
  return (
    /\b(show|display|give|print|export)\s+(me\s+)?(the\s+)?(a\s+)?table\b/.test(s) ||
    /\b(as|in)\s+a\s+table\b/.test(s) ||
    /\btable\s+(format|view|output|layout)\b/.test(s) ||
    /\bdata\s+table\b/.test(s) ||
    /\btabular\b/.test(s) ||
    /\bin\s+columns?\s+and\s+rows?\b/.test(s) ||
    /\bmarkdown\s+table\b/.test(s) ||
    /\bgrid\s+(view|format)\b/.test(s)
  );
}
