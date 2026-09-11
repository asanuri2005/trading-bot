export const byReturn = (books) => [...books].sort((a, b) => {
  const x = Number.isFinite(a.cum_ret) ? a.cum_ret : -Infinity;
  const y = Number.isFinite(b.cum_ret) ? b.cum_ret : -Infinity;
  return Math.abs(y - x) < 1e-10 || y === x ? a.name.localeCompare(b.name) : y - x;
});

export function percentagePoints(gap) {
  return `${(gap * 100).toFixed(2)} pp`;
}
