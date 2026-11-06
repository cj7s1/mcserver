/**
 * Fuzzy matching for the command palette (used as cmdk's `filter`).
 *
 * Every whitespace-separated query token must match one of the entry's keywords, in any order
 * ("console vanilla" finds "Vanilla Test › Console"). A token scores highest as a prefix, then as a
 * substring at a word start, then anywhere, then as an in-order subsequence ("bkp" → "backups").
 * The first keyword is the entry's label and weighs slightly more than the others.
 */

export function normalizeText(s: string): string {
  return s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
}

function isWordStart(text: string, i: number): boolean {
  return i === 0 || !/[\p{L}\p{N}]/u.test(text[i - 1]);
}

function subsequenceScore(token: string, text: string): number {
  let best = 0;
  for (let start = text.indexOf(token[0]); start !== -1; start = text.indexOf(token[0], start + 1)) {
    let prev = start;
    let gaps = 0;
    let jumpsToWordStart = 0;
    let matched = 1;
    for (; matched < token.length; matched++) {
      const next = text.indexOf(token[matched], prev + 1);
      if (next === -1) break;
      if (next !== prev + 1) {
        gaps++;
        if (isWordStart(text, next)) jumpsToWordStart++;
      }
      prev = next;
    }
    // Later start positions have even fewer characters left, so they cannot match either.
    if (matched < token.length) break;
    const density = token.length / (prev - start + 1);
    // Scattered letters that do not land on word starts are noise ("cons" in "account & password").
    if (density < 0.5 && jumpsToWordStart < gaps) continue;
    const score = 0.25 + 0.3 * density + (isWordStart(text, start) ? 0.1 : 0) + (gaps ? 0.05 * (jumpsToWordStart / gaps) : 0.05);
    best = Math.max(best, score);
  }
  return Math.min(best, 0.7);
}

/** Score one normalized token against one normalized text: 0 means no match, 1 is a prefix match. */
export function scoreToken(token: string, text: string): number {
  if (!token) return 1;
  if (!text) return 0;
  const idx = text.indexOf(token);
  if (idx === 0) return 1;
  if (idx > 0) {
    for (let i = idx; i !== -1; i = text.indexOf(token, i + 1)) {
      if (isWordStart(text, i)) return 0.9;
    }
    return 0.8;
  }
  return token.length > 1 ? subsequenceScore(token, text) : 0;
}

/** Score a query against an entry's keywords (label first). 0 hides the entry; higher sorts first. */
export function fuzzyScore(query: string, keywords: readonly string[] | undefined): number {
  const tokens = normalizeText(query).split(/\s+/).filter(Boolean);
  if (!tokens.length) return 1;
  const texts = (keywords ?? []).map(normalizeText).filter(Boolean);
  if (!texts.length) return 0;
  let total = 0;
  for (const token of tokens) {
    let best = 0;
    texts.forEach((text, i) => {
      const s = scoreToken(token, text) * (i === 0 ? 1 : 0.92);
      if (s > best) best = s;
    });
    if (best === 0) return 0;
    total += best;
  }
  return total / tokens.length;
}

/**
 * Order groups by their best-matching item for `query` (stable for ties). cmdk sorts items inside
 * a group but leaves the groups in render order, so the palette orders them itself.
 */
export function orderGroups<G extends { keywords: readonly (readonly string[])[] }>(groups: readonly G[], query: string): G[] {
  return groups
    .map((g, index) => ({ g, index, best: g.keywords.reduce((m, k) => Math.max(m, fuzzyScore(query, k)), 0) }))
    .sort((a, b) => b.best - a.best || a.index - b.index)
    .map((x) => x.g);
}
