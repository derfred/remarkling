export class AuthError extends Error {}

// The auth proxy in front of the app owns the session: its cookie rides along on every same-origin
// fetch. A 401, or a redirect to its sign-in page, means the session has lapsed, so reload the page
// and let the proxy sign the user in again. A second failure right after a reload is not a lapsed
// session (e.g. the server lacks API_AUTH_HEADER) and shows as an error instead of looping.
const RELOADED = "remarkling.authReload";

function reauthenticate() {
  let last = 0;
  try { last = Number(sessionStorage.getItem(RELOADED)) || 0; } catch {}
  if (Date.now() - last < 30_000) throw new Error("Not signed in: the server did not accept this session.");
  try { sessionStorage.setItem(RELOADED, String(Date.now())); } catch {}
  location.reload();
  throw new AuthError("unauthorized");
}

async function request(url, method = "GET") {
  const r = await fetch(url, { method, credentials: "same-origin", redirect: "manual" });
  if (r.status === 401 || r.type === "opaqueredirect") reauthenticate();
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
  return r;
}

/** Call /api/cache<path> with the proxy's session. */
export async function api(path, method = "GET") {
  return (await request("/api/cache" + path, method)).json();
}

/** GET /api/docs<path> as JSON. */
export async function docs(path) {
  return (await request("/api/docs" + path)).json();
}

/** GET /api/docs<path> as a blob URL (for <img>; a lapsed session then reloads instead of breaking the image). Revoke it when done. */
export async function blobUrl(path) {
  return URL.createObjectURL(await (await request("/api/docs" + path)).blob());
}

/** The file name a Content-Disposition header gives: filename*= (UTF-8) over filename=. */
function dispositionName(header) {
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header || "");
  if (star) {
    try { return decodeURIComponent(star[1]); } catch {}
  }
  return /filename="([^"]*)"/i.exec(header || "")?.[1] || "download";
}

/**
 * Save a file from /api/docs<path>, which the server fetches from the tablet. Fetching it first
 * (rather than a plain link) keeps the user on the page if the session lapsed or the tablet is offline.
 */
export async function download(path) {
  const r = await request("/api/docs" + path);
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: dispositionName(r.headers.get("Content-Disposition")) });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

// The library is fetched once and reused while navigating folders; reload() and sync drop it.
let library = null;
export async function getLibrary() {
  library ??= api("/library").then((r) => r.items).catch((e) => { library = null; throw e; });
  return library;
}
export function forgetLibrary() {
  library = null;
}
