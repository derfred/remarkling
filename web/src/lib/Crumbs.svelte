<script>
  /** items: [{ href, label }] */
  let { items } = $props();
</script>

<nav class="no-scrollbar flex gap-1 overflow-x-auto pt-1.5 pb-0.5 text-sm" aria-label="Breadcrumbs">
  {#each items as item, i}
    {#if i}<span class="self-center text-muted">›</span>{/if}
    <a href={item.href} class="whitespace-nowrap rounded-lg bg-surface-2 px-2 py-1 text-ink no-underline">{item.label}</a>
  {/each}
</nav>
