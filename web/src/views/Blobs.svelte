<script>
  import Page from "./Page.svelte";
  import Header from "../lib/Header.svelte";
  import Search from "../lib/Search.svelte";
  import List from "../lib/List.svelte";
  import Row from "../lib/Row.svelte";
  import Button from "../lib/Button.svelte";
  import Empty from "../lib/Empty.svelte";
  import Await from "../lib/Await.svelte";
  import { api } from "../lib/api.js";
  import { enc } from "../lib/router.svelte.js";
  import { bytes, plural, short, when } from "../lib/format.js";

  const KINDS = ["all", "root", "index", "metadata", "content", "other"];
  const PAGE = 100;
  const saved = (() => { try { return JSON.parse(sessionStorage.getItem("blobs")) || {}; } catch { return {}; } })();

  let kind = $state(saved.kind || "all");
  let query = $state(saved.q || "");
  let items = $state([]);
  let total = $state(0);
  let more = $state(false);
  let promise = $state(load());
  let timer;

  function load(append = false) {
    try { sessionStorage.setItem("blobs", JSON.stringify({ kind, q: query })); } catch {}
    const qs = new URLSearchParams({ q: query, limit: PAGE, offset: append ? items.length : 0 });
    if (kind !== "all") qs.set("kind", kind);
    return api("/blobs?" + qs).then((r) => {
      items = append ? [...items, ...r.items] : r.items;
      total = r.total;
    });
  }

  function pick(k) { kind = k; promise = load(); }
  function search() { clearTimeout(timer); timer = setTimeout(() => (promise = load()), 250); }
  async function loadMore() { more = true; try { await load(true); } finally { more = false; } }

  function sub(b) {
    return [b.kind, b.doc_name ? (b.filename || "").replace(b.doc_uuid, "…") : null, bytes(b.size), when(b.created_at)]
      .filter(Boolean).join(" · ");
  }
</script>

<Header title="Blobs">
  <Search bind:value={query} placeholder="Search key, file or document" oninput={search} />
  <div class="no-scrollbar flex gap-1.5 overflow-x-auto pt-2 pb-0.5">
    {#each KINDS as k}
      <button aria-pressed={kind === k} onclick={() => pick(k)}
        class="h-8.5 flex-none cursor-pointer rounded-full border border-line bg-surface px-3.5 text-sm aria-pressed:border-ink aria-pressed:bg-ink aria-pressed:text-bg">{k}</button>
    {/each}
  </div>
</Header>

<Page>
  <Await {promise}>
    {#snippet children()}
      <p class="mx-1 my-2 text-sm text-muted">{plural(total, "entry", "entries")}</p>
      {#if total}
        <List>
          {#each items as b (b.key)}
            <Row href="#/blob/{enc(b.key)}" icon="blob" mono={!b.doc_name}
                 title={b.doc_name || b.filename || short(b.hash) || b.key} sub={sub(b)} />
          {/each}
        </List>
        {#if items.length < total}
          <Button onclick={loadMore}>{more ? "Loading…" : "Load more"}</Button>
        {/if}
      {:else}
        <Empty>Nothing here.</Empty>
      {/if}
    {/snippet}
  </Await>
</Page>
