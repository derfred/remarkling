import { logout, session } from "./session.svelte.js";

export class AuthError extends Error {}

async function request(url, method = "GET") {
  const r = await fetch(url, { method, headers: { Authorization: "Bearer " + session.token } });
  if (r.status === 401) {
    logout("That token was not accepted.");
    throw new AuthError("unauthorized");
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
  return r;
}

/** Call /api/cache<path>. A 401 forgets the token, which sends the app back to the login form. */
export async function api(path, method = "GET") {
  return (await request("/api/cache" + path, method)).json();
}

/** GET /api/docs<path> as JSON. */
export async function docs(path) {
  return (await request("/api/docs" + path)).json();
}

/** GET /api/docs<path> as a blob URL (for <img>, which cannot send the token). Revoke it when done. */
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
 * Save a file from /api/docs<path>, which the server fetches from the tablet. A plain link cannot
 * send the bearer token, so this fetches it and hands the browser a blob URL to save.
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
