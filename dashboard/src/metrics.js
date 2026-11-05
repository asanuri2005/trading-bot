export const byReturn = (books) => [...books].sort((a, b) => {
  const x = Number.isFinite(a.cum_ret) ? a.cum_ret : -Infinity;
  const y = Number.isFinite(b.cum_ret) ? b.cum_ret : -Infinity;
  return Math.abs(y - x) < 1e-10 || y === x ? a.name.localeCompare(b.name) : y - x;
});

export function percentagePoints(gap) {
  return `${(gap * 100).toFixed(2)} pp`;
}

const close = (a, b) => Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b));

function sameBook(a, b) {
  const pa = a.positions || {};
  const pb = b.positions || {};
  const symbols = Object.keys(pa);
  if (symbols.length !== Object.keys(pb).length) return false;
  if (symbols.some((s) => !(s in pb) || !close(pa[s], pb[s]))) return false;
  const sa = a.series || [];
  const sb = b.series || [];
  return sa.length > 0 && sa.length === sb.length
    && sa.every((p, i) => p.date === sb[i].date && close(p.cum_ret, sb[i].cum_ret));
}

// Books whose holdings and returns have matched on every recorded day, as
// { name: [names of its twins] }. Dual momentum only departs from plain
// rotation once its cash filter triggers; until then the two are one result
// shown twice, and the page should not present them as independent evidence.
export function identicalBooks(books) {
  const twins = {};
  for (const a of books) {
    for (const b of books) {
      if (a !== b && sameBook(a, b)) (twins[a.name] ||= []).push(b.name);
    }
  }
  return twins;
}

// The leading group (the best book plus any twins) and its lead over the
// best book outside that group. `gap` is null when every book is identical.
export function leaderGap(books, twins = identicalBooks(books)) {
  const ranked = byReturn(books);
  if (!ranked.length) return { leaders: [], gap: null };
  const group = new Set([ranked[0].name, ...(twins[ranked[0].name] || [])]);
  const leaders = ranked.filter((b) => group.has(b.name));
  const runnerUp = ranked.find((b) => !group.has(b.name));
  return { leaders, gap: runnerUp ? (leaders[0].cum_ret || 0) - (runnerUp.cum_ret || 0) : null };
}
