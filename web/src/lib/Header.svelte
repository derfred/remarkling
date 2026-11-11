<script>
  import Icon from "./Icon.svelte";
  import { forgetLibrary } from "./api.js";
  import { reload } from "./router.svelte.js";
  import { runSync, sync } from "./sync.svelte.js";

  /** title; back: hash to go to when there is no history; children: search, chips, breadcrumbs. */
  let { title, back = null, children } = $props();

  const iconBtn = "grid size-11 flex-none place-items-center rounded-full active:bg-surface-2 disabled:opacity-60";

  function goBack() {
    if (history.length > 1) history.back();
    else location.hash = back;
  }
</script>

<header class="pt-safe sticky top-0 z-10 border-b border-line bg-bg/90 px-4 pb-2 backdrop-blur-md">
  <div class="mx-auto max-w-182">
    <div class="flex min-h-11 items-center gap-2">
      {#if back}
        <button class={iconBtn} aria-label="Back" onclick={goBack}><Icon name="back" /></button>
      {/if}
      <h1 class="min-w-0 flex-1 truncate text-xl font-bold">{title}</h1>
      <button class={iconBtn} aria-label="Sync from reMarkable cloud" title="Sync from reMarkable cloud"
              disabled={sync.busy} onclick={runSync}>
        <Icon name="sync" class="size-5.5 {sync.busy ? 'animate-pulse' : ''}" />
      </button>
      <button class={iconBtn} aria-label="Reload" title="Reload" onclick={() => { forgetLibrary(); reload(); }}>
        <Icon name="reload" class="size-5.5" />
      </button>
    </div>
    {@render children?.()}
  </div>
</header>
