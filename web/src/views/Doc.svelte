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
  import { api } from "../lib/api.js";
  import { enc } from "../lib/router.svelte.js";
  import { bytes, fullTime, short } from "../lib/format.js";

  let { uuid } = $props();
  let doc = $state(null);
  const promise = $derived(api(`/library/${enc(uuid)}`).then((d) => (doc = d)));

  const back = $derived(doc?.parent && doc.parent !== "trash" ? `#/library/${enc(doc.parent)}` : "#/library");
  const crumbs = $derived(doc ? [{ href: "#/library", label: "Library" },
    ...doc.path.map((p) => ({ href: `#/library/${enc(p.uuid)}`, label: p.name }))] : []);
  const mono = "font-mono text-[13px]";
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
