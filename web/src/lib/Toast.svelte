<script>
  import { toast } from "./toast.svelte.js";
</script>

<div role="status"
  class="pointer-events-none fixed inset-x-4 bottom-[calc(72px+env(safe-area-inset-bottom))] z-20 mx-auto max-w-120 rounded-xl px-4 py-3 text-center text-[15px] transition duration-200
         {toast.error ? 'bg-danger text-white' : 'bg-ink text-bg'} {toast.visible ? 'translate-y-0 opacity-100' : 'translate-y-2 opacity-0'}">
  {toast.text}
</div>
