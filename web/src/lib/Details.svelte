<script>
  let { summary, children } = $props();
</script>

<details class="group mt-2.5">
  <summary class="cursor-pointer list-none px-1 py-3 font-semibold text-accent [&::-webkit-details-marker]:hidden">
    <span class="inline-block transition-transform group-open:rotate-90">▸</span> {summary}
  </summary>
  {@render children()}
</details>
