<script>
  import Icon from "./Icon.svelte";

  /** A list row; without href it is not a link. children: badges after the title. */
  let { href = null, icon = "doc", tint = false, title, sub = "", mono = false, children } = $props();
</script>

<svelte:element this={href ? "a" : "div"} {href}
  class="flex min-h-15 items-center gap-3 border-t border-line px-3.5 py-2.5 text-inherit no-underline first:border-t-0 {href ? 'cursor-pointer active:bg-surface-2' : ''}">
  <span class="grid size-9 flex-none place-items-center rounded-[10px] {tint ? 'bg-accent-soft text-accent' : 'bg-surface-2 text-muted'}">
    <Icon name={icon} />
  </span>
  <div class="min-w-0 flex-1">
    <div class="truncate font-semibold {mono ? 'font-mono text-[13px]' : ''}">{title}{@render children?.()}</div>
    {#if sub}<div class="truncate text-[13px] text-muted">{sub}</div>{/if}
  </div>
  {#if href}<Icon name="chev" class="size-4 flex-none text-muted" />{/if}
</svelte:element>
