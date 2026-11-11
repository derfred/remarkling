<script>
  import { onDestroy } from "svelte";
  import { blobUrl } from "./api.js";

  /** One rendered page, fetched when it scrolls near the viewport. width/height: page size in points. */
  let { uuid, page } = $props();

  let box = $state();
  let src = $state(null);
  let failed = $state(false);

  $effect(() => {
    if (!box) return;
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      io.disconnect();
      // Pixels for the box at this screen's density, in steps so the server's cache can help.
      const px = Math.min(2400, Math.ceil((box.clientWidth * (window.devicePixelRatio || 1)) / 200) * 200);
      blobUrl(`/${encodeURIComponent(uuid)}/pages/${page.number}.png?width=${px}`)
        .then((url) => (src = url))
        .catch(() => (failed = true));
    }, { rootMargin: "600px 0px" });
    io.observe(box);
    return () => io.disconnect();
  });

  onDestroy(() => src && URL.revokeObjectURL(src));
</script>

<!-- Tall pages scroll inside their box instead of shrinking to a sliver. -->
<div class="max-h-[80vh] overflow-y-auto bg-white">
  <div bind:this={box} class="relative w-full" style="aspect-ratio: {page.width} / {page.height}">
    {#if src}
      <img {src} alt="Page {page.number}" class="block w-full" />
    {:else}
      <div class="absolute inset-x-0 top-0 grid h-40 place-items-center text-[13px] text-muted">
        {failed ? "Could not render this page" : "Rendering…"}
      </div>
    {/if}
  </div>
</div>
