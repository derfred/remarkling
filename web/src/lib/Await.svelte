<script>
  import { AuthError } from "./api.js";

  /** Renders children(data) once `promise` resolves; a spinner before, the error after a failure. */
  let { promise, children } = $props();
</script>

{#await promise}
  <div class="mx-auto my-14 size-7 animate-spin rounded-full border-3 border-line border-t-accent" role="status" aria-label="Loading"></div>
{:then data}
  {@render children(data)}
{:catch error}
  {#if !(error instanceof AuthError)}
    <div class="mb-3 rounded-xl bg-danger-soft px-3.5 py-3 text-danger">{error.message || error}</div>
  {/if}
{/await}
