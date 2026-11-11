<script>
  let { label, children } = $props();
</script>

<div class="flex gap-3 border-t border-line px-3.5 py-2.75 first:border-t-0">
  <dt class="w-[38%] max-w-40 flex-none text-muted">{label}</dt>
  <dd class="min-w-0 flex-1 text-right [overflow-wrap:anywhere]">{@render children()}</dd>
</div>
