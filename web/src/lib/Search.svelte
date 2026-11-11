<script>
  let { value = $bindable(""), placeholder, oninput } = $props();
</script>

<input type="search" bind:value {placeholder} {oninput} enterkeyhint="search" autocomplete="off"
       class="mt-1 h-11 w-full rounded-xl border border-line bg-surface px-3.5 outline-none focus:border-accent" />
