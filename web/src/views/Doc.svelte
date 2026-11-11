<script>
  import Page from "./Page.svelte";
  import Header from "../lib/Header.svelte";
  import Crumbs from "../lib/Crumbs.svelte";
  import List from "../lib/List.svelte";
  import Row from "../lib/Row.svelte";
  import Badge from "../lib/Badge.svelte";
  import InkBadge from "../lib/InkBadge.svelte";
  import Section from "../lib/Section.svelte";
  import Details from "../lib/Details.svelte";
  import KV from "../lib/KV.svelte";
  import KVRow from "../lib/KVRow.svelte";
  import Json from "../lib/Json.svelte";
  import Await from "../lib/Await.svelte";
  import Button from "../lib/Button.svelte";
  import Icon from "../lib/Icon.svelte";
  import { api, download, AuthError } from "../lib/api.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { enc } from "../lib/router.svelte.js";
  import { bytes, fullTime, short } from "../lib/format.js";

  let { uuid } = $props();
  let doc = $state(null);
  const promise = $derived(api(`/library/${enc(uuid)}`).then((d) => (doc = d)));

  const back = $derived(doc?.parent && doc.parent !== "trash" ? `#/library/${enc(doc.parent)}` : "#/library");
  const crumbs = $derived(doc ? [{ href: "#/library", label: "Library" },
    ...doc.path.map((p) => ({ href: `#/library/${enc(p.uuid)}`, label: p.name }))] : []);
  const mono = "font-mono text-[13px]";

  // The original the document was made from (PDF or EPUB); notebooks have none.
  const original = $derived(doc?.file_list.find((f) => /\.(pdf|epub)$/.test(f.name) && !f.name.includes("/")));
  let busy = $state(null);  // path being downloaded

  async function save(path) {
    if (busy) return;
    busy = path;
    try {
      await download(path);
    } catch (e) {
      if (!(e instanceof AuthError)) showToast("Download failed: " + e.message, true);
    } finally {
      busy = null;
    }
  }
  const filePath = (f) => `/${enc(uuid)}/files/${f.name.split("/").map(encodeURIComponent).join("/")}`;
</script>

<Header title={doc?.name ?? "Document"} {back}>
  {#if doc}<Crumbs items={crumbs} />{/if}
</Header>

<Page>
  <Await {promise}>
    {#snippet children(d)}
      {#if !d.metadata}
        <div class="mb-3 rounded-xl bg-danger-soft px-3.5 py-3 text-danger">This document's metadata is not in the cache.</div>
      {/if}

      <Section>Overview</Section>
      <KV>
        <KVRow label="Type">{d.type}{d.file_type ? " · " + d.file_type.toUpperCase() : ""}</KVRow>
        {#if d.page_count != null}<KVRow label="Pages">{d.page_count}</KVRow>{/if}
        <KVRow label="Modified">{fullTime(d.last_modified)}</KVRow>
        <KVRow label="Opened">{fullTime(d.last_opened)}</KVRow>
        <KVRow label="Size">{bytes(d.size)}</KVRow>
        {#if d.tags.length}<KVRow label="Tags">{d.tags.join(", ")}</KVRow>{/if}
        <KVRow label="Pinned">{d.pinned ? "yes" : "no"}</KVRow>
        {#if d.deleted}<KVRow label="Status"><Badge tone="danger">trashed</Badge></KVRow>{/if}
        <KVRow label="UUID"><span class={mono}>{d.uuid}</span></KVRow>
      </KV>

      {#if d.inkbridge}
        {@const ink = d.inkbridge}
        <Section>Ink Bridge</Section>
        <KV>
          <KVRow label="Doc ID"><span class={mono}>{ink.doc_id}</span></KVRow>
          <KVRow label="Title">{ink.title}</KVRow>
          <KVRow label="Status"><InkBadge {ink} /></KVRow>
          <KVRow label="Source">{ink.source_kind}</KVRow>
          <KVRow label="Pushed">{fullTime(ink.pushed_at)}</KVRow>
          {#if ink.pulled_at}<KVRow label="Pulled">{fullTime(ink.pulled_at)}</KVRow>{/if}
          {#if ink.trilium_url}
            <KVRow label="Trilium"><a class="text-accent" href={ink.trilium_url} target="_blank" rel="noopener">Open note</a></KVRow>
          {/if}
        </KV>
      {/if}

      {#if d.type === "document"}
        <Section>Download</Section>
        {#if original}
          {@const kind = original.name.split(".").pop().toUpperCase()}
          <Button primary onclick={() => save(filePath(original))}>
            {busy === filePath(original) ? "Downloading…" : `Download ${kind} · ${bytes(original.size)}`}
          </Button>
        {/if}
        <Button onclick={() => save(`/${enc(uuid)}/rmdoc`)}>
          {busy === `/${enc(uuid)}/rmdoc` ? "Downloading…" : "Download .rmdoc (all files, with strokes)"}
        </Button>
        <p class="mx-1 mt-2 text-[13px] text-muted">Fetched from the tablet now, not from the cache.</p>
      {/if}

      <Section>Files ({d.file_list.length})</Section>
      {#if d.index_key || d.file_list.length}
        <List>
          {#if d.index_key}
            <Row href="#/blob/{enc(d.index_key)}" icon="blob" mono title="….docSchema" sub="document index · {short(d.hash)}">
              <Badge tone="ok">cached</Badge>
            </Row>
          {/if}
          {#each d.file_list as f (f.name)}
            <Row href={f.cached ? `#/blob/${enc("cloud.blob:" + f.hash)}` : null} icon="blob" mono
                 title={f.name.replace(d.uuid, "…")} sub="{bytes(f.size)} · {short(f.hash)}">
              {#if f.cached}<Badge tone="ok">cached</Badge>{:else}<Badge>not cached</Badge>{/if}
              {#snippet action()}
                <button type="button" aria-label="Download {f.name.replace(d.uuid, '…')}" disabled={busy === filePath(f)}
                  class="grid size-12 flex-none cursor-pointer place-items-center border-l border-line text-accent active:bg-surface-2 disabled:opacity-40"
                  onclick={() => save(filePath(f))}>
                  <Icon name="download" />
                </button>
              {/snippet}
            </Row>
          {/each}
        </List>
      {:else}
        <p class="mx-1 my-2 text-sm text-muted">Document index not cached.</p>
      {/if}

      {#if d.metadata}<Details summary="Raw metadata"><Json value={d.metadata} /></Details>{/if}
      {#if d.content}<Details summary="Raw content"><Json value={d.content} /></Details>{/if}
    {/snippet}
  </Await>
</Page>
