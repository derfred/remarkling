export const toast = $state({ text: "", error: false, visible: false });
let timer;

export function showToast(text, error = false) {
  Object.assign(toast, { text, error, visible: true });
  clearTimeout(timer);
  timer = setTimeout(() => { toast.visible = false; }, error ? 6000 : 3000);
}
