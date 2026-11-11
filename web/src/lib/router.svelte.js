// Hash routes: #/library[/<folder uuid>|/trash], #/doc/<uuid>, #/blobs, #/blob/<key>, #/stats.
function parse() {
  const [, view, ...rest] = (location.hash || "#/library").split("/");
  return { view: view || "library", arg: rest.length ? decodeURIComponent(rest.join("/")) : null };
}

// `n` bumps on reload so views keyed on it fetch again.
export const route = $state({ ...parse(), n: 0 });

window.addEventListener("hashchange", () => {
  Object.assign(route, parse());
  window.scrollTo(0, 0);
});

export function reload() {
  route.n++;
}

export const enc = encodeURIComponent;
