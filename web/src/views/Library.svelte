<script>
  import Page from "./Page.svelte";
  import NodeRow from "./NodeRow.svelte";
  import Header from "../lib/Header.svelte";
  import Search from "../lib/Search.svelte";
  import Crumbs from "../lib/Crumbs.svelte";
  import List from "../lib/List.svelte";
  import Row from "../lib/Row.svelte";
  import Section from "../lib/Section.svelte";
  import Details from "../lib/Details.svelte";
  import KV from "../lib/KV.svelte";
  import KVRow from "../lib/KVRow.svelte";
  import Empty from "../lib/Empty.svelte";
  import Await from "../lib/Await.svelte";
  import { getLibrary } from "../lib/api.js";
  import { enc } from "../lib/router.svelte.js";
  import { fullTime, plural } from "../lib/format.js";

  /** folder: a folder UUID, "trash", or null for the top level. */
  let { folder } = $props();

  const stored = (() => { try { return sessionStorage.getItem("libq") || ""; } catch { return ""; } })();
  let query = $state(stored);
  $effect(() => { try { sessionStorage.setItem("libq", query); } catch {} });

  // "Recent": every document changed in the last RECENT_HOURS, newest first, ignoring folders.
  const RECENT_HOURS = 5;
  let recent = $state((() => { try { return sessionStorage.getItem("librecent") === "1"; } catch { return false; } })());
  $effect(() => { try { sessionStorage.setItem("librecent", recent ? "1" : ""); } catch {} });

  let items = $state([]);
  const promise = getLibrary().then((list) => (items = list));

  const byId = $derived(Object.fromEntries(items.map((n) => [n.uuid, n])));
  const current = $derived(folder && folder !== "trash" ? byId[folder] : null);
  const title = $derived(folder === "trash" ? "Trash" : current ? current.name : "Library");

  const crumbs = $derived.by(() => {
    if (!folder) return [];
    const chain = [], seen = new Set();
    let p = folder === "trash" ? null : current?.parent;
    while (p && byId[p] && !seen.has(p)) { seen.add(p); chain.unshift(byId[p]); p = byId[p].parent; }
    return [
      { href: "#/library", label: "Library" },
      ...(p === "trash" ? [{ href: "#/library/trash", label: "Trash" }] : []),
      ...chain.map((c) => ({ href: `#/library/${enc(c.uuid)}`, label: c.name })),
    ];
  });

  const term = $derived(query.trim().toLowerCase());
  const shown = $derived(
    recent ? items
      .filter((n) => n.type === "document" && !n.deleted && n.last_modified >= Date.now() / 1000 - RECENT_HOURS * 3600)
      .filter((n) => !term || n.name.toLowerCase().includes(term) || n.uuid.startsWith(term) || n.inkbridge?.doc_id?.includes(term))
      .sort((a, b) => b.last_modified - a.last_modified)
    : term ? items.filter((n) => n.name.toLowerCase().includes(term) || n.uuid.startsWith(term) || n.inkbridge?.doc_id?.includes(term))
    : folder === "trash" ? items.filter((n) => n.parent === "trash")
    : items.filter((n) => (n.parent || "") === (folder || "") && !(n.deleted && !folder)));
  const trashed = $derived(items.filter((n) => n.parent === "trash").length);
</script>

<Header {title} back={folder ? "#/library" : null}>
  <Search bind:value={query} placeholder="Search all documents" />
  <div class="flex gap-1.5 pt-2 pb-0.5">
    <button aria-pressed={recent} onclick={() => (recent = !recent)}
      class="h-8.5 flex-none cursor-pointer rounded-full border border-line bg-surface px-3.5 text-sm aria-pressed:border-ink aria-pressed:bg-ink aria-pressed:text-bg">Updated in the last {RECENT_HOURS} h</button>
  </div>
  {#if crumbs.length}<Crumbs items={crumbs} />{/if}
</Header>

<Page>
  <Await {promise}>
    {#snippet children()}
      {#if !items.length}
        <Empty>The cache holds no library yet.<br />Tap the cloud button to sync it now.</Empty>
      {:else if !shown.length}
        <Empty>{recent ? `No documents updated in the last ${RECENT_HOURS} hours.` : term ? "No matches." : "This folder is empty."}</Empty>
      {:else}
        <List>{#each shown as node (node.uuid)}<NodeRow {node} />{/each}</List>
      {/if}

      {#if !folder && !term && !recent && trashed}
        <Section>Other</Section>
        <List><Row href="#/library/trash" icon="trash" title="Trash" sub={plural(trashed, "item")} /></List>
      {/if}

      {#if current}
        <Details summary="Folder details">
          <KV>
            <KVRow label="UUID"><span class="font-mono text-[13px]">{current.uuid}</span></KVRow>
            <KVRow label="Modified">{fullTime(current.last_modified)}</KVRow>
          </KV>
        </Details>
      {/if}
    {/snippet}
  </Await>
</Page>
