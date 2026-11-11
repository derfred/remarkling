<script>
  import Row from "../lib/Row.svelte";
  import Badge from "../lib/Badge.svelte";
  import InkBadge from "../lib/InkBadge.svelte";
  import { enc } from "../lib/router.svelte.js";
  import { plural, when } from "../lib/format.js";

  let { node } = $props();
  const folder = $derived(node.type === "folder");
  const sub = $derived(
    !node.metadata_cached ? "metadata not cached"
    : folder ? plural(node.children, "item")
    : [node.file_type?.toUpperCase(), node.page_count != null && `${node.page_count} p.`, when(node.last_modified)]
        .filter(Boolean).join(" · "));
</script>

<Row href={folder ? `#/library/${enc(node.uuid)}` : `#/doc/${enc(node.uuid)}`}
     icon={folder ? "folder" : "doc"} tint={folder} title={node.name} {sub}>
  {#if node.pinned}<Badge tone="warn">★</Badge>{/if}
  <InkBadge ink={node.inkbridge} />
</Row>
