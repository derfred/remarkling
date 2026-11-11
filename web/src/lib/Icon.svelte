<script>
  // Inline SVG icons; the markup is static, never user data.
  const ICONS = {
    back: '<path d="M15 18l-6-6 6-6" stroke-width="2"/>',
    reload: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    sync: '<path d="M7 18a5 5 0 0 1-.6-9.96A6 6 0 0 1 18 9a4.5 4.5 0 0 1-.5 8.97"/><path d="M12 12v8M9 17l3 3 3-3"/>',
    folder: '<path fill="currentColor" stroke="none" d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    folderOutline: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    doc: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    blob: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9h8M8 13h8M8 17h5"/>',
    blobs: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    stats: '<path d="M5 20V10M12 20V4M19 20v-7"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    chev: '<path d="M9 6l6 6-6 6" stroke-width="2"/>',
  };
  let { name, class: cls = "size-5" } = $props();
</script>

<svg class={cls} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">{@html ICONS[name]}</svg>
