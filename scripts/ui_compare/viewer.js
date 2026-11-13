const state = Object.assign({mode:"slider", theme:"light", size:"viewport"},
  JSON.parse(localStorage.getItem("qa-compare") || "{}"));
const el = (tag, props = {}, ...kids) => { const n = Object.assign(document.createElement(tag), props); n.append(...kids); return n; };
const src = (id, side) => `shots/${id}-${side}-${state.theme}${state.size === "full" && side !== "diff" ? "-full" : ""}.png`;
function img(id, side, cls = "") {
  const i = el("img", {className: cls, src: src(id, side), alt: side, loading: "lazy"});
  i.onerror = () => i.replaceWith(el("div", {className: "missing", textContent: `No ${side} capture`}));
  return i;
}
function slider(id) {
  const before = img(id, "before", "before"), handle = el("div", {className: "handle"});
  const range = el("input", {type: "range", min: 0, max: 100, value: 50});
  range.setAttribute("aria-label", "Before/after position");
  const box = el("div", {className: "slider"}, img(id, "after"), before, handle, range);
  const set = pct => { pct = Math.max(0, Math.min(100, pct)); range.value = pct;
    before.style.clipPath = `inset(0 ${100 - pct}% 0 0)`; handle.style.left = pct + "%"; };
  const fromEvent = e => set((e.clientX - box.getBoundingClientRect().left) / box.offsetWidth * 100);
  box.addEventListener("pointerdown", e => { box.setPointerCapture(e.pointerId); fromEvent(e); });
  box.addEventListener("pointermove", e => { if (e.buttons) fromEvent(e); });
  range.addEventListener("input", () => set(+range.value));
  return [el("div", {className: "labels"}, el("span", {textContent: "◀ Before"}), el("span", {textContent: "After ▶"})), box];
}
function render() {
  localStorage.setItem("qa-compare", JSON.stringify(state));
  document.querySelectorAll(".seg").forEach(seg => seg.querySelectorAll("button").forEach(b =>
    b.setAttribute("aria-pressed", String(state[seg.dataset.opt] === b.dataset.v))));
  document.querySelectorAll("section").forEach(sec => {
    const id = sec.dataset.id, v = sec.querySelector(".viewer");
    if (state.mode === "side") {
      const fig = (cap, side) => el("figure", {}, el("figcaption", {textContent: cap}), img(id, side));
      v.replaceChildren(el("div", {className: "pair"}, fig("Before", "before"), fig("After", "after")));
    } else if (state.mode === "diff") {
      v.replaceChildren(el("div", {className: "labels"}, el("span", {textContent: "Changed pixels (red) over the after view, viewport only"})),
        el("div", {className: "single"}, img(id, "diff")));
    } else {
      v.replaceChildren(...slider(id));
    }
  });
}
document.querySelectorAll(".seg").forEach(seg => seg.addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  state[seg.dataset.opt] = b.dataset.v; render();
}));
render();
