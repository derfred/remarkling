<script>
  import Button from "../lib/Button.svelte";
  import { login, session } from "../lib/session.svelte.js";

  let token = $state("");
  let input;
  $effect(() => input.focus());

  function submit(e) {
    e.preventDefault();
    if (token.trim()) login(token.trim());
  }
</script>

<header class="pt-safe border-b border-line px-4 pb-2">
  <h1 class="flex min-h-11 items-center text-xl font-bold">remarkling</h1>
</header>
<form class="mx-auto mt-[12vh] max-w-105 px-4" onsubmit={submit}>
  <h2 class="mb-2 text-[13px] font-semibold tracking-wider text-muted uppercase">Cache explorer</h2>
  <p class="mb-3 text-muted">Enter the API_TOKEN of this server. It stays in this browser.</p>
  {#if session.message}
    <div class="mb-3 rounded-xl bg-danger-soft px-3.5 py-3 text-danger">{session.message}</div>
  {/if}
  <input bind:this={input} bind:value={token} type="password" placeholder="API token" autocomplete="current-password" required
         class="h-12 w-full rounded-xl border border-line bg-surface px-3.5 outline-none focus:border-accent" />
  <Button primary type="submit">Continue</Button>
</form>
