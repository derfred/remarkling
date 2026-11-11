import { logout, session } from "./session.svelte.js";

export class AuthError extends Error {}

/** Call /api/cache<path>. A 401 forgets the token, which sends the app back to the login form. */
export async function api(path, method = "GET") {
  const r = await fetch("/api/cache" + path, { method, headers: { Authorization: "Bearer " + session.token } });
  if (r.status === 401) {
    logout("That token was not accepted.");
    throw new AuthError("unauthorized");
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
  return r.json();
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
