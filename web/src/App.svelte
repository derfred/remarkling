<script>
  import Login from "./views/Login.svelte";
  import Library from "./views/Library.svelte";
  import Doc from "./views/Doc.svelte";
  import Blobs from "./views/Blobs.svelte";
  import Blob from "./views/Blob.svelte";
  import Stats from "./views/Stats.svelte";
  import TabBar from "./lib/TabBar.svelte";
  import Toast from "./lib/Toast.svelte";
  import { route } from "./lib/router.svelte.js";
  import { session } from "./lib/session.svelte.js";
</script>

{#if !session.token}
  <Login />
{:else}
  <!-- Keyed so a new route or a reload remounts the view, which then fetches afresh. -->
  {#key `${route.view}/${route.arg}/${route.n}`}
    {#if route.view === "doc" && route.arg}
      <Doc uuid={route.arg} />
    {:else if route.view === "blobs"}
      <Blobs />
    {:else if route.view === "blob" && route.arg}
      <Blob key={route.arg} />
    {:else if route.view === "stats"}
      <Stats />
    {:else}
      <Library folder={route.arg} />
    {/if}
  {/key}
  <TabBar />
  <Toast />
{/if}
