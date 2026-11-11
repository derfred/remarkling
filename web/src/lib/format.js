export function bytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + units[i];
}

export function when(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000), s = (Date.now() - d) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  if (s < 86400 * 7) return Math.floor(s / 86400) + " d ago";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export const fullTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "–");
export const short = (s) => (s ? s.slice(0, 12) + "…" : "–");
export const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
