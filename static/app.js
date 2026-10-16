// Cover Art Downloader — front-end logic (vanilla JS, no frameworks).
(function () {
  "use strict";

  const PAGE_SIZE = 24;
  const $ = (id) => document.getElementById(id);

  const els = {
    form: $("search-form"),
    album: $("album"),
    artist: $("artist"),
    searchBtn: $("search-btn"),
    status: $("status"),
    resultsSection: $("results-section"),
    results: $("results"),
    resultsCount: $("results-count"),
    prev: $("prev-btn"),
    next: $("next-btn"),
    pageInfo: $("page-info"),
    artSection: $("art-section"),
    back: $("back-btn"),
    releaseInfo: $("release-info"),
    typeFilter: $("type-filter"),
    artGrid: $("art-grid"),
  };

  const state = { album: "", artist: "", offset: 0, count: 0, images: [], release: null, filter: "All" };

  // ---------- helpers ----------
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function setStatus(msg, { loading = false, error = false } = {}) {
    els.status.className = "status" + (error ? " error" : "");
    els.status.replaceChildren();
    if (loading) els.status.append(el("span", { class: "spinner" }));
    if (msg) els.status.append(msg);
  }

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    let data;
    try { data = await res.json(); } catch (_) { data = {}; }
    if (!res.ok) throw new Error(data.error || `Request failed (HTTP ${res.status})`);
    return data;
  }

  function placeholderThumb() {
    return el("span", { "aria-hidden": "true" }, "♪");
  }

  // ---------- search ----------
  async function search(offset) {
    state.offset = offset;
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
    if (state.album) params.set("album", state.album);
    if (state.artist) params.set("artist", state.artist);

    els.searchBtn.disabled = true;
    showView("results");
    setStatus("Searching MusicBrainz…", { loading: true });
    try {
      const data = await getJSON(`/api/search?${params}`);
      state.count = data.count;
      renderResults(data.releases);
      setStatus("");
    } catch (err) {
      els.resultsSection.hidden = true;
      setStatus(err.message, { error: true });
    } finally {
      els.searchBtn.disabled = false;
    }
  }

  // Aborts outstanding art-type lookups when the results list is replaced.
  let artTypesController = null;

  async function loadArtTypes(mbid, item, row) {
    const signal = artTypesController.signal;
    try {
      const res = await fetch(`/api/release/${encodeURIComponent(mbid)}/art-types`, {
        headers: { Accept: "application/json" },
        signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Lookup failed");

      row.classList.remove("loading");
      if (!data.has_art) {
        item.classList.add("no-art");
        row.replaceChildren(el("span", { class: "art-none" }, "No artwork available"));
        return;
      }
      row.replaceChildren(
        el("span", { class: "art-label" }, `${data.image_count} image${data.image_count === 1 ? "" : "s"}:`),
        ...data.types.map((t) =>
          el("span", { class: "art-tag", title: `${t.count} ${t.type} image${t.count === 1 ? "" : "s"}` },
            t.type, t.count > 1 ? el("span", { class: "art-count" }, `×${t.count}`) : null)
        )
      );
    } catch (err) {
      if (err.name === "AbortError") return;
      row.classList.remove("loading");
      row.replaceChildren(el("span", { class: "art-none" }, "Couldn't check artwork"));
    }
  }

  function renderResults(releases) {
    if (artTypesController) artTypesController.abort();
    artTypesController = new AbortController();
    els.results.replaceChildren();
    els.resultsSection.hidden = false;
    els.resultsCount.textContent = `(${state.count.toLocaleString()} found)`;

    if (!releases.length) {
      els.results.append(el("li", { class: "empty" }, "No releases matched your search."));
    }

    for (const r of releases) {
      const thumb = el("div", { class: "thumb" });
      const img = el("img", { src: r.thumbnail, alt: "", loading: "lazy" });
      img.addEventListener("error", () => thumb.replaceChildren(placeholderThumb()));
      thumb.append(img);

      const tags = [
        r.primary_type,
        r.date,
        r.country,
        ...(r.formats || []),
        r.track_count ? `${r.track_count} tracks` : null,
        ...(r.labels || []).slice(0, 2),
        r.status && r.status !== "Official" ? r.status : null,
      ].filter(Boolean);

      const artRow = el("div", { class: "art-types loading" },
        el("span", { class: "spinner small" }), "Checking artwork…");

      const item = el(
        "li",
        {
          class: "result",
          tabindex: "0",
          role: "button",
          onclick: () => openRelease(r.id),
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openRelease(r.id); } },
        },
        thumb,
        el(
          "div",
          { class: "result-body" },
          el("p", { class: "result-title" }, r.title,
            r.disambiguation ? el("span", { class: "muted" }, ` (${r.disambiguation})`) : null),
          el("p", { class: "result-artist muted" }, r.artist),
          el("div", { class: "meta" }, tags.map((t) => el("span", { class: "tag" }, t))),
          artRow
        ),
        r.score != null ? el("span", { class: "score", title: "Search relevance" }, `${r.score}%`) : null
      );
      els.results.append(item);
      loadArtTypes(r.id, item, artRow);
    }

    const page = Math.floor(state.offset / PAGE_SIZE) + 1;
    const pages = Math.max(1, Math.ceil(state.count / PAGE_SIZE));
    els.pageInfo.textContent = `Page ${page} of ${pages.toLocaleString()}`;
    els.prev.disabled = state.offset === 0;
    els.next.disabled = state.offset + PAGE_SIZE >= state.count;
  }

  // ---------- release / artwork ----------
  async function openRelease(mbid) {
    showView("art");
    els.releaseInfo.replaceChildren();
    els.typeFilter.replaceChildren();
    els.artGrid.replaceChildren();
    setStatus("Loading artwork from the Cover Art Archive…", { loading: true });
    window.scrollTo({ top: 0, behavior: "smooth" });

    try {
      const data = await getJSON(`/api/release/${encodeURIComponent(mbid)}`);
      state.release = data.release;
      state.images = data.images;
      state.filter = "All";
      renderRelease();
      setStatus("");
    } catch (err) {
      setStatus(err.message, { error: true });
    }
  }

  function renderRelease() {
    const r = state.release;
    const sub = [r.date, r.country, ...(r.formats || [])].filter(Boolean).join(" · ");
    els.releaseInfo.replaceChildren(
      el("h2", {}, r.title || r.id),
      r.artist ? el("p", {}, r.artist) : null,
      sub ? el("p", { class: "muted" }, sub) : null,
      el("p", { class: "mb-link" },
        el("a", { href: `https://musicbrainz.org/release/${r.id}`, target: "_blank", rel: "noopener" },
          "View on MusicBrainz ↗"))
    );

    if (!state.images.length) {
      els.typeFilter.replaceChildren();
      els.artGrid.replaceChildren(
        el("div", { class: "empty", style: "grid-column: 1 / -1" },
          "The Cover Art Archive has no artwork for this release.")
      );
      return;
    }

    // Build type filter chips with counts.
    const counts = { All: state.images.length };
    for (const img of state.images) for (const t of img.types) counts[t] = (counts[t] || 0) + 1;
    els.typeFilter.replaceChildren(
      ...Object.entries(counts).map(([type, n]) =>
        el("button", {
          type: "button",
          class: "chip" + (state.filter === type ? " active" : ""),
          onclick: () => { state.filter = type; renderRelease(); },
        }, `${type} (${n})`)
      )
    );

    const visible = state.filter === "All"
      ? state.images
      : state.images.filter((i) => i.types.includes(state.filter));
    els.artGrid.replaceChildren(...visible.map(renderArtCard));
  }

  function renderArtCard(img) {
    const r = state.release;
    const typeLabel = img.types.join("+");
    const baseName = [r.artist, r.title, typeLabel, img.id].filter(Boolean).join(" - ");

    const downloads = img.sizes.map((s) => {
      const params = new URLSearchParams({ url: s.url, name: `${baseName} - ${s.label}` });
      return el("a", {
        class: "btn" + (s.key === "original" ? "" : " alt"),
        href: `/api/download?${params}`,
        title: `Download ${s.label}`,
      }, s.label);
    });

    return el(
      "article",
      { class: "art-card" },
      el("a", { class: "preview", href: img.sizes.length ? img.sizes[img.sizes.length - 1].url : "#", target: "_blank", rel: "noopener", title: "Open original in new tab" },
        el("img", { src: img.preview, alt: `${typeLabel} artwork`, loading: "lazy" })),
      el(
        "div",
        { class: "info" },
        el("div", { class: "types" }, img.types.map((t) => el("span", { class: "tag" }, t))),
        img.comment ? el("p", { class: "comment" }, img.comment) : null,
        el("span", { class: "downloads-label" }, "Download"),
        el("div", { class: "downloads" }, downloads)
      )
    );
  }

  // ---------- view switching ----------
  function showView(view) {
    els.artSection.hidden = view !== "art";
    els.resultsSection.hidden = view !== "results" || !els.results.children.length;
  }

  // ---------- events ----------
  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    state.album = els.album.value.trim();
    state.artist = els.artist.value.trim();
    if (!state.album && !state.artist) {
      setStatus("Enter an album and/or artist to search.", { error: true });
      return;
    }
    els.results.replaceChildren();
    search(0);
  });

  els.prev.addEventListener("click", () => search(Math.max(0, state.offset - PAGE_SIZE)));
  els.next.addEventListener("click", () => search(state.offset + PAGE_SIZE));
  els.back.addEventListener("click", () => { setStatus(""); showView("results"); });
})();
