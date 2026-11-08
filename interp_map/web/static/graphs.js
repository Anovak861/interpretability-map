"use strict";
/* Views built to stay readable as the map grows:
   - the lineage timeline: documents in horizontal bands by region, sized by importance
     (how much later work builds on them), with labels appearing as you zoom in;
   - the concept map: concepts clustered inside their regions; zoomed out it collapses into
     a map of the field's regions.
   Loaded before app.js; uses its helpers at call time. */

const GRAPH_FONT = "Inter, system-ui, sans-serif";

function hashString(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) h = (Math.imul(31, h) + str.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

/* Pinned-selection panel shared by both graphs. */
function graphPanel(html) {
  const p = $("#panel");
  if (!html) { p.style.display = "none"; return; }
  p.innerHTML = html;
  p.style.display = "block";
}

/* Run fn once on the next frame, or after a short timeout if frames aren't being painted
   (background tab, minimised window); repeated calls before it runs are coalesced. */
function scheduler(fn) {
  let raf = 0, timer = 0;
  return () => {
    cancelAnimationFrame(raf);
    clearTimeout(timer);
    const run = () => { cancelAnimationFrame(raf); clearTimeout(timer); fn(); };
    raf = requestAnimationFrame(run);
    timer = setTimeout(run, 80);
  };
}

/* Measures a label as Cytoscape will draw it: words wrapped at maxWidth, in the given font.
   Results are cached; labels are drawn at a constant on-screen size so zoom doesn't matter. */
function labelMeasurer(font, maxWidth) {
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.font = font;
  const cache = new Map();
  return (text) => {
    if (cache.has(text)) return cache.get(text);
    let lines = 1, line = "", widest = 0;
    for (const word of text.split(/\s+/)) {
      const attempt = line ? `${line} ${word}` : word;
      if (ctx.measureText(attempt).width <= maxWidth || !line) line = attempt;
      else { widest = Math.max(widest, ctx.measureText(line).width); line = word; lines++; }
    }
    widest = Math.min(maxWidth, Math.max(widest, ctx.measureText(line).width));
    const out = { width: widest, lines };
    cache.set(text, out);
    return out;
  };
}

/* Cytoscape only notices window resizes; also follow the container's own size changes
   (layout settling, side panels opening) and re-run `onResize` afterwards. */
function watchSize(cy, onResize) {
  let last = "";
  const ro = new ResizeObserver(() => {
    const el = cy.container();
    if (!el) return ro.disconnect();
    const size = `${el.clientWidth}x${el.clientHeight}`;
    if (size === last) return;
    last = size;
    cy.resize();
    onResize();
  });
  ro.observe(cy.container());
  cy.on("destroy", () => ro.disconnect());
}

/* Legends can be collapsed to a small button; the choice is remembered per browser. */
function wireLegend() {
  const legend = $(".graph-legend");
  if (!legend) return;
  let collapsed = false;
  try { collapsed = localStorage.getItem("interp-map-legend") === "collapsed"; } catch { /* no storage */ }
  legend.insertAdjacentHTML("afterbegin", `<button class="legend-toggle" type="button" aria-label="Hide legend">×</button>`);
  const toggle = () => {
    legend.classList.toggle("collapsed", collapsed);
    $(".legend-toggle", legend).textContent = collapsed ? "Legend" : "×";
    $(".legend-toggle", legend).setAttribute("aria-label", collapsed ? "Show legend" : "Hide legend");
  };
  $(".legend-toggle", legend).addEventListener("click", () => {
    collapsed = !collapsed;
    try { localStorage.setItem("interp-map-legend", collapsed ? "collapsed" : "open"); } catch { /* no storage */ }
    toggle();
  });
  toggle();
}

function hoverTips(cy, tipHtml, { hood = true } = {}) {
  const tip = $("#tip");
  cy.on("mouseover", "node", (e) => {
    const n = e.target;
    if (!n.data("n") || cy.scratch("pinned")) return;
    tip.innerHTML = tipHtml(n.data("n"));
    tip.style.display = "block";
    if (hood) {
      const h = n.closedNeighborhood().filter((x) => x.isEdge() || x.data("n"));
      cy.elements().not(h).not(".band, :parent").addClass("faded");
      h.edges().addClass("hl");
    }
  });
  cy.on("mousemove", "node", (e) => {
    const box = $(".graph-wrap").getBoundingClientRect();
    const x = e.originalEvent.clientX - box.left, y = e.originalEvent.clientY - box.top;
    tip.style.left = Math.max(8, Math.min(x + 14, box.width - 330)) + "px";
    tip.style.top = y + 14 + "px";
  });
  cy.on("mouseout", "node", () => {
    tip.style.display = "none";
    if (!cy.scratch("pinned")) cy.elements().removeClass("faded hl");
  });
}

/* ================================================================ lineage */

const lineageState = { group: true, links: "auto", region: "" };

async function renderLineage(_, params) {
  const g = await api("/api/graph/lineage");
  app.className = "wide";
  const focus = params.get("focus");
  const links = lineageState.links === "auto" ? (g.edges.length > 250 ? "hover" : "all") : lineageState.links;
  const regionName = Object.fromEntries(g.regions.map((r) => [r.slug, r.name]));

  app.innerHTML = `
    <div class="page-head">
      <div><h1>Lineage</h1><p>How documents build on each other, left to right through time${lineageState.group ? ", in bands by region of the field" : ""}.
        Bigger nodes are more influential: more later work cites them or engages with claims they proposed. Zoom in for more labels; click a document to pin it and trace its lineage.</p></div>
      <div class="actions">
        <div class="seg" id="lg-group"><button data-v="1" class="${lineageState.group ? "on" : ""}">By region</button><button data-v="0" class="${lineageState.group ? "" : "on"}">One timeline</button></div>
        <div class="seg" id="lg-links" title="Citation links"><button data-v="all" class="${links === "all" ? "on" : ""}">All links</button><button data-v="hover" class="${links === "hover" ? "on" : ""}">Links on hover</button></div>
        ${lineageState.group && g.regions.length ? `<select id="lg-region" aria-label="Highlight region"><option value="">All regions</option>
          ${g.regions.map((r) => `<option value="${esc(r.slug)}" ${lineageState.region === r.slug ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select>` : ""}
      </div>
    </div>
    <div class="card graph-wrap">
      <div class="graph" id="cy"></div>
      <div class="axis-y" id="axis-bands" aria-hidden="true"></div>
      <div class="axis-x" id="axis-years" aria-hidden="true"></div>
      <div class="graph-tip" id="tip"></div>
      <div class="graph-panel" id="panel"></div>
      <div class="graph-controls"><button class="btn" id="fit">Fit</button></div>
      <div class="graph-legend">${Object.keys(SOURCE_LABEL).filter((s) => g.nodes.some((n) => n.source === s))
        .map((s) => `<div class="row">${src(s)}</div>`).join("")}
        <div class="row small">Size = influence on later work${g.edges.some((e) => e.link_type === "inferred_influence") ? " · dashed = inferred" : ""}</div></div>
    </div>`;
  $$("#lg-group button").forEach((b) => b.addEventListener("click", () => { lineageState.group = b.dataset.v === "1"; render({ keepScroll: true }); }));
  $$("#lg-links button").forEach((b) => b.addEventListener("click", () => { lineageState.links = b.dataset.v; render({ keepScroll: true }); }));
  const sel = $("#lg-region");
  if (sel) sel.addEventListener("change", () => { lineageState.region = sel.value; highlightRegion(); labelForZoom(); });

  // ---- bands: regions ordered so that regions citing each other sit next to each other
  const bySlug = Object.fromEntries(g.nodes.map((n) => [n.slug, n]));
  const bandOf = (n) => (lineageState.group ? n.region || "_other" : "_all");
  const bandIds = [...new Set(g.nodes.map(bandOf))];
  const size = (b) => g.nodes.filter((n) => bandOf(n) === b).length;
  const cross = {};
  g.edges.forEach((e) => {
    const a = bandOf(bySlug[e.source]), b = bandOf(bySlug[e.target]);
    if (a !== b) { cross[a + "|" + b] = (cross[a + "|" + b] || 0) + 1; cross[b + "|" + a] = (cross[b + "|" + a] || 0) + 1; }
  });
  const real = bandIds.filter((b) => b !== "_other").sort((a, b) => size(b) - size(a));
  const order = real.length ? [real.shift()] : [];
  while (real.length) {
    const last = order[order.length - 1];
    real.sort((a, b) => (cross[last + "|" + b] || 0) - (cross[last + "|" + a] || 0) || size(b) - size(a));
    order.push(real.shift());
  }
  if (bandIds.includes("_other")) order.push("_other");
  const bandLabel = (b) => (b === "_other" ? "Other" : b === "_all" ? "All documents" : regionName[b] || b);

  // ---- positions: x = date; within a band, greedy lanes so nearby nodes don't collide
  const t = (d) => new Date(d.slice(0, 10)).getTime();
  const dated = g.nodes.filter((n) => n.published_date);
  const t0 = Math.min(...dated.map((n) => t(n.published_date)));
  const t1 = Math.max(...dated.map((n) => t(n.published_date)));
  const years = (t1 - t0) / (365 * 864e5) || 1;
  const pxPerYear = Math.max(220, Math.min(520, (g.nodes.length * 9) / years));
  const xOf = (d) => ((t(d) - t0) / (365 * 864e5)) * pxPerYear;
  const laneH = 26, minGap = 70, bandPad = 26;
  const pos = {}, bands = [];
  let y = 0;
  const maxX = xOf(new Date(t1).toISOString());
  order.forEach((b) => {
    // important documents claim lanes first, so they sit near the band's top edge
    const members = g.nodes.filter((n) => bandOf(n) === b)
      .sort((a, c) => (c.percentile || 0) - (a.percentile || 0));
    const lanes = [];
    members.forEach((n) => {
      const x = n.published_date ? xOf(n.published_date) : maxX + minGap;
      let lane = lanes.findIndex((xs) => xs.every((o) => Math.abs(o - x) >= minGap));
      if (lane === -1) { lane = lanes.length; lanes.push([]); }
      lanes[lane].push(x);
      pos[n.slug] = { x, y: y + bandPad + lane * laneH };
    });
    const h = bandPad * 2 + Math.max(0, lanes.length - 1) * laneH;
    bands.push({ id: b, y0: y, h, n: members.length });
    y += h;
  });

  const text = cssVar("--text"), border = cssVar("--border-strong"), faint = cssVar("--faint"),
    muted = cssVar("--muted"), accent = cssVar("--accent"), surface2 = cssVar("--surface-2");
  const width = maxX + 2 * minGap;
  const yearTicks = [];
  for (let yr = new Date(t0).getFullYear() + 1; yr <= new Date(t1).getFullYear(); yr++) yearTicks.push([yr, xOf(`${yr}-01-01`)]);
  const cy = cytoscape({
    container: $("#cy"),
    elements: [
      ...bands.map((b, i) => ({ data: { id: `band:${b.id}`, w: width + 260, h: b.h, shade: i % 2 }, classes: "band",
        position: { x: width / 2 - 130, y: b.y0 + b.h / 2 } })),
      ...g.nodes.map((n) => {
        const p = n.percentile || 0;
        return { data: { id: n.slug, label: n.title.length > 60 ? n.title.slice(0, 58) + "…" : n.title, color: cssVar(`--src-${n.source}`) || faint,
          size: 7 + 26 * Math.pow(p, 1.6), p, n }, position: pos[n.slug] };
      }),
      ...g.edges.map((e, i) => ({ data: { id: `e${i}`, source: e.source, target: e.target },
        classes: (e.link_type === "inferred_influence" ? "inferred " : "") + (links === "hover" ? "hide" : "") })),
    ],
    style: [
      { selector: "node", style: { "background-color": "data(color)", width: "data(size)", height: "data(size)",
        label: "", color: text, "font-family": GRAPH_FONT, "text-valign": "bottom", "text-margin-y": 3, "text-wrap": "wrap",
        "border-width": 1, "border-color": cssVar("--surface"), "z-index": 10 } },
      // labels are switched on by importance for the current zoom (see labelForZoom)
      { selector: "node.lbl", style: { label: "data(label)", "text-outline-width": 2, "text-outline-color": cssVar("--surface"),
        "text-outline-opacity": 0.85 } },
      { selector: "node.band", style: { shape: "rectangle", width: "data(w)", height: "data(h)", "background-color": surface2,
        "background-opacity": 0.55, "border-width": 0, label: "", events: "no", "z-index": 0 } },
      { selector: "node.band[shade = 1]", style: { "background-opacity": 0.2 } },
      { selector: "node.band.hot", style: { "background-color": accent, "background-opacity": 0.09 } },
      { selector: "edge", style: { width: 1, "line-color": border, "target-arrow-color": border, "target-arrow-shape": "triangle",
        "arrow-scale": .5, "curve-style": "bezier", opacity: g.edges.length > 120 ? .22 : .5, "z-index": 5 } },
      { selector: "edge.inferred", style: { "line-style": "dashed" } },
      { selector: "edge.hide", style: { display: "none" } },
      { selector: ".faded", style: { opacity: .1 } },
      { selector: "edge.hl", style: { display: "element", "line-color": accent, "target-arrow-color": accent, width: 1.6, opacity: .9 } },
      { selector: "node.pin", style: { "border-width": 3, "border-color": accent, "z-index": 20 } },
      { selector: "node.dim", style: { opacity: .12 } },
    ],
    layout: { name: "preset", padding: 30 },
    wheelSensitivity: 0.25, minZoom: 0.08, maxZoom: 3,
  });
  cyInstances.push(cy);
  cy.$(".band").ungrabify().unselectify();

  // Zoom-aware labels: the most influential documents are labelled first, at a constant
  // on-screen size; a label is placed only where its measured, wrapped box overlaps no other
  // label and no labelled document's dot. The year ruler is an HTML overlay.
  const docs = cy.nodes().filter((x) => x.data("n"));
  const axisBands = $("#axis-bands"), axisYears = $("#axis-years");
  const byImportance = docs.toArray().sort((a, b) => b.data("p") - a.data("p"));
  const LABEL_W = 170, FONT_PX = 11, LINE_H = 13.5, GAP = 4, MAX_LABELS = 80;
  const measure = labelMeasurer(`${FONT_PX}px ${GRAPH_FONT}`, LABEL_W);
  const labelForZoom = () => {
    const z = cy.zoom(), pan = cy.pan(), w = cy.width(), h = cy.height();
    const boxes = [];
    const pinned = cy.scratch("pinned");
    const order = pinned ? [cy.$id(pinned)[0], ...byImportance] : byImportance;
    const show = new Set();
    const hits = (b) => boxes.some((o) => b.x1 < o.x2 && b.x2 > o.x1 && b.y1 < o.y2 && b.y2 > o.y1);
    for (const node of order) {
      if (show.size >= MAX_LABELS) break;
      const r = node.renderedPosition(), half = (node.data("size") * z) / 2;
      if (r.x < -100 || r.x > w + 100 || r.y < -40 || r.y > h + 40) continue;   // off screen
      const { width: lw, lines } = measure(node.data("label"));
      const label = { x1: r.x - lw / 2 - GAP, x2: r.x + lw / 2 + GAP, y1: r.y + half + 1, y2: r.y + half + 3 + lines * LINE_H + GAP };
      const dot = { x1: r.x - half - 2, x2: r.x + half + 2, y1: r.y - half - 2, y2: r.y + half + 2 };
      if (node.id() !== pinned && (hits(label) || hits(dot))) continue;
      boxes.push(label, dot);
      show.add(node.id());
    }
    cy.batch(() => {
      docs.forEach((x) => x.toggleClass("lbl", show.has(x.id())));
      cy.style().selector("node.lbl").style({ "font-size": FONT_PX / z, "text-max-width": LABEL_W / z, "text-margin-y": 3 / z }).update();
    });
    // verification pass with Cytoscape's own label boxes (the estimate above can be a few
    // pixels off for long hyphenated words); a lower-ranked label that still collides is dropped
    const kept = [];
    for (const node of order) {
      if (!show.has(node.id())) continue;
      const b = node.renderedBoundingBox({ includeNodes: false, includeLabels: true });
      if (node.id() !== pinned && kept.some((o) => b.x1 < o.x2 + 2 && b.x2 > o.x1 - 2 && b.y1 < o.y2 + 2 && b.y2 > o.y1 - 2)) {
        node.removeClass("lbl");
      } else {
        kept.push(b);
      }
    }
    axisYears.innerHTML = yearTicks.map(([yr, x]) => `<div class="axis-year" style="left:${x * z + pan.x}px">${yr}</div>`).join("");
    showBandName();
  };

  // band names appear for the band under the pointer (and for a region chosen in the menu)
  let hoverBand = null;
  function showBandName() {
    const z = cy.zoom(), pan = cy.pan();
    const visible = [...new Set([hoverBand, lineageState.region || null].filter(Boolean))];
    cy.$(".band").removeClass("hot");
    axisBands.innerHTML = visible.map((id) => {
      const b = bands.find((x) => x.id === id);
      if (!b) return "";
      cy.$id(`band:${b.id}`).addClass("hot");
      return `<div class="axis-band" style="top:${Math.max(28, b.y0 * z + pan.y + 6)}px">${esc(bandLabel(b.id))} <span>${plural(b.n, "document")}</span></div>`;
    }).join("");
  }
  cy.on("mousemove", (e) => {
    if (!e.position) return;
    const yy = e.position.y;
    const b = bands.find((x) => yy >= x.y0 && yy < x.y0 + x.h);
    const id = b ? b.id : null;
    if (id !== hoverBand) { hoverBand = id; showBandName(); }
  });
  cy.container().addEventListener("mouseleave", () => { hoverBand = null; showBandName(); });
  cy.on("viewport", scheduler(labelForZoom));

  const docTip = (n) => `<div class="eyebrow">${esc(regionName[n.region] || "Other")}</div><b>${esc(n.title)}</b>${esc(authorsShort(n.authors))}<br>${fmtDate(n.published_date)} · ${esc(SOURCE_LABEL[n.source] || n.source)}` +
    `<div class="muted" style="margin-top:4px">cited by ${n.cited_by || 0} later · ${n.idea_uptake || 0} engage with its claims</div>`;
  hoverTips(cy, docTip);
  wireLegend();

  const unpin = () => { cy.scratch("pinned", null); cy.elements().removeClass("faded hl pin"); graphPanel(null); highlightRegion(); };
  const pin = (node, { trace = false } = {}) => {
    cy.scratch("pinned", node.id());
    cy.elements().removeClass("faded hl pin");
    const n = node.data("n");
    // in a dense field nearly everything descends from early work, so tracing stops at two
    // generations each way (what it built on, and what built on that; likewise forward)
    const hops = (start, step, k) => {
      let frontier = start, all = cy.collection();
      for (let i = 0; i < k; i++) { frontier = step(frontier.nodes()); all = all.union(frontier); }
      return all;
    };
    let keep = node.closedNeighborhood();
    if (trace) keep = keep.union(hops(node, (c) => c.incomers(), 2)).union(hops(node, (c) => c.outgoers(), 2));
    cy.elements().not(keep).not(".band").addClass("faded");
    keep.edges().addClass("hl");
    node.addClass("pin");
    const earlier = node.incomers().nodes().length, later = node.outgoers().nodes().length;
    graphPanel(`
      <div class="eyebrow">${esc(regionName[n.region] || "Other")} · ${fmtDate(n.published_date)}</div>
      <b>${esc(n.title)}</b>
      <div class="small muted">${esc(authorsShort(n.authors, 4))}</div>
      <div class="small" style="margin-top:8px">More influential than ${Math.round((n.percentile || 0) * 100)}% of mapped documents:
        cited by ${plural(n.cited_by || 0, "later document")}; ${n.idea_uptake || 0} engage with claims it proposed.</div>
      <div class="small muted" style="margin-top:4px">Directly builds on ${earlier} · directly built on by ${later} mapped documents${trace ? " · showing two generations each way" : ""}</div>
      <div class="panel-actions">
        <a class="btn sm primary" href="#/document/${enc(n.slug)}">Open document</a>
        <a class="btn sm" href="#/reading/${enc(n.slug)}">Reading list</a>
        <button class="btn sm" id="trace">${trace ? "Neighbours only" : "Trace lineage"}</button>
        <button class="btn sm ghost" id="unpin">Close</button>
      </div>`);
    $("#trace").addEventListener("click", () => pin(node, { trace: !trace }));
    $("#unpin").addEventListener("click", unpin);
  };
  cy.on("tap", "node", (e) => { if (e.target.data("n")) { $("#tip").style.display = "none"; pin(e.target); } });
  cy.on("tap", (e) => { if (e.target === cy) unpin(); });

  function highlightRegion() {
    cy.nodes().removeClass("dim");
    if (!lineageState.region || cy.scratch("pinned")) return;
    cy.nodes().filter((x) => x.data("n") && x.data("n").region !== lineageState.region).addClass("dim");
  }
  highlightRegion();

  // fit the documents below the year ruler
  const fitDocs = () => {
    const bb = docs.boundingBox({ includeLabels: false }), left = 40, top = 44, pad = 40;
    const z = Math.min((cy.width() - left - pad) / bb.w, (cy.height() - top - pad) / bb.h, 1.5);
    cy.viewport({ zoom: z, pan: { x: left - bb.x1 * z, y: top - bb.y1 * z } });
  };
  $("#fit").addEventListener("click", fitDocs);
  const target = focus && cy.$id(focus);
  if (target && target.nonempty()) {
    cy.zoom(1.1);
    cy.center(target);
    pin(target);
  } else {
    fitDocs();
  }
  labelForZoom();
  // the container can still be settling its size when the graph is built: keep the initial
  // view fitted until the user pans or zooms themselves
  let userMoved = Boolean(target && target.nonempty());
  cy.on("tapstart", () => { userMoved = true; });
  cy.container().addEventListener("wheel", () => { userMoved = true; }, { passive: true });
  watchSize(cy, () => { if (!userMoved) fitDocs(); });
}

/* ================================================================ concept map */

let conceptLayout = "map";

/* Distinct, soft colours for regions (Tableau 10); claim health is shown in the labels. */
const REGION_COLORS = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#86bcb6",
                       "#8cd17d", "#d4a6c8", "#a0cbe8", "#ffbe7d"];

function conceptGraph(d) {
  const map = conceptLayout === "map" && d.regions.length > 0;
  $("#concept-body").innerHTML = `
    <div class="card graph-wrap">
      ${map ? `<svg class="map-layer" id="map-layer" aria-hidden="true"></svg>` : ""}
      <div class="graph" id="cy"></div>
      ${map ? `<div class="map-labels" id="map-labels" aria-hidden="true"></div>` : ""}
      <div class="graph-tip" id="tip"></div>
      <div class="graph-panel" id="panel"></div>
      <div class="graph-controls">
        <div class="seg" id="cg-layout"><button data-v="map" class="${conceptLayout === "map" ? "on" : ""}">Map</button><button data-v="tree" class="${conceptLayout === "tree" ? "on" : ""}">Hierarchy</button></div>
        <button class="btn" id="fit">Fit</button>
      </div>
      <div class="graph-legend">
        ${map ? `<div><b>Regions</b> are large areas of the field with little overlap, each shaded in its own colour. Zoom out for the overview; click a region to zoom in.</div>` : ""}
        <div style="margin-top:4px"><b>Dot colour</b>: share of claims replicated or supported</div>
        <div class="row"><span class="small">weak</span><span class="ramp"></span><span class="small">solid</span></div>
        <div class="row"><span style="width:12px;height:12px;border-radius:50%;border:3px solid var(--st-contested)"></span>has fragile foundations</div>
      </div>
    </div>`;
  $$("#cg-layout button").forEach((b) => b.addEventListener("click", () => { conceptLayout = b.dataset.v; conceptGraph(d); }));

  cyInstances.forEach((c) => c.destroy());
  cyInstances = [];
  const text = cssVar("--text"), border = cssVar("--border-strong"), accent = cssVar("--accent");
  const regionInfo = Object.fromEntries(d.regions.map((r, i) => [r.slug, { ...r, color: REGION_COLORS[i % REGION_COLORS.length] }]));
  const elements = [
    // regions are invisible compound nodes: they only hold their concepts together in the layout
    ...(map ? d.regions.map((r) => ({ data: { id: `region:${r.slug}` }, classes: "region" })) : []),
    ...d.nodes.map((n) => ({ data: { id: n.slug, label: n.name, claims: n.claims || 0, color: mixColor(n.solid_share),
      fragile: n.fragile || 0, n, ...(map && n.region ? { parent: `region:${n.region}` } : {}) },
      classes: d.regions.some((r) => r.slug === n.slug) ? "region-root" : "" })),
    ...d.edges.map((e) => ({ data: { id: `${e.parent}->${e.child}`, source: e.parent, target: e.child } })),
  ];
  // force-directed layout takes a few seconds on big maps; reuse it while the structure is unchanged
  const signature = map ? `cmap2:${hashString(JSON.stringify([d.nodes.map((n) => [n.slug, n.region]).sort(), d.edges.map((e) => e.parent + ">" + e.child).sort()]))}` : null;
  let cached = null;
  try { cached = signature && JSON.parse(localStorage.getItem(signature) || "null"); } catch { cached = null; }
  const useCache = cached && d.nodes.every((n) => cached[n.slug]);
  if (useCache) elements.forEach((el) => { if (cached[el.data.id]) el.position = cached[el.data.id]; });
  const cy = cytoscape({
    container: $("#cy"),
    elements,
    style: [
      { selector: "node", style: {
        "background-color": "data(color)", label: "data(label)", color: text, "font-size": 11, "font-family": GRAPH_FONT,
        "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "wrap", "text-max-width": 110, "min-zoomed-font-size": 7,
        "text-outline-width": 2, "text-outline-color": cssVar("--surface"), "text-outline-opacity": 0.75,
        width: "mapData(claims, 0, 40, 12, 44)", height: "mapData(claims, 0, 40, 12, 44)", "border-width": 1, "border-color": border } },
      { selector: "node.region-root", style: { "font-weight": 700, "font-size": 13 } },
      { selector: "node[fragile > 0]", style: { "border-width": 3, "border-color": cssVar("--st-contested") } },
      { selector: ":parent", style: { "background-opacity": 0, "border-width": 0, label: "", padding: 24, events: "no" } },
      { selector: "edge", style: { width: 1, "line-color": border, "target-arrow-color": border, "target-arrow-shape": "triangle",
        "arrow-scale": .6, "curve-style": "bezier", opacity: .5 } },
      { selector: "node.ov-child", style: { opacity: .45, label: "" } },
      { selector: "edge.ov-edge", style: { opacity: .06 } },
      { selector: ".faded", style: { opacity: .12 } },
      { selector: "edge.hl", style: { "line-color": accent, "target-arrow-color": accent, width: 2, opacity: 1 } },
      { selector: "node.pin", style: { "border-width": 3, "border-color": accent } },
    ],
    layout: useCache ? { name: "preset", fit: true, padding: 40 } : map
      ? { name: window.cytoscapeFcose ? "fcose" : "cose", quality: "proof", randomize: true, animate: false,
          nodeRepulsion: () => 9000, idealEdgeLength: () => 80, edgeElasticity: () => 0.3, nestingFactor: 0.12,
          gravity: 0.25, gravityCompound: 1.4, gravityRangeCompound: 1.2, numIter: 3000, tile: true, nodeSeparation: 70,
          packComponents: true, fit: true, padding: 40 }
      : { name: "dagre", rankDir: "TB", nodeSep: 70, rankSep: 110, edgeSep: 12 },
    wheelSensitivity: 0.25, minZoom: 0.05, maxZoom: 2.5,
  });
  cyInstances.push(cy);
  if (map && !useCache) {
    try {
      const positions = {};
      cy.nodes().filter((x) => !x.isParent()).forEach((x) => { positions[x.id()] = x.position(); });
      localStorage.setItem(signature, JSON.stringify(positions));
    } catch { /* storage unavailable or full: just lay out again next time */ }
  }

  const conceptTip = (n) => `${n.region && regionInfo[n.region] ? `<div class="eyebrow">${esc(regionInfo[n.region].name)}</div>` : ""}<b>${esc(n.name)}</b>${esc(n.kind)} · ${plural(n.claims || 0, "claim")} · ${plural(n.documents || 0, "doc")}` +
    (n.solid_share != null ? ` · ${Math.round(n.solid_share * 100)}% solid` : "") +
    (n.fragile ? `<br><span class="tag-flag">${n.fragile} fragile foundation${n.fragile > 1 ? "s" : ""}</span>` : "") +
    (n.description ? `<div class="muted" style="margin-top:4px">${esc(n.description)}</div>` : "");
  hoverTips(cy, conceptTip);
  wireLegend();

  const concepts = cy.nodes().filter((x) => !x.isParent());
  const territory = map ? buildTerritories(cy, concepts, regionInfo) : null;

  // semantic zoom: below this zoom the map shows regions, above it the concepts inside them
  const OVERVIEW_BELOW = 0.55;
  let overview = null;
  const onViewport = () => {
    if (!map) return;
    const z = cy.zoom(), ov = z < OVERVIEW_BELOW;
    if (ov !== overview) {
      overview = ov;
      cy.batch(() => {
        concepts.toggleClass("ov-child", ov);
        cy.edges().toggleClass("ov-edge", ov);
      });
      $("#map-layer").classList.toggle("ov", ov);
    }
    territory.draw(ov);
  };
  cy.on("viewport", scheduler(onViewport));
  onViewport();

  const zoomToRegion = (slug) => {
    const members = concepts.filter((x) => x.data("n").region === slug);
    if (!members.length) return;
    const bb = members.boundingBox({ includeLabels: false });
    const fitZoom = Math.min((cy.width() - 120) / Math.max(bb.w, 1), (cy.height() - 120) / Math.max(bb.h, 1));
    cy.animate({ zoom: Math.min(1.4, Math.max(fitZoom, OVERVIEW_BELOW + 0.15)), center: { eles: members }, duration: 350 });
  };

  const unpin = () => { cy.scratch("pinned", null); cy.elements().removeClass("faded hl pin"); graphPanel(null); };
  cy.on("tap", "node", (e) => {
    const node = e.target;
    const n = node.data("n");
    if (!n) return;
    if (map && overview && n.region) { unpin(); zoomToRegion(n.region); return; }
    $("#tip").style.display = "none";
    cy.scratch("pinned", node.id());
    cy.elements().removeClass("faded hl pin");
    const keep = node.closedNeighborhood();
    cy.elements().not(keep).not(":parent").addClass("faded");
    keep.edges().addClass("hl");
    node.addClass("pin");
    const r = n.region && regionInfo[n.region];
    graphPanel(`
      <div class="eyebrow">${esc(n.kind)}${r ? ` · ${esc(r.name)}` : ""}</div>
      <b>${esc(n.name)}</b>
      ${n.description ? `<div class="small muted" style="margin-top:4px">${esc(n.description)}</div>` : ""}
      <div class="small" style="margin-top:8px">${plural(n.claims || 0, "claim")} · ${plural(n.documents || 0, "document")}${n.solid_share != null ? ` · ${Math.round(n.solid_share * 100)}% solid` : ""}</div>
      ${n.claims ? `<div style="margin-top:8px">${mixbar(n.statuses || {})}</div>` : ""}
      <div class="panel-actions">
        <a class="btn sm primary" href="#/concept/${enc(n.slug)}">Open concept</a>
        <button class="btn sm ghost" id="unpin">Close</button>
      </div>`);
    $("#unpin").addEventListener("click", unpin);
  });
  cy.on("tap", (e) => {
    if (e.target !== cy) return;
    // clicking open ground inside a region zooms into that region
    const slug = map ? territory.regionAt(e.position) : null;
    if (slug && overview) { unpin(); zoomToRegion(slug); } else unpin();
  });
  const fitAll = () => cy.animate({ fit: { eles: concepts, padding: 50 }, duration: 300 });
  $("#fit").addEventListener("click", fitAll);
  if (map) watchSize(cy, () => cy.fit(concepts, 50));

  if (!map && cy.zoom() < 0.75) {
    // hierarchy: start readable near the top of the tree
    cy.zoom(0.75);
    const root = cy.nodes().max((n) => n.data("claims") || 0).ele;
    cy.center(root);
    cy.panBy({ x: 0, y: 90 - root.renderedPosition("y") });
  }
}

/* The map layer: a Voronoi cell around every concept, coloured by its region, so each region
   reads as one shaded territory with borders where territories meet; everything is clipped to
   a "coastline" of circles around the concepts. Geometry is computed once in model coordinates
   and the SVG group is simply transformed to follow the viewport. */
function buildTerritories(cy, concepts, regionInfo) {
  const svg = $("#map-layer"), labels = $("#map-labels");
  const nodes = concepts.toArray();
  const pts = nodes.map((x) => [x.position("x"), x.position("y")]);
  const region = nodes.map((x) => x.data("n").region || null);
  if (!window.d3 || pts.length < 3) return { draw() {}, regionAt: () => null };

  // coastline radius scales with how spread out the concepts are
  const delaunay = d3.Delaunay.from(pts);
  const nn = pts.map((p, i) => {
    let best = Infinity;
    for (const j of delaunay.neighbors(i)) best = Math.min(best, Math.hypot(p[0] - pts[j][0], p[1] - pts[j][1]));
    return best;
  }).filter(Number.isFinite).sort((a, b) => a - b);
  const R = Math.max(70, Math.min(170, 1.6 * (nn[Math.floor(nn.length / 2)] || 100)));
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const vor = delaunay.voronoi([Math.min(...xs) - R, Math.min(...ys) - R, Math.max(...xs) + R, Math.max(...ys) + R]);

  const poly = (i) => vor.cellPolygon(i) || [];
  const pathOf = (cells) => cells.map((i) => { const c = poly(i); return c.length ? `M${c.map((p) => p.join(",")).join("L")}Z` : ""; }).join("");
  const byRegion = {};
  region.forEach((r, i) => (byRegion[r || "_none"] ||= []).push(i));

  // borders: the Voronoi edge shared by two neighbouring cells in different regions
  const key = (p) => `${p[0].toFixed(3)},${p[1].toFixed(3)}`;
  let borders = "";
  for (let i = 0; i < pts.length; i++) {
    const mine = new Set(poly(i).map(key));
    for (const j of delaunay.neighbors(i)) {
      if (j <= i || region[i] === region[j]) continue;
      const shared = poly(j).filter((p) => mine.has(key(p)));
      if (shared.length >= 2) borders += `M${shared[0].join(",")}L${shared[1].join(",")}`;
    }
  }

  // the coastline: circles around each concept, blurred and re-thresholded ("goo") so they
  // melt into smooth landmasses instead of a bubbly outline
  const id = Math.random().toString(36).slice(2, 8);
  svg.innerHTML = `
    <defs>
      <filter id="goo-${id}" x="-20%" y="-20%" width="140%" height="140%" color-interpolation-filters="sRGB">
        <feGaussianBlur stdDeviation="${R * 0.35}"/>
        <feColorMatrix values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 18 -8"/>
      </filter>
      <mask id="coast-${id}" maskUnits="userSpaceOnUse" x="${Math.min(...xs) - 2 * R}" y="${Math.min(...ys) - 2 * R}"
            width="${Math.max(...xs) - Math.min(...xs) + 4 * R}" height="${Math.max(...ys) - Math.min(...ys) + 4 * R}">
        <g filter="url(#goo-${id})">${pts.map((p) => `<circle cx="${p[0]}" cy="${p[1]}" r="${R}" fill="#fff"/>`).join("")}</g>
      </mask>
    </defs>
    <g class="map-view">
      <g mask="url(#coast-${id})">
        <rect class="land" x="${Math.min(...xs) - 2 * R}" y="${Math.min(...ys) - 2 * R}" width="${Math.max(...xs) - Math.min(...xs) + 4 * R}" height="${Math.max(...ys) - Math.min(...ys) + 4 * R}"/>
        ${Object.entries(byRegion).filter(([r]) => r !== "_none").map(([r, cells]) =>
          `<path class="territory${regionInfo[r]?.minor ? " minor" : ""}" d="${pathOf(cells)}" style="--rc:${regionInfo[r]?.color || "#888"}"/>`).join("")}
        <path class="borders" d="${borders}"/>
      </g>
    </g>`;
  const view = $(".map-view", svg);

  // label anchors: centre of each region's concepts (overview) and its top edge (detail)
  const anchors = Object.entries(byRegion).filter(([r]) => r !== "_none" && regionInfo[r]).map(([r, cells]) => {
    const cx = cells.reduce((a, i) => a + pts[i][0], 0) / cells.length;
    const cy0 = cells.reduce((a, i) => a + pts[i][1], 0) / cells.length;
    const top = Math.min(...cells.map((i) => pts[i][1])) - R * 0.55;
    return { slug: r, cx, cy: cy0, top, info: regionInfo[r], n: cells.length };
  });

  return {
    draw(overview) {
      const z = cy.zoom(), pan = cy.pan(), w = cy.width(), h = cy.height();
      view.setAttribute("transform", `translate(${pan.x},${pan.y}) scale(${z})`);
      labels.innerHTML = anchors.map((a) => {
        const x = a.cx * z + pan.x, y = (overview ? a.cy : a.top) * z + pan.y;
        if (x < -150 || x > w + 150 || y < -60 || y > h + 60) return `<div class="region-label" style="display:none"></div>`;
        const r = a.info;
        return overview
          ? `<div class="region-label big" style="left:${x}px;top:${y}px;--rc:${r.color}">
               <b>${esc(r.name)}</b>
               <span>${plural(r.concepts, "concept")}${r.solid_share != null ? ` · ${Math.round(r.solid_share * 100)}% solid` : ""}</span>
               ${r.claims ? mixbar(r.statuses || {}) : ""}
             </div>`
          : `<div class="region-label" style="left:${x}px;top:${y}px;--rc:${r.color}">${esc(r.name)}</div>`;
      }).join("");
      // biggest regions keep their full card; a label that would overlap one already placed
      // shrinks to its name, and is hidden if even that doesn't fit
      const placed = [];
      const overlaps = (b) => placed.some((o) => b.left < o.right + 4 && b.right > o.left - 4 && b.top < o.bottom + 4 && b.bottom > o.top - 4);
      [...labels.children].map((el, i) => ({ el, n: anchors[i] ? anchors[i].n : 0 }))
        .sort((a, b) => b.n - a.n)
        .forEach(({ el }) => {
          let box = el.getBoundingClientRect();
          if (overlaps(box) && el.classList.contains("big")) {
            el.classList.add("compact");
            box = el.getBoundingClientRect();
          }
          if (overlaps(box)) el.style.visibility = "hidden";
          else placed.push(box);
        });
    },
    regionAt(p) {
      const i = delaunay.find(p.x, p.y);
      return Math.hypot(p.x - pts[i][0], p.y - pts[i][1]) <= R ? region[i] : null;
    },
  };
}

/* ================================================================ reading list */

const readingState = { direction: "back", depth: 2, sort: "reading", showStubs: true };
const READ_KEY = "interp-map-read";

function readSet() {
  try { return new Set(JSON.parse(localStorage.getItem(READ_KEY) || "[]")); } catch { return new Set(); }
}
function saveReadSet(set) {
  try { localStorage.setItem(READ_KEY, JSON.stringify([...set])); } catch { /* per-browser convenience only */ }
}

async function renderReading(slug) {
  const st = readingState;
  const r = await api(`/api/documents/${enc(slug)}/lineage?direction=${st.direction}&depth=${st.depth}`);
  app.className = "";
  const back = st.direction === "back";
  const read = readSet();
  // how central a work is to this lineage: influence (or, for works not yet in the map, how
  // often mapped documents cite it), discounted by distance
  const essential = (x) => (x.in_map ? (x.importance || 0) : Math.min(1, (x.cited_in_map || 0) / 8)) / x.generation;
  const sorters = {
    // foundations first: what everything else builds on
    reading: (a, b) => (a.published_date || "9999").localeCompare(b.published_date || "9999"),
    essential: (a, b) => essential(b) - essential(a),
    generation: (a, b) => a.generation - b.generation || essential(b) - essential(a),
  };
  const visible = () => r.items.filter((x) => st.showStubs || x.in_map || !back).sort(sorters[st.sort]);

  app.innerHTML = `
    <div class="eyebrow">Reading list</div>
    <h1 class="detail-title">${back ? "Background for" : "Work building on"} <a href="#/document/${enc(r.document.slug)}">${esc(r.document.title)}</a></h1>
    <p class="muted" style="max-width:72ch">${back
      ? "Everything this document builds on: works it cites and documents whose claims it uses, then what those built on, and so on."
      : "Everything that builds on this document, directly or through later work."}
      Works not yet in the map are included too, and you can queue them for the next update. Ticks are saved in this browser.</p>
    <div class="toolbar" style="margin-top:14px">
      <div class="seg" id="rd-dir"><button data-v="back" class="${back ? "on" : ""}">Background</button><button data-v="forward" class="${back ? "" : "on"}">Built on it</button></div>
      <select id="rd-depth" aria-label="Generations">${[1, 2, 3, 4, 5].map((n) => `<option value="${n}" ${n === st.depth ? "selected" : ""}>${n === 1 ? "Direct only" : `${n} generations`}</option>`).join("")}</select>
      <select id="rd-sort" aria-label="Order">
        ${[["reading", "Reading order (oldest first)"], ["essential", "Most essential first"], ["generation", "By generation"]].map(([v, l]) =>
          `<option value="${v}" ${st.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      ${back ? `<label class="filter-chip ${st.showStubs ? "on" : ""}"><input type="checkbox" id="rd-stubs" ${st.showStubs ? "checked" : ""} hidden>Include works not yet in the map</label>` : ""}
      <span class="rd-actions">
        <button class="btn sm" id="rd-copy">Copy as Markdown</button>
        <a class="btn sm" href="#/lineage?focus=${enc(r.document.slug)}">Show in lineage</a>
      </span>
    </div>
    <div class="card" id="rd-list"></div>`;

  const draw = () => {
    const items = visible();
    const done = items.filter((x) => read.has(x.slug)).length;
    const groups = st.sort === "generation"
      ? [...new Set(items.map((x) => x.generation))].map((g) => [g === 1 ? (back ? "Directly built on" : "Builds on it directly") : `${g} steps away`, items.filter((x) => x.generation === g)])
      : [[null, items]];
    $("#rd-list").innerHTML = items.length ? `
      <h2>${plural(items.length, "work")} <span class="hint">${done} of ${items.length} read${r.truncated ? " · list truncated at 600" : ""}</span></h2>
      <div class="progress"><span style="width:${(done / items.length) * 100}%"></span></div>
      ${groups.map(([title, list]) => `${title ? `<h3 class="rd-group">${esc(title)} <span class="faint">${list.length}</span></h3>` : ""}
        <ul class="reading">${list.map((x) => `
          <li class="${read.has(x.slug) ? "is-read" : ""}">
            <input type="checkbox" data-read="${esc(x.slug)}" ${read.has(x.slug) ? "checked" : ""} aria-label="Mark as read">
            <div class="rd-main">
              <div class="rd-title">${x.in_map
                ? `<a href="#/document/${enc(x.slug)}">${esc(x.title)}</a>`
                : `${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)} ↗</a>` : esc(x.title)} <span class="chip">not in map</span>`}</div>
              <div class="small muted">${[year(x.published_date), authorsShort(x.authors, 3), x.in_map ? SOURCE_LABEL[x.source] : null].filter(Boolean).map(esc).join(" · ")}</div>
              <div class="small faint rd-why">${x.generation > 1 && x.via ? `via <a href="#/document/${enc(x.via.slug)}">${esc(x.via.title.length > 60 ? x.via.title.slice(0, 58) + "…" : x.via.title)}</a> · ` : ""}${esc(x.why.length > 140 ? x.why.slice(0, 138) + "…" : x.why)}</div>
            </div>
            <div class="rd-side">
              <span class="gen" title="Steps away">${x.generation === 1 ? "direct" : `${x.generation} steps`}</span>
              ${x.in_map ? `<span class="imp" title="Influence: more than ${Math.round((x.importance || 0) * 100)}% of mapped documents"><i style="width:${Math.round((x.importance || 0) * 100)}%"></i></span>`
                : `<span class="small faint" title="Mapped documents citing it">cited ×${x.cited_in_map}</span>
                   ${back ? `<button class="btn sm" data-queue="${esc(x.slug)}">Queue</button>` : ""}`}
            </div>
          </li>`).join("")}</ul>`).join("")}`
      : `<div class="empty">${back ? "No known background yet: nothing this document cites or uses is recorded." : "Nothing in the map builds on this yet."}</div>`;
  };
  draw();

  $$("#rd-dir button").forEach((b) => b.addEventListener("click", () => { st.direction = b.dataset.v; renderReading(slug); }));
  $("#rd-depth").addEventListener("change", (e) => { st.depth = +e.target.value; renderReading(slug); });
  $("#rd-sort").addEventListener("change", (e) => { st.sort = e.target.value; draw(); });
  const stubs = $("#rd-stubs");
  if (stubs) stubs.addEventListener("change", (e) => { st.showStubs = e.target.checked; e.target.parentElement.classList.toggle("on", st.showStubs); draw(); });
  $("#rd-list").addEventListener("change", (e) => {
    const box = e.target.closest("[data-read]");
    if (!box) return;
    if (box.checked) read.add(box.dataset.read); else read.delete(box.dataset.read);
    saveReadSet(read);
    draw();
  });
  $("#rd-list").addEventListener("click", async (e) => {
    const q = e.target.closest("[data-queue]");
    if (!q) return;
    try {
      await api(`/api/documents/${enc(q.dataset.queue)}/queue`, { method: "POST" });
      q.textContent = "Queued";
      q.disabled = true;
      toast("Queued for the next update");
    } catch (err) { toast(err.message); }
  });
  $("#rd-copy").addEventListener("click", async () => {
    const md = [`# ${back ? "Background for" : "Work building on"}: ${r.document.title}`, "",
      ...visible().map((x) => `- [${read.has(x.slug) ? "x" : " "}] ${x.url ? `[${x.title}](${x.url})` : x.title}` +
        ` (${[year(x.published_date), authorsShort(x.authors, 3)].filter(Boolean).join(", ")})` +
        `${x.generation > 1 ? ` · ${x.generation} steps away` : ""}`)].join("\n");
    try { await navigator.clipboard.writeText(md); toast("Copied as Markdown"); }
    catch { toast("Clipboard unavailable in this browser"); }
  });
}
