<script>
  /** Pretty JSON, or text as is. */
  let { value } = $props();
  const text = $derived(typeof value === "string" ? value : JSON.stringify(value, null, 2));
</script>

<pre class="max-h-[70vh] overflow-auto rounded-2xl border border-line bg-surface px-3.5 py-3 font-mono text-[12.5px] leading-normal whitespace-pre">{text}</pre>
