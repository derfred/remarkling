<script>
  import Page from "./Page.svelte";
  import Header from "../lib/Header.svelte";
  import Section from "../lib/Section.svelte";
  import KV from "../lib/KV.svelte";
  import KVRow from "../lib/KVRow.svelte";
  import Await from "../lib/Await.svelte";
  import { api } from "../lib/api.js";
  import { enc } from "../lib/router.svelte.js";
  import { bytes, fullTime, short } from "../lib/format.js";

  const promise = api("");

  function showKind(kind) {
    try { sessionStorage.setItem("blobs", JSON.stringify({ kind, q: "" })); } catch {}
  }
</script>

{#snippet tile(n, label)}
  <div class="rounded-2xl border border-line bg-surface p-3.5">
    <div class="text-[26px] font-bold tabular-nums">{n}</div>
    <div class="text-[13px] text-muted">{label}</div>
  </div>
{/snippet}

<Header title="Cache" />

<Page>
  <Await {promise}>
    {#snippet children(s)}
      {@const max = Math.max(1, ...Object.values(s.by_kind).map((k) => k.bytes))}
      <Section>Library</Section>
      <div class="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        {@render tile(s.library.documents, "documents")}
        {@render tile(s.library.folders, "folders")}
        {@render tile(s.library.tracked, "tracked by Ink Bridge")}
        {@render tile(s.library.trashed, "in trash")}
      </div>

      <Section>Store</Section>
      <div class="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        {@render tile(s.entries, "entries")}
        {@render tile(bytes(s.bytes), "stored")}
      </div>

      <Section>By kind</Section>
      <div class="overflow-hidden rounded-2xl border border-line bg-surface">
        {#each Object.entries(s.by_kind) as [kind, v]}
          <a href="#/blobs" onclick={() => showKind(kind)}
             class="block border-t border-line px-3.5 py-3 text-inherit no-underline first:border-t-0 active:bg-surface-2">
            <div class="flex justify-between text-sm">
              <span><b>{kind}</b> · {v.count}</span><span class="font-mono">{bytes(v.bytes)}</span>
            </div>
            <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-2">
              <div class="h-full rounded-full bg-accent" style:width="{(v.bytes / max) * 100}%"></div>
            </div>
          </a>
        {/each}
      </div>

      <Section>Root</Section>
      {#if s.root}
        <KV>
          <KVRow label="Hash"><a class="font-mono text-[13px] text-accent" href="#/blob/{enc('cloud.blob:' + s.root.hash)}">{short(s.root.hash)}</a></KVRow>
          <KVRow label="Entries">{s.root.entries}</KVRow>
          <KVRow label="Cached">{fullTime(s.root.created_at)}</KVRow>
          {#if s.root.roots_cached > 1}<KVRow label="Roots cached">{s.root.roots_cached}</KVRow>{/if}
        </KV>
      {:else}
        <p class="mx-1 my-2 text-sm text-muted">No root index cached yet.</p>
      {/if}

      <Section>Health</Section>
      <KV>
        <KVRow label="Uncached metadata">{s.library.uncached}</KVRow>
        <KVRow label="Unreferenced entries">{s.unreferenced}</KVRow>
        <KVRow label="Oldest entry">{fullTime(s.oldest)}</KVRow>
        <KVRow label="Newest entry">{fullTime(s.newest)}</KVRow>
      </KV>

    {/snippet}
  </Await>
</Page>
