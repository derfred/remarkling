import { api, AuthError, forgetLibrary } from "./api.js";
import { reload } from "./router.svelte.js";
import { showToast } from "./toast.svelte.js";
import { plural } from "./format.js";

export const sync = $state({ busy: false });

/** POST /api/cache/sync: fetch whatever the cache lacks for the current root, then reload the view. */
export async function runSync() {
  if (sync.busy) return;
  sync.busy = true;
  try {
    const r = await api("/sync", "POST");
    forgetLibrary();
    showToast(r.fetched
      ? `Synced: ${plural(r.fetched, "blob")} fetched, ${plural(r.documents, "item")}`
      : `Up to date: ${plural(r.documents, "item")}`);
    reload();
  } catch (e) {
    if (!(e instanceof AuthError)) showToast("Sync failed: " + (e.message || e), true);
  } finally {
    sync.busy = false;
  }
}
