/* sitegraph explorer — vanilla JS, no build step and no network fetches beyond
   the crawl data itself, so `serve` works offline.

   Two views over one dataset: a pannable/zoomable graph, and a contact sheet.
   Both open the same inspector.

   All page-derived strings (titles above all) come from a site we do not
   control, so they are only ever inserted as text nodes — never as HTML. */

"use strict";

/* #world is a fixed 12000x12000 coordinate space with its origin at the
   centre; the pan/zoom transform is applied to it as a whole, which means the
   node cards and the edge SVG share one coordinate system for free. */
const WORLD = 12000;
const ORIGIN = WORLD / 2;
const CARD_W = 168;
const CARD_H = 138;
const MIN_K = 0.04;
const MAX_K = 3;

const REPULSION = 900000;
const SPRING = 0.02;
const SPRING_LENGTH = 230;
const GRAVITY = 0.0016;
const DAMPING = 0.82;

/* Every node is pushed by every other node, so a fixed pairwise repulsion
   makes the settled radius grow with the node count — at 300 nodes the graph
   settles tens of thousands of units across and frames as an empty screen.
   Dividing by the count keeps the radius roughly constant instead. */
const REPULSION_NODE_SCALE = 40;

/* The simulation runs inside the fixed #world box, and the finished layout is
   rescaled to fit it. Without a hard bound, one runaway node stretches the
   bounding box that `fit` frames, which is how you get a blank graph view. */
const LAYOUT_MARGIN = CARD_W;

const state = {
  nodes: [],
  byId: new Map(),
  edges: [],
  edgeEls: [],
  nodeEls: new Map(),
  selected: null,
  filter: "",
  transform: { x: 0, y: 0, k: 1 },
  pos: new Map(), // id -> {x, y, vx, vy, pinned}
  pages: new Map(), // id -> page record (fetched lazily)
};

/* ------------------------------------------------------------------ utils */

const $ = (id) => document.getElementById(id);

function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

/** Build an element. Children given as strings become text nodes — never HTML. */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

/** Path + query of a URL, for display. */
function pathOf(url) {
  try {
    const parsed = new URL(url);
    return parsed.pathname + parsed.search;
  } catch {
    return url;
  }
}

function titleOf(node) {
  return node.title && node.title.trim() ? node.title.trim() : "";
}

function matches(node) {
  if (!state.filter) return true;
  const needle = state.filter.toLowerCase();
  return (
    node.url.toLowerCase().includes(needle) ||
    (node.title || "").toLowerCase().includes(needle)
  );
}

/** A thumbnail, or a hatched placeholder when there is no screenshot to show. */
function thumb(node, className) {
  const box = el("div", { class: className });
  if (node.screenshot) {
    const img = el("img", {
      src: node.screenshot,
      alt: "",
      loading: "lazy",
      draggable: "false",
    });
    // A re-crawl can leave the graph pointing at a file that a failed capture
    // removed; a missing thumbnail must not leave a broken-image icon.
    img.addEventListener("error", () => {
      img.remove();
      box.append(el("div", { class: "shot-missing", text: "no screenshot" }));
    });
    box.append(img);
  } else {
    box.append(
      el("div", {
        class: "shot-missing",
        text: node.failed ? "failed to render" : "no screenshot",
      })
    );
  }
  return box;
}

/* ------------------------------------------------------- loading + layout */

async function init() {
  wireEvents();

  let graph;
  try {
    const response = await fetch("graph.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    graph = await response.json();
  } catch (error) {
    showEmpty(
      "Could not load graph.json — " + String(error),
      "Run sitegraph crawl from the directory you are serving."
    );
    return;
  }

  // Valid JSON is not necessarily *our* JSON — a truncated or hand-edited file
  // would otherwise throw on the first property access and leave a blank page.
  if (!graph || typeof graph !== "object" || !Array.isArray(graph.nodes)) {
    showEmpty(
      "graph.json is not a sitegraph graph.",
      "Expected an object with a nodes array — re-run sitegraph crawl to regenerate it."
    );
    return;
  }

  state.nodes = graph.nodes || [];
  state.edges = (graph.edges || []).filter(
    (edge) => edge && edge.source !== edge.target
  );
  for (const node of state.nodes) state.byId.set(node.id, node);

  const root = state.byId.get(graph.root);
  $("root-label").textContent = root ? pathOf(root.url) : "";
  $("root-label").title = root ? root.url : "";
  document.title = root ? `sitegraph — ${pathOf(root.url)}` : "sitegraph";

  if (!state.nodes.length) {
    showEmpty(
      "This crawl captured no pages.",
      "Run sitegraph crawl <url> and reload."
    );
    return;
  }

  buildLayout();
  renderGraph();
  renderSheet();

  const rootNode = root || state.nodes[0];
  state.nodeEls.get(rootNode.id)?.classList.add("root");
  fit();
  select(rootNode.id, { center: false });
  await settle();
  fit(); // frame the relaxed layout, not the spiral seed
}

function buildLayout() {
  // Seed on a golden-angle spiral whose radius grows with crawl depth, so the
  // graph starts out already meaning something — root in the middle, each
  // level outwards — and the force pass only has to tidy up overlap.
  const golden = Math.PI * (3 - Math.sqrt(5));
  state.nodes.forEach((node, index) => {
    const depth = Math.min(node.depth || 0, 8);
    const radius = 60 + depth * 235;
    const angle = index * golden;
    state.pos.set(node.id, {
      x: ORIGIN + Math.cos(angle) * radius,
      y: ORIGIN + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      pinned: false,
    });
  });
}

/** One force step: pairwise repulsion, edge springs, gravity to the centre. */
function tick(points, springs, repulsion) {
  const count = points.length;

  for (let i = 0; i < count; i++) {
    const a = points[i];
    for (let j = i + 1; j < count; j++) {
      const b = points[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) {
        // Coincident nodes have no direction to separate along; nudge them
        // deterministically apart so the result is stable across reloads.
        dx = ((i % 7) - 3) * 0.5 + 0.1;
        dy = ((j % 5) - 2) * 0.5 + 0.1;
        d2 = dx * dx + dy * dy;
      }
      const distance = Math.sqrt(d2);
      const force = repulsion / d2;
      const ux = (dx / distance) * force;
      const uy = (dy / distance) * force;
      a.vx += ux;
      a.vy += uy;
      b.vx -= ux;
      b.vy -= uy;
    }
  }

  for (let i = 0; i < springs.length; i += 2) {
    const a = points[springs[i]];
    const b = points[springs[i + 1]];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.sqrt(dx * dx + dy * dy) || 1;
    const force = (distance - SPRING_LENGTH) * SPRING;
    const ux = (dx / distance) * force;
    const uy = (dy / distance) * force;
    a.vx += ux;
    a.vy += uy;
    b.vx -= ux;
    b.vy -= uy;
  }

  for (let i = 0; i < count; i++) {
    const point = points[i];
    if (point.pinned) {
      point.vx = point.vy = 0;
      continue;
    }
    point.vx = (point.vx + (ORIGIN - point.x) * GRAVITY) * DAMPING;
    point.vy = (point.vy + (ORIGIN - point.y) * GRAVITY) * DAMPING;
    point.x = clamp(point.x + point.vx, LAYOUT_MARGIN, WORLD - LAYOUT_MARGIN);
    point.y = clamp(point.y + point.vy, LAYOUT_MARGIN, WORLD - LAYOUT_MARGIN);
  }
}

/** Relax the layout, in frames, so the page stays responsive while it runs. */
async function settle() {
  const ids = state.nodes.map((node) => node.id);
  const index = new Map(ids.map((id, i) => [id, i]));

  // The step works on a flat array of the same point objects the map holds:
  // a Map lookup per pair inside an O(n^2) loop is most of the cost of a pass.
  const points = ids.map((id) => state.pos.get(id));
  const springs = [];
  for (const edge of state.edges) {
    const source = index.get(edge.source);
    const target = index.get(edge.target);
    if (source !== undefined && target !== undefined) springs.push(source, target);
  }

  // O(n^2) per step, so the budget shrinks as the graph grows; the pass is
  // cosmetic after the spiral seed, not load-bearing.
  const count = points.length;
  const repulsion = REPULSION / (1 + count / REPULSION_NODE_SCALE);
  const steps = count <= 60 ? 320 : count <= 200 ? 200 : 120;
  const perFrame = count <= 120 ? 8 : 4;

  for (let done = 0; done < steps; done += perFrame) {
    for (let n = 0; n < perFrame && done + n < steps; n++) {
      tick(points, springs, repulsion);
    }
    paintPositions();
    await new Promise((resolve) => requestAnimationFrame(resolve));
  }

  fitToWorld(points);
  paintPositions();
}

/** Rescale a settled layout into the world box, shrinking only.
 *
 * The clamp in `tick` bounds every point, but a graph whose natural spread is
 * larger than the world would otherwise end up flattened against the edges.
 * Rescaling preserves the shape the simulation produced and guarantees that
 * `fit` has something reasonable to frame. A layout that already fits is left
 * alone, so small graphs keep the spacing the forces chose.
 */
function fitToWorld(points) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of points) {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    maxX = Math.max(maxX, point.x);
    maxY = Math.max(maxY, point.y);
  }
  if (!Number.isFinite(minX)) return;

  const span = Math.max(maxX - minX, maxY - minY);
  const target = WORLD - 2 * LAYOUT_MARGIN;
  if (span <= target || span === 0) return;

  const scale = target / span;
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  for (const point of points) {
    point.x = ORIGIN + (point.x - cx) * scale;
    point.y = ORIGIN + (point.y - cy) * scale;
  }
}

/* -------------------------------------------------------------- rendering */

function renderGraph() {
  const edgeLayer = $("edges");
  const nodeLayer = $("nodes");
  edgeLayer.replaceChildren();
  nodeLayer.replaceChildren();
  state.edgeEls = [];
  state.nodeEls = new Map();

  // Arrowheads are markers, so their colour comes from here rather than CSS —
  // read from the custom properties so the two cannot drift apart.
  const styles = getComputedStyle(document.documentElement);
  const colour = (name) => styles.getPropertyValue(name).trim() || "#6b7484";
  edgeLayer.append(
    el(
      "defs",
      {},
      marker("arrow", colour("--line")),
      marker("arrow-out", colour("--out")),
      marker("arrow-in", colour("--in"))
    )
  );

  for (const edge of state.edges) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("class", "edge");
    line.setAttribute("marker-end", "url(#arrow)");
    edgeLayer.append(line);
    state.edgeEls.push(Object.assign({ el: line }, edge));
  }

  for (const node of state.nodes) {
    const card = el(
      "div",
      {
        class: `node${node.failed ? " failed" : ""}`,
        "data-id": node.id,
        title: node.url,
      },
      thumb(node, "thumb"),
      el(
        "div",
        { class: "meta" },
        el("div", {
          class: "title",
          text: titleOf(node) || pathOf(node.url),
        }),
        el("div", { class: "path", text: pathOf(node.url) })
      )
    );
    card.style.marginLeft = `${-CARD_W / 2}px`;
    card.style.marginTop = `${-CARD_H / 2}px`;
    nodeLayer.append(card);
    state.nodeEls.set(node.id, card);
  }

  paintPositions();
  applyFilter();
}

function marker(id, color) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "marker");
  node.setAttribute("id", id);
  node.setAttribute("viewBox", "0 0 10 10");
  node.setAttribute("refX", "9"); // place the tip, not the middle, on the endpoint
  node.setAttribute("refY", "5");
  // Sized in user space rather than the default `strokeWidth` units: with the
  // default, the same marker renders at a different size on a highlighted edge
  // (stroke 2.5) than on a plain one (stroke 1.5), and at a size that depends
  // on the stroke in a way that is hard to reason about. In user space the
  // arrow is a fixed 18 world units — always in proportion to the 168-unit
  // cards, and it scales with the zoom like everything else.
  node.setAttribute("markerUnits", "userSpaceOnUse");
  node.setAttribute("markerWidth", "18");
  node.setAttribute("markerHeight", "18");
  node.setAttribute("orient", "auto");
  const tip = document.createElementNS("http://www.w3.org/2000/svg", "path");
  tip.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  tip.setAttribute("fill", color);
  node.append(tip);
  return node;
}

/** Move the cards and re-draw the edges at their current layout positions. */
function paintPositions() {
  for (const [id, card] of state.nodeEls) {
    const point = state.pos.get(id);
    if (point) {
      card.style.transform = `translate(${point.x}px, ${point.y}px)`;
    }
  }
  for (const edge of state.edgeEls) {
    const source = state.pos.get(edge.source);
    const target = state.pos.get(edge.target);
    if (!source || !target) continue;
    drawEdge(edge.el, source, target);
  }
}

/** Draw an edge between card centres, trimmed back to each card's border. */
function drawEdge(line, source, target) {
  const halfW = CARD_W / 2;
  const halfH = CARD_H / 2;
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy);
  if (distance < 1) {
    line.setAttribute("x1", source.x);
    line.setAttribute("y1", source.y);
    line.setAttribute("x2", target.x);
    line.setAttribute("y2", target.y);
    return;
  }

  // Scale the unit vector until it leaves the rectangle: the smaller ratio
  // wins, which is the first border it crosses.
  const ux = dx / distance;
  const uy = dy / distance;
  const exit = Math.min(
    ux === 0 ? Infinity : halfW / Math.abs(ux),
    uy === 0 ? Infinity : halfH / Math.abs(uy)
  );
  const pad = 3;
  const start = exit + pad;
  const end = Math.max(start, distance - exit - pad);

  line.setAttribute("x1", source.x + ux * start);
  line.setAttribute("y1", source.y + uy * start);
  line.setAttribute("x2", source.x + ux * end);
  line.setAttribute("y2", source.y + uy * end);
}

function renderSheet() {
  const grid = $("grid");
  grid.replaceChildren();

  for (const node of state.nodes) {
    const tile = el(
      "button",
      { class: "tile", type: "button", "data-id": node.id },
      thumb(node, "thumb"),
      el(
        "div",
        { class: "meta" },
        el("div", { class: "title", text: titleOf(node) || pathOf(node.url) }),
        el("div", { class: "path", text: pathOf(node.url) })
      )
    );
    tile.addEventListener("click", () => select(node.id, { center: true }));
    grid.append(tile);
  }
}

/* ------------------------------------------------------- pan / zoom / fit */

function applyTransform() {
  const { x, y, k } = state.transform;
  $("world").style.transform = `translate(${x}px, ${y}px) scale(${k})`;
}

function viewportSize() {
  const stage = $("stage");
  return { width: stage.clientWidth, height: stage.clientHeight };
}

function zoomAt(clientX, clientY, factor) {
  const stage = $("stage").getBoundingClientRect();
  const mx = clientX - stage.left;
  const my = clientY - stage.top;
  const before = state.transform;
  const k = clamp(before.k * factor, MIN_K, MAX_K);
  if (k === before.k) return;
  const wx = (mx - before.x) / before.k;
  const wy = (my - before.y) / before.k;
  state.transform = { k, x: mx - wx * k, y: my - wy * k };
  applyTransform();
}

function fit() {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of state.pos.values()) {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    maxX = Math.max(maxX, point.x);
    maxY = Math.max(maxY, point.y);
  }
  if (!Number.isFinite(minX)) return;

  const pad = CARD_W;
  minX -= pad;
  minY -= pad;
  maxX += pad;
  maxY += pad;

  const { width, height } = viewportSize();
  const k = clamp(
    Math.min(width / (maxX - minX), height / (maxY - minY)),
    MIN_K,
    1.1
  );
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  state.transform = { k, x: width / 2 - cx * k, y: height / 2 - cy * k };
  applyTransform();
}

/** Bring a node into view without changing the zoom level. */
function centreOn(id) {
  const point = state.pos.get(id);
  if (!point) return;
  const { width, height } = viewportSize();
  const { k } = state.transform;
  state.transform.x = width / 2 - point.x * k;
  state.transform.y = height / 2 - point.y * k;
  applyTransform();
}

/* ------------------------------------------------------------- selection */

function select(id, options = {}) {
  if (!state.byId.has(id)) return;
  state.selected = id;

  for (const [otherId, card] of state.nodeEls) {
    card.classList.toggle("selected", otherId === id);
  }
  for (const tile of document.querySelectorAll(".tile")) {
    tile.classList.toggle("selected", tile.dataset.id === id);
  }
  highlightEdges(id);
  if (options.center) centreOn(id);
  openInspector(id);
}

/** Colour the selected node's edges by direction; fade everything else. */
function highlightEdges(id) {
  for (const edge of state.edgeEls) {
    const out = id !== null && edge.source === id;
    const into = id !== null && edge.target === id;
    const only = out !== into; // a self-link would be both; there are none
    edge.el.classList.toggle("out", only && out);
    edge.el.classList.toggle("in", only && into);
    edge.el.classList.toggle("faded", id !== null && !out && !into);
    edge.el.setAttribute(
      "marker-end",
      `url(#${only ? (out ? "arrow-out" : "arrow-in") : "arrow"})`
    );
  }
}

function clearSelection() {
  state.selected = null;
  for (const card of state.nodeEls.values()) card.classList.remove("selected");
  for (const tile of document.querySelectorAll(".tile")) {
    tile.classList.remove("selected");
  }
  highlightEdges(null);
  $("inspector").hidden = true;
  document.body.classList.remove("inspecting");
}

/* ------------------------------------------------------------- inspector */

async function loadPage(id) {
  if (!state.pages.has(id)) {
    state.pages.set(
      id,
      fetch(`pages/${id}.json`, { cache: "no-store" })
        .then((response) => (response.ok ? response.json() : null))
        .catch(() => null)
    );
  }
  return state.pages.get(id);
}

async function openInspector(id) {
  const node = state.byId.get(id);
  if (!node) return;

  const inspector = $("inspector");
  inspector.hidden = false;
  document.body.classList.add("inspecting");

  const head = el(
    "div",
    { class: "insp-head" },
    thumb(node, "insp-shot"),
    el("button", {
      class: "insp-close",
      text: "×",
      title: "Close (Esc)",
      onclick: clearSelection,
    })
  );
  const body = el("div", { class: "insp-body" });
  inspector.replaceChildren(head, body);

  const title = titleOf(node);
  body.append(
    el("h2", {
      class: title ? "" : "untitled",
      text: title || "(untitled)",
    }),
    el("a", {
      class: "url",
      href: node.url,
      target: "_blank",
      rel: "noreferrer noopener",
      text: node.url,
      title: "Open the original URL",
    })
  );

  const chips = el("div", { class: "chips" });
  if (node.failed) {
    chips.append(el("span", { class: "chip bad", text: "failed to render" }));
  } else if (node.status) {
    chips.append(
      el("span", {
        class: `chip ${node.status < 400 ? "ok" : "bad"}`,
        text: String(node.status),
      })
    );
  }
  chips.append(
    el("span", { class: "chip", text: `depth ${node.depth}` }),
    el("span", { class: "chip", text: node.id })
  );
  body.append(chips);

  if (node.failed && node.error) {
    body.append(el("div", { class: "links none", text: node.error }));
  }

  // Both sections take URLs, not IDs: the outgoing list comes from the page
  // record (which stores absolute URLs) and the incoming list is derived from
  // the edges, so the edge's ID has to be resolved back to its URL first.
  const incoming = state.edges
    .filter((edge) => edge.target === id)
    .map((edge) => state.byId.get(edge.source)?.url)
    .filter(Boolean);

  body.append(linkSection("out", "Outgoing", await outgoingTargets(node)));
  body.append(linkSection("in", "Incoming", incoming));
}

/** The page record's full link list; the graph's edges if it is unavailable. */
async function outgoingTargets(node) {
  const record = await loadPage(node.id);
  if (record && Array.isArray(record.links)) return record.links;
  return state.edges
    .filter((edge) => edge.source === node.id)
    .map((edge) => state.byId.get(edge.target)?.url)
    .filter(Boolean);
}

function linkSection(direction, label, targets) {
  const section = el(
    "div",
    { class: `insp-section ${direction}` },
    el(
      "h3",
      {},
      el("span", { class: "swatch" }),
      `${label} (${targets.length})`
    )
  );
  const list = el("ul", { class: "links" });

  if (!targets.length) {
    list.append(el("li", { class: "none", text: "none" }));
  }

  for (const url of targets) {
    const targetId = idForUrl(url);
    if (targetId) {
      const node = state.byId.get(targetId);
      const item = el(
        "li",
        {},
        el(
          "button",
          { type: "button", title: node.url, onclick: () => select(targetId, { center: true }) },
          el("div", { class: "path", text: pathOf(node.url) }),
          el("div", {
            class: "sub",
            text: titleOf(node) || (node.failed ? "failed" : ""),
          })
        )
      );
      list.append(item);
    } else {
      // Real data from the page record, but never captured — most often a page
      // left in the queue by --max-pages. Shown plainly rather than dropped.
      list.append(
        el(
          "li",
          { class: "uncrawled", title: url },
          el(
            "div",
            {},
            el("span", { class: "path", text: pathOf(url) }),
            el("div", { class: "why", text: "not crawled" })
          )
        )
      );
    }
  }

  section.append(list);
  return section;
}

function idForUrl(url) {
  for (const node of state.nodes) if (node.url === url) return node.id;
  return null;
}

/* --------------------------------------------------------------- filtering */

function applyFilter() {
  for (const node of state.nodes) {
    state.nodeEls
      .get(node.id)
      ?.classList.toggle("dim", !matches(node));
  }
  for (const tile of document.querySelectorAll(".tile")) {
    const node = state.byId.get(tile.dataset.id);
    tile.hidden = Boolean(node) && !matches(node);
  }
  updateCounts();
}

function updateCounts() {
  const total = state.nodes.length;
  if (!state.filter) {
    $("counts").textContent = `${total} pages · ${state.edges.length} links`;
    return;
  }
  const shown = state.nodes.filter(matches).length;
  $("counts").textContent = `${shown} / ${total} pages`;
}

function setView(name) {
  $("graph-view").hidden = name !== "graph";
  $("sheet-view").hidden = name !== "sheet";
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.id === `tab-${name}`;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  if (name === "graph") fit();
}

function showEmpty(message, hint) {
  const box = $("empty");
  box.hidden = false;
  box.replaceChildren(el("div", {}, el("p", { text: message }), hint ? el("p", { text: hint }) : null));
}

/* ----------------------------------------------------------------- events */

function wireEvents() {
  $("tab-graph").addEventListener("click", () => setView("graph"));
  $("tab-sheet").addEventListener("click", () => setView("sheet"));

  $("filter").addEventListener("input", (event) => {
    state.filter = event.target.value.trim();
    applyFilter();
  });

  const stage = $("stage");

  stage.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".node")) return;
    if (event.target.closest(".hud")) return;
    stage.classList.add("panning");
    stage.setPointerCapture(event.pointerId);
    const start = {
      x: event.clientX,
      y: event.clientY,
      tx: state.transform.x,
      ty: state.transform.y,
    };
    const move = (moveEvent) => {
      state.transform.x = start.tx + (moveEvent.clientX - start.x);
      state.transform.y = start.ty + (moveEvent.clientY - start.y);
      applyTransform();
    };
    const up = () => {
      stage.classList.remove("panning");
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerup", up);
      stage.removeEventListener("pointercancel", up);
    };
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", up);
    stage.addEventListener("pointercancel", up);
  });

  stage.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      zoomAt(event.clientX, event.clientY, Math.exp(-event.deltaY * 0.0015));
    },
    { passive: false }
  );

  // Node drag is delegated: the cards are recreated on every render.
  $("nodes").addEventListener("pointerdown", (event) => {
    const card = event.target.closest(".node");
    if (!card) return;
    event.stopPropagation();
    const id = card.dataset.id;
    const point = state.pos.get(id);
    if (!point) return;
    const start = { x: event.clientX, y: event.clientY, px: point.x, py: point.y };
    let moved = false;
    card.setPointerCapture(event.pointerId);

    const move = (moveEvent) => {
      const k = state.transform.k;
      const dx = (moveEvent.clientX - start.x) / k;
      const dy = (moveEvent.clientY - start.y) / k;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
      point.x = start.px + dx;
      point.y = start.py + dy;
      point.pinned = true; // a placed node stays where it was put
      paintPositions();
    };
    const up = () => {
      card.removeEventListener("pointermove", move);
      card.removeEventListener("pointerup", up);
      card.removeEventListener("pointercancel", up);
      if (!moved) select(id, { center: false });
    };
    card.addEventListener("pointermove", move);
    card.addEventListener("pointerup", up);
    card.addEventListener("pointercancel", up);
  });

  // zoomAt takes client coordinates, so the centre of the stage has to be
  // taken from the stage's own rect rather than from its size.
  const zoomCentre = (factor) => {
    const rect = $("stage").getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  };

  $("controls").addEventListener("click", (event) => {
    const action = event.target.dataset.zoom;
    if (action === "in") zoomCentre(1.25);
    else if (action === "out") zoomCentre(1 / 1.25);
    else if (action === "reset") fit();
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.tagName === "INPUT") {
      if (event.key === "Escape") event.target.blur();
      return;
    }
    if (event.key === "Escape") clearSelection();
    else if (event.key === "0") fit();
    else if (event.key === "+" || event.key === "=") zoomCentre(1.25);
    else if (event.key === "-") zoomCentre(1 / 1.25);
  });
}

init();
