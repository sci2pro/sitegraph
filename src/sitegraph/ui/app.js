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
/* The height `.node` is given by `--card-h`. The two must agree: this is what
   the edge trim stops short of and what the separation pass keeps apart, and a
   card that is drawn taller than the number here gets edges that stop in mid
   air. `.thumb` (105) + `.meta` (46) + two 1px borders. */
const CARD_H = 153;
const MIN_K = 0.04;
const MAX_K = 3;

/* A card narrower than this on screen is drawn as a mark rather than a card:
   no picture, no text, no shadow. Below roughly this size a thumbnail stops
   being a picture of a page and starts being noise, and 11px type is being
   painted at five. It is deliberately well under the zoom a small graph
   settles at — an eleven-route crawl fits at about 108px a card — so a small
   site looks exactly as it always did. */
const FULL_CARD_PX = 64;

/* The space kept between two neighbouring cards by the separation pass. */
const CARD_GAP = 12;

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

/* How long one frame of the layout pass may spend on arithmetic before it
   hands the thread back to paint. A frame budget rather than a fixed number of
   steps, so it holds on a slow machine and at any graph size. */
const FRAME_MS = 12;

/* How long the whole relaxation may take. A step is O(n²) — measured at 3ms
   for a thousand nodes and 36ms for four thousand — so the number of steps a
   graph can afford falls away as it grows, and the clock is the only honest
   bound on the pass. Past this the layout is simply left as rough as it got:
   it is a cosmetic pass over the spiral seed, not a load-bearing one. A
   spatial index for the repulsion is the way to raise this properly. */
const SETTLE_BUDGET_MS = 1500;

/* How long the packing pass may take. It converges in a few hundred rounds at
   a thousand nodes and cannot finish at all on a graph too big for its
   footprint, so it stops on the clock as well as on the count. */
const SEPARATION_BUDGET_MS = 400;

/* The simulation runs inside the fixed #world box, and the finished layout is
   rescaled to fit it. Without a hard bound, one runaway node stretches the
   bounding box that `fit` frames, which is how you get a blank graph view. */
const LAYOUT_MARGIN = CARD_W;

/* How many times the overlap pass may be re-run on the finished layout. */
const SEPARATION_PASSES = 300;

/* How far into each other two cards may sit before the layout is considered
   unfinished. A packed layout is made of grazing contacts, and chasing the
   last of them costs far more than it shows: at a thousand cards the
   difference between 300 rounds and 2000 is 1.4 seconds and three pairs that
   overlap by a quarter of a card. Anything shallower than this is left alone. */
const OVERLAP_TOLERANCE = 0.25;

const state = {
  nodes: [], // the drawn nodes: one per route, see foldByShape
  all: [], // every crawled page, folded or not
  byId: new Map(), // id -> node, for *every* page
  idByUrl: new Map(), // url -> id, for every page
  repOf: new Map(), // id -> the id of the route it belongs to
  instances: new Map(), // representative id -> every member node
  edges: [],
  nodeEls: new Map(),
  edgePool: [], // <line> elements, grown to the widest view seen so far
  edgeRefs: [], // the edge each pool slot currently holds, for the used prefix
  edgeUsed: 0, // how much of the pool is in use
  drawn: new Map(), // id -> the {hidden, lod} last applied to that card
  selected: null, // the page asked for; may be a folded instance
  highlightId: null, // the *route* it resolves to — what the edges light up
  dragging: null, // the card under the pointer, which must never be culled
  settling: false, // the layout is still moving; see syncView
  filter: "",
  transform: { x: 0, y: 0, k: 1 },
  viewport: { width: 0, height: 0 }, // kept by a ResizeObserver, never measured
  pos: new Map(), // id -> {x, y, vx, vy, pinned}
  pages: new Map(), // id -> page record (fetched lazily)
  sheetBuilt: false, // the contact sheet renders on first open, not at load
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

/** How many pages a drawn node stands for, when it stands for more than one. */
function countBadge(node) {
  const total = node.instances ? node.instances.length : 1;
  if (total < 2) return null;
  return el("span", {
    class: "count",
    text: `×${total}`,
    title: `${total} pages of this route were crawled`,
  });
}

/** Whether a drawn node matches the filter.
 *
 * Its members count, not just the representative: filtering for the page you
 * came to see should not hide the route it lives on.
 */
function matches(node) {
  if (!state.filter) return true;
  const needle = state.filter.toLowerCase();
  return (node.instances || [node]).some(
    (member) =>
      member.url.toLowerCase().includes(needle) ||
      (member.title || "").toLowerCase().includes(needle)
  );
}

/** A thumbnail, or a hatched placeholder when there is no screenshot to show.
 *
 * ``grow`` is for the inspector, where the picture is the point. A capture
 * taller than it is wide is otherwise cropped to its top sliver by
 * `object-fit: cover` in a box shaped like a screen, which is the thing a
 * full-page capture was meant to avoid — so it takes its own height and the
 * inspector scrolls. Asked of the image rather than of the graph, so a tall
 * `--viewport` behaves the same way as `--full-page`. Node cards keep the
 * crop: a card is a thumbnail and one page's height is not a thumbnail.
 *
 * ``full`` asks for the capture itself rather than the card-sized copy, and
 * only the inspector does. An `<img>` decodes at its intrinsic size whatever
 * it is painted at, so pointing a few hundred cards at full captures means the
 * browser decodes a few hundred 1440x900 images to paint each one the size of
 * a full stop — measured at a 251ms worst frame and 792ms of blocked main
 * thread on 500 routes. The copy costs nothing to draw and, at the size a card
 * is actually shown, is what the card was showing anyway.
 *
 * A crawl made before these existed has no ``thumb``, and falls back to the
 * full capture: slower, and still correct. Asking for the copy unconditionally
 * would 404 at a file that was never written.
 */
function thumb(node, className, { grow = false, full = false } = {}) {
  const box = el("div", { class: className });
  const source = full ? node.screenshot : node.thumb || node.screenshot;
  if (source) {
    const img = el("img", {
      src: source,
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
    if (grow) {
      img.addEventListener("load", () => {
        if (img.naturalHeight > img.naturalWidth) box.classList.add("tall");
      });
    }
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

  state.all = graph.nodes || [];
  for (const node of state.all) {
    state.byId.set(node.id, node);
    state.idByUrl.set(node.url, node.id);
  }
  foldByShape();
  state.edges = foldEdges(graph.edges || []);

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

  // Set before the first paint, not inside `settle`: `fit` runs on the seed
  // layout and would otherwise paint one frame at the seed's zoom, which is
  // enough for the browser to start fetching every thumbnail it can see.
  state.settling = true;
  buildLayout();
  renderGraph();

  const rootNode = root || state.nodes[0];
  state.nodeEls.get(rootNode.id)?.classList.add("root");
  fit();
  select(rootNode.id, { center: false });
  await settle();
  fit(); // frame the relaxed layout, not the spiral seed
}

/** Collapse every route's instances into one drawn node.
 *
 * A page that is one instance of `/courses/:id` is not interesting five
 * hundred times over, and drawing them all makes the graph's complexity track
 * the app's *rows* rather than its *routes* — which is the opposite of what
 * the graph is for. Only the drawn list folds: `byId` keeps every page, so the
 * inspector can still open any instance and the links between pages still
 * resolve.
 *
 * Folding here rather than at each use is what keeps the rest of this file
 * unchanged — positions, cards, edges and selection are all keyed by node id
 * and carry on as they were.
 */
function foldByShape() {
  const routes = new Map(); // shape -> members, in id order
  for (const node of state.all) {
    const key = node.shape || node.url;
    if (!routes.has(key)) routes.set(key, []);
    routes.get(key).push(node);
  }

  state.nodes = [];
  for (const [key, members] of routes) {
    // The first member that actually rendered, so a route does not look broken
    // because instance one happened to 404.
    const representative = members.find((node) => !node.failed) || members[0];
    representative.shape = key;
    representative.instances = members;
    state.instances.set(representative.id, members);
    for (const member of members) state.repOf.set(member.id, representative.id);
    state.nodes.push(representative);
  }
}

/** Move the edges onto the routes, dropping what only relates a route to itself.
 *
 * This is where the graph stops being "every link between every page" and
 * becomes "how the routes relate", which is the whole point: otherwise the
 * edges multiply by the instance count too.
 */
function foldEdges(edges) {
  const seen = new Set();
  const folded = [];
  for (const edge of edges) {
    if (!edge) continue;
    const source = state.repOf.get(edge.source);
    const target = state.repOf.get(edge.target);
    if (!source || !target || source === target) continue;
    const key = `${source}->${target}`;
    if (seen.has(key)) continue;
    seen.add(key);
    folded.push({ source, target });
  }
  return folded;
}

/** The route a node belongs to — itself, for a page with no identifier in it. */
function routeOf(id) {
  return state.repOf.get(id) || id;
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

/** The footprint the separation pass must keep clear, shrunk when the world
 * cannot hold that many cards.
 *
 * A card plus its gap is 180 by 165 units, so the world holds about 4100 of
 * them before anything has to overlap. Past that the footprint shrinks and
 * cards overlap gradually, which is better than every card being wedged
 * against the world's edge by a constraint that cannot be satisfied.
 */
function footprintFor(count) {
  const usable = (WORLD - 2 * LAYOUT_MARGIN) ** 2;
  const wanted = count * (CARD_W + CARD_GAP) * (CARD_H + CARD_GAP);
  const scale = Math.min(1, Math.sqrt((usable * 0.9) / wanted));
  return { width: CARD_W * scale, height: CARD_H * scale };
}

/** Push apart any two cards whose boxes touch.
 *
 * Axis-aligned rather than radial, because that is the shape a card actually
 * is: two of them are clear of each other as soon as they differ by a card's
 * width in x *or* a card's height in y. A radial separation would have to
 * clear hypot(168, 153) = 227 in every direction, which caps the world at
 * about 2600 cards instead of 4100.
 *
 * Bucketed by cell, so each card is only compared against the nine cells
 * around it. That makes this O(n) where the repulsion in `tick` is O(n²), and
 * is what lets it run on every step. Cards are visited in index order and
 * pairs taken once, so the pass is deterministic — the layout has to be the
 * same shape on every reload.
 *
 * A pinned card is moved by nobody: a card the user placed stays put.
 *
 * Returns how many pairs are still overlapping by more than `OVERLAP_TOLERANCE`
 * — zero means the layout is as unpacked as it needs to be.
 */
function separate(points, footprint) {
  const width = footprint.width + CARD_GAP;
  const height = footprint.height + CARD_GAP;
  let overlaps = 0;

  const cells = new Map();
  for (let i = 0; i < points.length; i++) {
    const point = points[i];
    const key =
      Math.floor(point.x / width) + "," + Math.floor(point.y / height);
    const bucket = cells.get(key);
    if (bucket) bucket.push(i);
    else cells.set(key, [i]);
  }

  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const cx = Math.floor(a.x / width);
    const cy = Math.floor(a.y / height);
    for (let ox = -1; ox <= 1; ox++) {
      for (let oy = -1; oy <= 1; oy++) {
        const bucket = cells.get(cx + ox + "," + (cy + oy));
        if (!bucket) continue;
        for (const j of bucket) {
          if (j <= i) continue; // once per pair, in index order
          const b = points[j];
          if (a.pinned && b.pinned) continue;

          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const overlapX = width - Math.abs(dx);
          const overlapY = height - Math.abs(dy);
          if (overlapX <= 0 || overlapY <= 0) continue; // already clear
          // Counted only past the tolerance, which is what the caller is
          // waiting for; the push below still takes every overlap apart.
          if (Math.max(overlapX / width, overlapY / height) > OVERLAP_TOLERANCE) {
            overlaps++;
          }

          // Out along the axis they overlap least: the shortest way clear, and
          // it moves the neighbourhood around the least.
          const horizontal = overlapX < overlapY;
          const step = (horizontal ? overlapX : overlapY) * 0.5;
          const sign = (horizontal ? dx : dy) < 0 ? 1 : -1;
          const sx = horizontal ? step * sign : 0;
          const sy = horizontal ? 0 : step * sign;

          // A pinned partner takes none of it, so the free card takes both
          // halves and the pair still ends up clear in one step.
          const free = a.pinned || b.pinned ? 2 : 1;
          if (!a.pinned) {
            a.x += sx * free;
            a.y += sy * free;
          }
          if (!b.pinned) {
            b.x -= sx * free;
            b.y -= sy * free;
          }
        }
      }
    }
  }

  for (const point of points) {
    point.x = clamp(point.x, LAYOUT_MARGIN, WORLD - LAYOUT_MARGIN);
    point.y = clamp(point.y, LAYOUT_MARGIN, WORLD - LAYOUT_MARGIN);
  }

  return overlaps;
}

/** One force step: pairwise repulsion, edge springs, gravity to the centre,
 * then the separation that keeps the cards off each other. */
function tick(points, springs, repulsion, footprint) {
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

  // After the clamp, not before: the clamp is a hard bound, and a separation
  // pass that ran first would have its pushes partly undone.
  separate(points, footprint);
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

  // O(n^2) per step, so a large graph buys fewer steps and a bounded frame
  // rather than a fixed count: 120 steps of a 4000-node graph is a minute of
  // arithmetic spent blocking the tab. The pass is cosmetic after the spiral
  // seed, not load-bearing, so a rougher layout at scale is a fair trade.
  const count = points.length;
  const repulsion = REPULSION / (1 + count / REPULSION_NODE_SCALE);
  const footprint = footprintFor(count);
  const steps = count <= 60 ? 320 : count <= 200 ? 180 : count <= 800 ? 120 : 60;
  const started = performance.now();

  for (let done = 0; done < steps; ) {
    const until = performance.now() + FRAME_MS;
    do {
      tick(points, springs, repulsion, footprint);
      done++;
    } while (done < steps && performance.now() < until);
    paintPositions();
    await new Promise((resolve) => requestAnimationFrame(resolve));
    if (performance.now() - started > SETTLE_BUDGET_MS) break;
  }

  // Rescaling the layout to fit the world scales the separation away with it,
  // so the gap is re-established in the coordinates that actually get drawn.
  //
  // This runs to convergence rather than for a fixed number of rounds, and it
  // is the only thing that sets the graph's density. The force model above has
  // no rest density at all: gravity pulls everything inward and the repulsion
  // is deliberately scaled down as the graph grows, so the equilibrium at a
  // few hundred routes is a clump of cards drawn on top of each other in the
  // middle. Here there is no competing pull, so every round can only reduce
  // the overlap — the packing expands until it fits, and the result is a graph
  // that fills the world instead of piling up in it.
  const shrink = fitToWorld(points);
  const fitted = {
    width: footprint.width * shrink,
    height: footprint.height * shrink,
  };
  const packing = performance.now();
  for (let pass = 0; pass < SEPARATION_PASSES; pass++) {
    if (separate(points, fitted) === 0) break;
    if (performance.now() - packing > SEPARATION_BUDGET_MS) break;
  }

  state.settling = false;
  paintPositions();
}

/** Rescale a settled layout into the world box, shrinking only.
 *
 * The clamp in `tick` bounds every point, but a graph whose natural spread is
 * larger than the world would otherwise end up flattened against the edges.
 * Rescaling preserves the shape the simulation produced and guarantees that
 * `fit` has something reasonable to frame. A layout that already fits is left
 * alone, so small graphs keep the spacing the forces chose.
 *
 * Returns the factor it applied, so the caller can shrink the separation
 * footprint by the same amount and put the gaps back.
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
  if (!Number.isFinite(minX)) return 1;

  const span = Math.max(maxX - minX, maxY - minY);
  const target = WORLD - 2 * LAYOUT_MARGIN;
  if (span <= target || span === 0) return 1;

  const scale = target / span;
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  for (const point of points) {
    point.x = ORIGIN + (point.x - cx) * scale;
    point.y = ORIGIN + (point.y - cy) * scale;
  }
  return scale;
}

/* -------------------------------------------------------------- rendering */

function renderGraph() {
  const edgeLayer = $("edges");
  const nodeLayer = $("nodes");
  edgeLayer.replaceChildren();
  nodeLayer.replaceChildren();
  state.edgePool = [];
  state.edgeRefs = [];
  state.edgeUsed = 0;
  state.drawn = new Map();
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

  // No edges here: they are created on demand by `paintEdges` for the ones
  // actually on screen. Writing out every edge up front is what made a
  // 76,000-link crawl cost a second of start-up and put 76,000 elements on the
  // layer the compositor re-rasters on every pan.

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
        el(
          "div",
          { class: "title" },
          el("span", {
            class: "label",
            text: titleOf(node) || pathOf(node.url),
          }),
          countBadge(node)
        ),
        // The *shape*, not the representative's own path: `/courses/1` would
        // misdescribe the four hundred others standing behind this card.
        el("div", { class: "path", text: pathOf(node.shape) })
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

/** The part of the world the stage is showing, in world units, grown by one
 * card so a card is materialised just before it scrolls into view. */
function visibleRect() {
  const { x, y, k } = state.transform;
  const { width, height } = state.viewport;
  return {
    x0: -x / k - CARD_W,
    y0: -y / k - CARD_H,
    x1: (width - x) / k + CARD_W,
    y1: (height - y) / k + CARD_H,
  };
}

/** Bring the DOM in line with the transform, the viewport and the positions.
 *
 * Called both when only the view moved (`moved: false`, a pan or a zoom) and
 * when the nodes themselves moved (`moved: true`, a settle step or a drag).
 * One loop rather than two, because the decisions are the same either way and
 * the only difference is whether a card has to be repositioned.
 *
 * Two things this must never do. It must not read layout — no `clientWidth`,
 * no `getBoundingClientRect` — because it runs inside the settle loop, where a
 * forced reflow per frame is the cost it exists to remove; the viewport is
 * kept up to date by a ResizeObserver instead. And it must not write a
 * property that has another owner: class names belong to `select`,
 * `clearSelection` and `applyFilter`, positions to the layout, and only
 * `hidden` and `data-lod` belong here. Keeping that boundary is what stops the
 * cache below from clobbering `.selected`, `.root` or `.dim`.
 */
function syncView({ moved = false } = {}) {
  const { k } = state.transform;
  const { width, height } = state.viewport;
  // The contact sheet is showing, or the observer has not fired yet.
  if (!width || !height) return;

  // While the layout is still moving, everything is a mark. The cards are in
  // flight, so what is printed on them is the least useful thing on screen,
  // and it is not free: the seed layout is more compact than the settled one,
  // so the view starts zoomed further in than it ends. Painting pictures and
  // links at that intermediate zoom decodes every thumbnail in the crawl and
  // then throws them away when `fit` pulls back — a few seconds of work, at
  // exactly the moment the page should be arriving.
  const lod =
    !state.settling && k * CARD_W >= FULL_CARD_PX ? "full" : "mark";
  const rect = visibleRect();

  for (const node of state.nodes) {
    const card = state.nodeEls.get(node.id);
    const point = state.pos.get(node.id);
    if (!card || !point) continue;

    const inside =
      point.x >= rect.x0 &&
      point.x <= rect.x1 &&
      point.y >= rect.y0 &&
      point.y <= rect.y1;
    // A card the user is holding, or the one whose links are lit up, stays
    // drawn wherever it is: dragging a card to the edge of the window must not
    // make it vanish from under the pointer, and selecting a page off screen
    // from the instance list must show something.
    const keep =
      inside || node.id === state.dragging || node.id === state.highlightId;

    const applied = state.drawn.get(node.id);
    if (!keep) {
      if (!applied || !applied.hidden) card.hidden = true;
      // The card keeps whatever tier it was drawn at. Recording the current
      // one instead would claim a tier the element never got, and the card
      // would come back at the wrong level of detail for good.
      state.drawn.set(node.id, { hidden: true, lod: applied ? applied.lod : null });
      continue;
    }

    // Position before un-hiding, so a card that was culled at its old spot
    // does not appear there for a frame.
    if (moved || !applied || applied.hidden) {
      card.style.transform = `translate(${point.x}px, ${point.y}px)`;
    }
    if (!applied || applied.hidden) card.hidden = false;
    if (!applied || applied.lod !== lod) card.dataset.lod = lod;
    state.drawn.set(node.id, { hidden: false, lod });
  }

  paintEdges(lod, rect);
}

/** Move the cards to their current layout positions. */
function paintPositions() {
  syncView({ moved: true });
}

/** A fresh edge element. Built with `createElementNS` and `setAttribute`
 * rather than `el()`: an SVG element's `className` is read-only, and `el()`
 * assigns to it. */
function newEdgeLine() {
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  $("edges").append(line); // `append`, never `replaceChildren` — the arrow
  return line; //             markers are a <defs> in the same layer
}

/** The one owner of how an edge looks: its class and its arrowhead.
 *
 * Called both when the selection changes and by the pool when it fills, so an
 * edge that comes into view while something is selected arrives already
 * coloured rather than grey among its lit neighbours.
 */
function applyEdgeStyle(line, source, target) {
  const id = state.highlightId;
  const out = id !== null && source === id;
  const into = id !== null && target === id;
  const only = out !== into; // a self-link would be both; there are none
  const classes = ["edge"];
  if (only) classes.push(out ? "out" : "in");
  else if (id !== null) classes.push("faded");
  line.setAttribute("class", classes.join(" "));
  line.setAttribute(
    "marker-end",
    `url(#${only ? (out ? "arrow-out" : "arrow-in") : "arrow"})`
  );
}

/** Draw the edges that join two cards on screen, and no others.
 *
 * Below `full` this draws nothing at all: an arrow only says something when
 * you can see both of the cards it joins, and a few thousand lines across a
 * field of marks is a grey haze that costs more to paint than the marks do.
 *
 * The pool is grown, never rebuilt, and only ever appended to. Slots past the
 * used prefix keep their geometry but lose their class and are hidden, so
 * `line.edge` still counts exactly the edges being drawn — which is what the
 * tests count.
 */
function paintEdges(lod, rect) {
  const pool = state.edgePool;
  const refs = state.edgeRefs;
  const onScreen = (point) =>
    point.x >= rect.x0 &&
    point.x <= rect.x1 &&
    point.y >= rect.y0 &&
    point.y <= rect.y1;

  let used = 0;
  if (lod === "full") {
    // A scan of every edge, per pass. At a few thousand routes that is well
    // under a millisecond, and the alternative — an index of which nodes are
    // drawn, kept in step with the pass above — is a second thing to keep
    // correct for no gain at the size this is built for.
    for (const edge of state.edges) {
      const source = state.pos.get(edge.source);
      const target = state.pos.get(edge.target);
      if (!source || !target) continue;
      if (!onScreen(source) || !onScreen(target)) continue;
      const line = pool[used] || (pool[used] = newEdgeLine());
      // Every activated slot, not just the new ones: a reused slot still holds
      // the last edge's geometry, and would otherwise flash a line across the
      // screen for a frame.
      drawEdge(line, source, target);
      applyEdgeStyle(line, edge.source, edge.target);
      line.hidden = false;
      refs[used] = edge;
      used++;
    }
  }

  for (let index = used; index < state.edgeUsed; index++) {
    pool[index].hidden = true;
    pool[index].removeAttribute("class");
  }
  state.edgeUsed = used;
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

/** Build the contact sheet.
 *
 * On first open rather than at load: a tile is a button with a thumbnail and
 * two lines of text, and a few thousand of them is work no one asked for while
 * the graph tab is showing. `setView` calls this.
 */
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
        el(
          "div",
          { class: "title" },
          el("span", {
            class: "label",
            text: titleOf(node) || pathOf(node.url),
          }),
          countBadge(node)
        ),
        el("div", { class: "path", text: pathOf(node.shape) })
      )
    );
    tile.addEventListener("click", () => select(node.id, { center: true }));
    grid.append(tile);
  }

  // Both of these may already be set — the sheet is built on first open, which
  // can be long after a filter was typed or a page selected.
  paintSelection();
  applyFilter();
}

/* ------------------------------------------------------- pan / zoom / fit */

/** Capture the pointer, tolerating one that is already gone.
 *
 * `setPointerCapture` throws if the pointer is no longer active — a pointer
 * released between the event and this call, or a synthetic event. Letting that
 * escape would abandon the drag halfway through setting it up and surface as
 * an uncaught error, for a gesture the user can simply start again.
 */
function capturePointer(element, pointerId) {
  try {
    element.setPointerCapture(pointerId);
  } catch {
    /* the drag still works; it just is not tracked outside the element */
  }
}

function applyTransform() {
  const { x, y, k } = state.transform;
  $("world").style.transform = `translate(${x}px, ${y}px) scale(${k})`;
  // Synchronously, not on a frame: it is O(n) comparisons with a change check,
  // and a view that lags the transform by a frame is a view that shows cards
  // where they no longer are.
  syncView();
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
  paintSelection();
  // A folded instance selects its route — that is the card on screen — while
  // the inspector still opens the page that was actually asked for.
  if (options.center) centreOn(routeOf(id));
  openInspector(id);
}

/** Mark the selected route wherever it is drawn: the card in the graph, the
 * tile in the contact sheet, and the edges joining it.
 *
 * One owner, called from `select`, `clearSelection` and `renderSheet` — the
 * sheet is built on first open now, so a selection made before that has to be
 * applied to tiles that did not exist when it happened.
 */
function paintSelection() {
  // `state.selected` is the page that was asked for, which may be one instance
  // of a folded route. What gets drawn — and what lights up — is the route.
  const drawn = state.selected === null ? null : routeOf(state.selected);
  state.highlightId = drawn;

  for (const [id, card] of state.nodeEls) {
    card.classList.toggle("selected", id === drawn);
  }
  for (const tile of document.querySelectorAll(".tile")) {
    tile.classList.toggle("selected", tile.dataset.id === drawn);
  }

  const { edgePool, edgeRefs, edgeUsed } = state;
  for (let index = 0; index < edgeUsed; index++) {
    applyEdgeStyle(edgePool[index], edgeRefs[index].source, edgeRefs[index].target);
  }

  // The selected card is never culled, so a selection made off screen has to
  // be drawn before it can be seen.
  syncView();
}

function clearSelection() {
  state.selected = null;
  paintSelection();
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
    thumb(node, "insp-shot", { grow: true, full: true }),
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
  if (node.shape && node.shape !== node.url) {
    chips.append(
      el("span", {
        class: "chip",
        text: pathOf(node.shape),
        title: "the route this page is one instance of",
      })
    );
  }
  body.append(chips);

  if (node.failed && node.error) {
    body.append(el("div", { class: "links none", text: node.error }));
  }

  const instances = instanceSection(node);
  if (instances) body.append(instances);

  // Both sections take URLs, not IDs: the outgoing list comes from the page
  // record (which stores absolute URLs) and the incoming list is derived from
  // the edges, so the edge's ID has to be resolved back to its URL first.
  //
  // Outgoing is this *page*'s links, which is what its record holds. Incoming
  // is this page's *route* being linked to, because that is the level the
  // edges were folded to — a route is a more useful answer than one arbitrary
  // instance of it.
  const incoming = state.edges
    .filter((edge) => edge.target === routeOf(id))
    .map((edge) => state.byId.get(edge.source)?.url)
    .filter(Boolean);

  body.append(linkSection("out", "Outgoing", await outgoingTargets(node)));
  body.append(linkSection("in", "Incoming", incoming));
}

/** How many instances of a route the inspector will list before summarising. */
const INSTANCES_SHOWN = 40;

/** The pages standing behind one drawn node, so a folded page stays reachable. */
function instanceSection(node) {
  const members = state.instances.get(routeOf(node.id));
  if (!members || members.length < 2) return null;

  const section = el(
    "div",
    { class: "insp-section instances" },
    el(
      "h3",
      {},
      el("span", { class: "swatch" }),
      `Instances (${members.length})`
    )
  );
  const list = el("ul", { class: "links" });

  for (const member of members.slice(0, INSTANCES_SHOWN)) {
    list.append(
      el(
        "li",
        {},
        el(
          "button",
          {
            type: "button",
            class: member.id === node.id ? "current" : "",
            title: member.url,
            onclick: () => select(member.id, { center: false }),
          },
          el("div", { class: "path", text: pathOf(member.url) }),
          el("div", {
            class: "sub",
            text: member.failed
              ? "failed to render"
              : [member.status, titleOf(member)].filter(Boolean).join(" · "),
          })
        )
      )
    );
  }
  section.append(list);

  if (members.length > INSTANCES_SHOWN) {
    section.append(
      el("div", {
        class: "links none",
        text: `…and ${members.length - INSTANCES_SHOWN} more`,
      })
    );
  }
  return section;
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
  // Every page, not just the drawn ones — a folded instance is still a real
  // page, and reporting it as "not crawled" would be a lie.
  return state.idByUrl.get(url) ?? null;
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
  const routes = state.nodes.length;
  const pages = state.all.length;
  const shown = state.filter ? state.nodes.filter(matches).length : routes;

  const parts = [`${shown === routes ? routes : `${shown} / ${routes}`} routes`];
  // Only worth saying when the two differ — which is exactly when folding did
  // something, and is the number that used to be the whole graph.
  if (pages !== routes) parts.push(`${pages} pages`);
  parts.push(`${state.edges.length} links`);
  $("counts").textContent = parts.join(" · ");
}

function setView(name) {
  $("graph-view").hidden = name !== "graph";
  $("sheet-view").hidden = name !== "sheet";
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.id === `tab-${name}`;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  // After the tab has been switched, so the click lands before the sheet is
  // built; and before `fit`, which has nothing to do with the graph tab.
  if (name === "sheet" && !state.sheetBuilt) {
    state.sheetBuilt = true;
    renderSheet();
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

  // The stage's size as the culler sees it, kept without measuring it: reading
  // clientWidth in the pass would force a reflow on every frame of the settle
  // loop, and would read 0x0 while the contact sheet is showing — from which
  // every card would be culled. The observer also catches the 380px the
  // inspector takes when it opens, which fires no window resize event.
  state.viewport = viewportSize();
  new ResizeObserver((entries) => {
    const { width, height } = entries[entries.length - 1].contentRect;
    if (!width || !height) return; // hidden behind the other tab
    state.viewport = { width, height };
    syncView();
  }).observe(stage);

  stage.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".node")) return;
    if (event.target.closest(".hud")) return;
    stage.classList.add("panning");
    capturePointer(stage, event.pointerId);
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
    capturePointer(card, event.pointerId);
    // Held: a card dragged to the edge of the window stays drawn, rather than
    // being culled out from under the pointer that is dragging it.
    state.dragging = id;

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
      state.dragging = null;
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
