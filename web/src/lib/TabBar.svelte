<script>
  import Icon from "./Icon.svelte";
  import { route } from "./router.svelte.js";

  const TABS = [
    { id: "library", label: "Library", icon: "folderOutline", views: ["library", "doc"] },
    { id: "blobs", label: "Blobs", icon: "blobs", views: ["blobs", "blob"] },
    { id: "stats", label: "Stats", icon: "stats", views: ["stats"] },
  ];
</script>

<nav class="pb-safe fixed inset-x-0 bottom-0 z-10 flex border-t border-line bg-surface/90 backdrop-blur-md md:justify-center">
  {#each TABS as tab}
    <a href="#/{tab.id}" aria-current={tab.views.includes(route.view) ? "page" : undefined}
       class="flex h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] font-semibold text-muted no-underline aria-[current=page]:text-accent md:flex-none md:basis-30">
      <Icon name={tab.icon} class="size-6" />{tab.label}
    </a>
  {/each}
</nav>
