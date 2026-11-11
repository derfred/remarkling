<script>
  import Badge from "./Badge.svelte";

  let { ink } = $props();
  const tone = $derived(ink?.status === "pulled" ? "ok" : ink?.status === "pushed" ? "warn" : "muted");
</script>

{#if ink}<Badge {tone}>{ink.status}</Badge>{/if}
