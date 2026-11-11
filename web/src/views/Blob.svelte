<script>
  import Page from "./Page.svelte";
  import Header from "../lib/Header.svelte";
  import List from "../lib/List.svelte";
  import Row from "../lib/Row.svelte";
  import Badge from "../lib/Badge.svelte";
  import Section from "../lib/Section.svelte";
  import KV from "../lib/KV.svelte";
  import KVRow from "../lib/KVRow.svelte";
  import Json from "../lib/Json.svelte";
  import Await from "../lib/Await.svelte";
  import { api } from "../lib/api.js";
  import { enc } from "../lib/router.svelte.js";
  import { bytes, fullTime, short } from "../lib/format.js";

  let { key } = $props();
  let blob = $state(null);
  const promise = $derived(api("/blobs/" + key.split("/").map(enc).join("/")).then((b) => (blob = b)));
  const mono = "font-mono text-[13px]";

  function entryRow(b, e) {
    const isDoc = !e.id.includes(".");
    return {
      href: isDoc ? `#/doc/${enc(e.id)}` : e.cached ? `#/blob/${enc("cloud.blob:" + e.hash)}` : null,
      icon: isDoc ? "doc" : "blob",
      mono: !isDoc || !e.name,
      title: isDoc ? e.name || e.id : b.doc_uuid ? e.id.replace(b.doc_uuid, "…") : e.id,
      sub: [bytes(e.size), isDoc && `${e.subfiles} files`, short(e.hash)].filter(Boolean).join(" · "),
    };
  }
</script>

<Header title={blob ? blob.doc_name || blob.filename || blob.kind : "Blob"} back="#/blobs" />

<Page>
  <Await {promise}>
    {#snippet children(b)}
      <Section>Entry</Section>
      <KV>
        <KVRow label="Kind"><Badge tone={b.kind === "root" ? "ok" : "muted"}>{b.kind}</Badge></KVRow>
        {#if b.filename}<KVRow label="File"><span class={mono}>{b.filename}</span></KVRow>{/if}
        {#if b.doc_uuid}
          <KVRow label="Document"><a class="text-accent" href="#/doc/{enc(b.doc_uuid)}">{b.doc_name || b.doc_uuid}</a></KVRow>
        {/if}
        <KVRow label="Size">{bytes(b.size)}</KVRow>
        <KVRow label="Cached">{fullTime(b.created_at)}</KVRow>
        <KVRow label="Key"><span class={mono}>{b.key}</span></KVRow>
      </KV>

      {#if b.index}
        <Section>Index entries ({b.index.entries.length}) · schema {b.index.schema}</Section>
        <List>
          {#each b.index.entries as e (e.id)}
            {@const r = entryRow(b, e)}
            <Row {...r}>
              {#if e.cached}<Badge tone="ok">cached</Badge>{:else}<Badge>not cached</Badge>{/if}
            </Row>
          {/each}
        </List>
      {/if}

      <Section>Value · {b.encoding}</Section>
      <Json value={b.value} />
    {/snippet}
  </Await>
</Page>
