// The API token, kept in this browser only. Storage can throw (private mode), so every access is guarded.
const KEY = "remarkling.apiToken";

function read() {
  try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
}

export const session = $state({ token: read(), message: "" });

export function login(token) {
  try { localStorage.setItem(KEY, token); } catch {}
  session.token = token;
  session.message = "";
}

export function logout(message = "") {
  try { localStorage.removeItem(KEY); } catch {}
  session.token = "";
  session.message = message;
}
