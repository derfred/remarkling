<script>
  import Icon from "./Icon.svelte";

  /**
   * A list row; without href it is not a link. children: badges after the title. action: a control
   * at the end of the row, outside the link (a button cannot sit inside an <a>).
   */
  let { href = null, icon = "doc", tint = false, title, sub = "", mono = false, children, action } = $props();
</script>

<div class="flex items-center border-t border-line first:border-t-0">
  <svelte:element this={href ? "a" : "div"} {href}
    class="flex min-h-15 min-w-0 flex-1 items-center gap-3 px-3.5 py-2.5 text-inherit no-underline {href ? 'cursor-pointer active:bg-surface-2' : ''}">
    <span class="grid size-9 flex-none place-items-center rounded-[10px] {tint ? 'bg-accent-soft text-accent' : 'bg-surface-2 text-muted'}">
      <Icon name={icon} />
    </span>
    <div class="min-w-0 flex-1">
      <div class="truncate font-semibold {mono ? 'font-mono text-[13px]' : ''}">{title}{@render children?.()}</div>
      {#if sub}<div class="truncate text-[13px] text-muted">{sub}</div>{/if}
    </div>
    {#if href}<Icon name="chev" class="size-4 flex-none text-muted" />{/if}
  </svelte:element>
  {@render action?.()}
</div>
