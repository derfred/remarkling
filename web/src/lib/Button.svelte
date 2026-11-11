<script>
  let { primary = false, type = "button", onclick, children } = $props();
</script>

<button {type} {onclick}
  class="mt-3 block h-12 w-full cursor-pointer rounded-xl font-semibold {primary ? 'bg-accent text-white dark:text-bg' : 'border border-line bg-surface text-ink'}">
  {@render children()}
</button>
