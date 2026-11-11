<script>
  /** A definition list; children: <KVRow> elements. */
  let { children } = $props();
</script>

<dl class="rounded-2xl border border-line bg-surface">{@render children()}</dl>
