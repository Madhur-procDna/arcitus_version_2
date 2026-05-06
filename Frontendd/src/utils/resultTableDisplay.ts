/**
 * Display-only hint for how many table rows to show before "View remaining records".
 * Does not affect queries or API payloads.
 *
 * - Explicit "top N" / "first N" / "topN" / superlative + N: use **N** (capped) so the grid
 *   can show the full requested slice without an expander when the payload has ≤ N rows.
 * - Bare ranking ("top", "most", …) without a number: preview **1** row (large result sets).
 * - Otherwise: preview **10** rows.
 */
const MAX_PREVIEW_ROWS = 500;

const ENGLISH_COUNT_WORDS: Record<string, number> = {
  one: 1,
  two: 2,
  three: 3,
  four: 4,
  five: 5,
  six: 6,
  seven: 7,
  eight: 8,
  nine: 9,
  ten: 10,
  eleven: 11,
  twelve: 12,
  thirteen: 13,
  fourteen: 14,
  fifteen: 15,
  sixteen: 16,
  seventeen: 17,
  eighteen: 18,
  nineteen: 19,
  twenty: 20,
};

const ENGLISH_COUNT_ALT = Object.keys(ENGLISH_COUNT_WORDS).sort((a, b) => b.length - a.length).join('|');

function englishCountWordToInt(w: string): number | undefined {
  const v = ENGLISH_COUNT_WORDS[w.trim().toLowerCase()];
  return v === undefined ? undefined : v;
}

function clampInt(n: number, lo: number, hi: number): number {
  if (!Number.isFinite(n) || Number.isNaN(n)) return lo;
  return Math.max(lo, Math.min(hi, Math.trunc(n)));
}

export function inferResultTablePreviewRowLimit(question: string): number {
  const q = question.trim();
  if (!q) return 10;

  // "Which 10 patients … highest …" — match N before bare "highest/top" rules fire.
  const whichShowList = q.match(
    /\b(?:which|show|list|find|get|tell)\s+(?:me\s+)?(\d{1,6})\b/i,
  );
  if (whichShowList) {
    return clampInt(parseInt(whichShowList[1], 10), 1, MAX_PREVIEW_ROWS);
  }
  const whichShowListWord = q.match(
    new RegExp(
      `\\b(?:which|list|find|get|tell)\\s+(?:me\\s+)?(${ENGLISH_COUNT_ALT})\\b`,
      'i',
    ),
  );
  if (whichShowListWord) {
    const v = englishCountWordToInt(whichShowListWord[1]);
    if (v !== undefined) return clampInt(v, 1, MAX_PREVIEW_ROWS);
  }
  const giveMe = q.match(/\bgive\s+(?:me\s+)?(\d{1,6})\b/i);
  if (giveMe) {
    return clampInt(parseInt(giveMe[1], 10), 1, MAX_PREVIEW_ROWS);
  }

  const spacedTopFirst = q.match(/\b(?:top|first|last|bottom)\s+(\d{1,6})\b/i);
  if (spacedTopFirst) {
    return clampInt(parseInt(spacedTopFirst[1], 10), 1, MAX_PREVIEW_ROWS);
  }

  const spacedBottomWord = q.match(new RegExp(`\\bbottom\\s+(${ENGLISH_COUNT_ALT})\\b`, 'i'));
  if (spacedBottomWord) {
    const v = englishCountWordToInt(spacedBottomWord[1]);
    if (v !== undefined) return clampInt(v, 1, MAX_PREVIEW_ROWS);
  }

  const spacedRankWord = q.match(
    new RegExp(`\\b(?:top|first|last|which)\\s+(${ENGLISH_COUNT_ALT})\\b`, 'i'),
  );
  if (spacedRankWord) {
    const v = englishCountWordToInt(spacedRankWord[1]);
    if (v !== undefined) return clampInt(v, 1, MAX_PREVIEW_ROWS);
  }

  const compactTop = q.match(/\btop\s*(\d{1,6})\b/i);
  if (compactTop) {
    return clampInt(parseInt(compactTop[1], 10), 1, MAX_PREVIEW_ROWS);
  }

  const compactFirst = q.match(/\bfirst\s*(\d{1,6})\b/i);
  if (compactFirst) {
    return clampInt(parseInt(compactFirst[1], 10), 1, MAX_PREVIEW_ROWS);
  }

  const superlativeWithCount = q.match(
    /\b(?:most|highest|lowest|best|worst)\s+(\d{1,6})\b/i,
  );
  if (superlativeWithCount) {
    return clampInt(parseInt(superlativeWithCount[1], 10), 1, MAX_PREVIEW_ROWS);
  }

  const nPatients = q.match(
    /\b(\d{1,6})\s+(?:patients?|hcps?|rows?|records?|results?|reps?|drugs?|molecules?|subjects?)\b/i,
  );
  if (nPatients) {
    return clampInt(parseInt(nPatients[1], 10), 1, MAX_PREVIEW_ROWS);
  }

  const nPatientsWord = q.match(
    new RegExp(
      `\\b(${ENGLISH_COUNT_ALT})\\s+(?:patients?|hcps?|rows?|records?|results?|reps?|drugs?|molecules?|subjects?)\\b`,
      'i',
    ),
  );
  if (nPatientsWord) {
    const v = englishCountWordToInt(nPatientsWord[1]);
    if (v !== undefined) return clampInt(v, 1, MAX_PREVIEW_ROWS);
  }

  if (/\b(?:the\s+)?(?:top|most|highest|lowest|best|worst)\b/i.test(q)) {
    return 1;
  }

  return 10;
}
