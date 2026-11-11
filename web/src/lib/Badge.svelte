<script>
  const TONES = {
    ok: "bg-ok-soft text-ok",
    warn: "bg-warn-soft text-warn",
    danger: "bg-danger-soft text-danger",
    muted: "bg-surface-2 text-muted",
  };
  let { tone = "muted", children } = $props();
</script>

<span class="ml-1 inline-block rounded-md px-1.5 py-0.5 align-[1px] text-[11px] font-semibold tracking-wide uppercase {TONES[tone]}">{@render children()}</span>
