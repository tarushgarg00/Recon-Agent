const state = {
  map: null,
  draftMarker: null,
  draftLngLat: null,
  sites: [],
  history: [],
  modeHint: "auto",
  workingTimer: null,
  workingId: null,
};

const els = {
  addressInput: document.querySelector("#addressInput"),
  geocodeBtn: document.querySelector("#geocodeBtn"),
  mapStatus: document.querySelector("#mapStatus"),
  confirmPinBtn: document.querySelector("#confirmPinBtn"),
  clearPinsBtn: document.querySelector("#clearPinsBtn"),
  siteList: document.querySelector("#siteList"),
  messages: document.querySelector("#messages"),
  resultPanel: document.querySelector("#resultPanel"),
  resultSection: document.querySelector("#resultSection"),
  chatForm: document.querySelector("#chatForm"),
  chatInput: document.querySelector("#chatInput"),
  sendBtn: document.querySelector("#sendBtn"),
  modeBtns: document.querySelectorAll(".mode-btn"),
  traceList: document.querySelector("#traceList"),
  loopCount: document.querySelector("#loopCount"),
  historyCount: document.querySelector("#historyCount"),
};

function text(value) {
  return value === null || value === undefined ? "" : String(value);
}

function escapeHtml(value) {
  return text(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function cleanText(value) {
  const raw = text(value).trim();
  if (!raw) return "";
  if (/^\s*[\[{]/.test(raw) || /"\w+"\s*:/.test(raw)) {
    return "Recon has a structured result ready below.";
  }
  return raw
    .replaceAll("**", "")
    .replaceAll("##", "")
    .replaceAll("`", "")
    .replace(/^\s*[-*]\s+/gm, "")
    .replace(/"?[A-Za-z_][\w]*"?\s*:\s*("[^"]*"|false|true|null|[-\d.]+)/g, "")
    .replace(/\b(get_site_data|search_zoning|score_site|compare_sites|save_site_brief|recall_site_briefs)\b/g, "source data")
    .replace(/\b(in_fema_flood_zone|nearest_substation_distance_mi|elevation_m|grid_proximity|regional_power)\b/g, "site signal")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function setStatus(message) {
  els.mapStatus.textContent = message;
}

async function loadConfig() {
  const res = await fetch("/config");
  return res.json();
}

function initMap(token) {
  if (!token) {
    setStatus("MAPBOX_TOKEN is missing. Add it to .env to enable address search and the map.");
    return;
  }

  mapboxgl.accessToken = token;
  state.map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/light-v11",
    center: [-96.8, 37.8],
    zoom: 3,
  });

  state.map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");
  setStatus("Search an address, drag the pin if needed, then confirm it.");
}

async function geocodeAddress() {
  const query = els.addressInput.value.trim();
  if (!query || !mapboxgl.accessToken) return;

  setStatus("Finding address...");
  const url = `https://api.mapbox.com/geocoding/v5/mapbox.places/${encodeURIComponent(query)}.json?limit=1&access_token=${encodeURIComponent(mapboxgl.accessToken)}`;
  const res = await fetch(url);
  const data = await res.json();
  const feature = data.features && data.features[0];

  if (!feature) {
    setStatus("No address match found.");
    return;
  }

  const [lon, lat] = feature.center;
  state.draftLngLat = { lat, lon, label: feature.place_name };

  if (state.draftMarker) state.draftMarker.remove();

  state.draftMarker = new mapboxgl.Marker({ draggable: true, color: "#141414" })
    .setLngLat([lon, lat])
    .addTo(state.map);

  state.draftMarker.on("dragend", () => {
    const lngLat = state.draftMarker.getLngLat();
    state.draftLngLat = { lat: lngLat.lat, lon: lngLat.lng, label: feature.place_name };
    setStatus(`Draft pin: ${lngLat.lat.toFixed(5)}, ${lngLat.lng.toFixed(5)}`);
  });

  state.map.flyTo({ center: [lon, lat], zoom: 11 });
  setStatus(`Draft pin: ${lat.toFixed(5)}, ${lon.toFixed(5)}`);
}

function confirmPin() {
  if (!state.draftLngLat) {
    setStatus("Search an address before confirming a pin.");
    return;
  }

  state.sites.push({
    lat: Number(state.draftLngLat.lat.toFixed(6)),
    lon: Number(state.draftLngLat.lon.toFixed(6)),
    label: state.draftLngLat.label || null,
  });
  state.draftLngLat = null;

  if (state.draftMarker) {
    state.draftMarker.setDraggable(false);
    state.draftMarker = null;
  }

  renderSites();
  setStatus(`${state.sites.length} confirmed site${state.sites.length === 1 ? "" : "s"} ready for chat.`);
}

function clearPins() {
  state.sites = [];
  if (state.draftMarker) state.draftMarker.remove();
  state.draftMarker = null;
  state.draftLngLat = null;
  renderSites();
  setStatus("Pins cleared.");
}

function siteLabel(site, index) {
  return cleanText(site.label || `Site ${index + 1}`) || `Site ${index + 1}`;
}

function shortCoords(site) {
  return `${Number(site.lat).toFixed(4)}, ${Number(site.lon).toFixed(4)}`;
}

function removeSite(index) {
  if (index < 0 || index >= state.sites.length) return;
  state.sites.splice(index, 1);
  renderSites();
  setStatus(`${state.sites.length} confirmed site${state.sites.length === 1 ? "" : "s"} ready for chat.`);
}

function renderSites() {
  els.siteList.innerHTML = state.sites
    .map(
      (site, index) => `
        <li>
          <span class="site-main">
            <strong>${escapeHtml(siteLabel(site, index))}</strong>
            <span>${escapeHtml(shortCoords(site))}</span>
          </span>
          <button class="site-remove" type="button" data-index="${index}" aria-label="Remove ${escapeHtml(siteLabel(site, index))}">x</button>
        </li>
      `
    )
    .join("");
}

function renderEmptyStates() {
  els.messages.innerHTML = `<div class="empty">Confirm a site on the map, then ask a question or pick a shortcut below.</div>`;
  els.traceList.innerHTML = `<li class="trace-empty">The agent's steps appear here as it works.</li>`;
}

function clearChatEmpty() {
  const empty = els.messages.querySelector(".empty");
  if (empty) empty.remove();
}

function addMessage(role, content) {
  clearChatEmpty();
  const row = document.createElement("div");
  row.className = `message ${role}`;
  row.innerHTML = `<div class="bubble">${escapeHtml(cleanText(content))}</div>`;
  els.messages.appendChild(row);
  els.messages.scrollTop = els.messages.scrollHeight;
}

function startWorking() {
  clearChatEmpty();
  const id = `working-${Date.now()}`;
  state.workingId = id;
  const row = document.createElement("div");
  row.className = "message assistant working";
  row.id = id;
  row.innerHTML = `<div class="bubble"><span class="working-text">Thinking</span><span class="dots"><span></span><span></span><span></span></span></div>`;
  els.messages.appendChild(row);
  els.messages.scrollTop = els.messages.scrollHeight;

  const statuses = ["Thinking", "Pulling flood, grid, and elevation data", "Searching the zoning ordinance", "Scoring the site"];
  let index = 0;
  state.workingTimer = setInterval(() => {
    const label = row.querySelector(".working-text");
    if (label) label.textContent = statuses[index % statuses.length];
    index += 1;
  }, 1800);
}

function updateWorking(message) {
  const row = document.getElementById(state.workingId);
  const label = row && row.querySelector(".working-text");
  if (label) label.textContent = message;
}

function stopWorking() {
  if (state.workingTimer) clearInterval(state.workingTimer);
  state.workingTimer = null;
  const row = document.getElementById(state.workingId);
  if (row) row.remove();
  state.workingId = null;
}

function verdictMeaning(verdict) {
  const key = text(verdict).toUpperCase();
  if (key === "GO") return "Strong initial fit.";
  if (key === "NO-GO") return "Do not advance without a major change.";
  return "Worth a look, with open questions.";
}

function sourceTag(reason) {
  const content = `${reason.point || ""} ${reason.grounded_in || ""}`.toLowerCase();
  if (content.includes("fema") || content.includes("flood")) return "FEMA flood maps";
  if (content.includes("hifld") || content.includes("substation") || content.includes("grid")) return "HIFLD grid data";
  if (content.includes("usgs") || content.includes("elevation")) return "USGS elevation";
  if (content.includes("zoning") || content.includes("ordinance") || content.includes("chunk")) return "Zoning ordinance";
  if (content.includes("eia") || content.includes("regional")) return "EIA regional data";
  if (content.includes("deterministic site screen") || content.includes("scored by")) return "Scored by deterministic site screen";
  return "Source data";
}

function findingIcon(reason) {
  const source = sourceTag(reason);
  if (source.includes("FEMA")) return { label: "FL", kind: "flood" };
  if (source.includes("HIFLD")) return { label: "KV", kind: "grid" };
  if (source.includes("USGS")) return { label: "EL", kind: "elevation" };
  if (source.includes("Zoning")) return { label: "ZO", kind: "zoning" };
  return { label: "DA", kind: "data" };
}

function plainFinding(reason) {
  const combined = `${reason.point || ""} ${reason.grounded_in || ""}`.toLowerCase();
  if (combined.includes("in_fema_flood_zone") && combined.includes("false")) {
    return "Not in a FEMA flood zone based on the mapped flood hazard data.";
  }
  if (combined.includes("in_fema_flood_zone") && combined.includes("true")) {
    return "The site appears to overlap a mapped FEMA flood zone.";
  }
  if (combined.includes("no usable") || combined.includes("no major substation") || combined.includes("substation")) {
    return combined.includes("no usable")
      ? "No major substation was found nearby, so grid access needs utility confirmation."
      : cleanText(reason.point);
  }
  if (combined.includes("elevation_m") || combined.includes("elevation")) {
    return cleanText(reason.point).replace(/elevation_m/gi, "elevation");
  }
  if (combined.includes("chunk") || combined.includes("zoning")) {
    return cleanText(reason.point).replace(/\[chunk \d+\]/gi, "the ordinance");
  }
  const point = cleanText(reason.point);
  if (point && point !== "Recon has a structured result ready below.") return point;
  return "Recon found a relevant site signal that should be reviewed.";
}

function shortSummary(value) {
  const cleaned = cleanText(value);
  const parts = cleaned.match(/[^.!?]+[.!?]+/g);
  if (!parts) return cleaned;
  return parts.slice(0, 2).join(" ").trim();
}

function scoreWidth(score) {
  const value = Math.max(0, Math.min(100, Number(score) || 0));
  return `${value}%`;
}

function rankWhy(rankings) {
  if (rankings.length < 2) return "Recon ranked the confirmed sites by deterministic screening score.";
  const first = cleanText(rankings[0].label) || "The first site";
  const second = cleanText(rankings[1].label) || "the second site";
  return `${first} ranks first because its deterministic score is higher than ${second}, mainly reflecting stronger screened site signals.`;
}

function rankEvidence(row) {
  const reason = cleanText(row.reason);
  if (!reason || reason === "Recon has a structured result ready below.") {
    return [{ point: "Scored by deterministic site screen.", grounded_in: "Scored by deterministic site screen" }];
  }
  // Split only existing reason text into auditable rows; this avoids fabricating missing site signals.
  const parts = reason.split(/[.;]\s+/).map((part) => part.trim()).filter(Boolean);
  return parts.map((part) => ({ point: part, grounded_in: sourceTag({ point: part, grounded_in: "" }) }));
}

function renderResult(result) {
  if (!result) {
    els.resultPanel.innerHTML = `<div class="result-empty">Your site evaluation will appear here.</div>`;
    return;
  }

  const rankings = result.rankings || [];
  const isCompare = rankings.length > 0;
  const verdict = result.verdict || (isCompare ? "COMPARE" : result.kind || "RESULT");
  const pillClass = text(verdict).toLowerCase().replaceAll(" ", "-");
  const summary = shortSummary(result.reply || "Recon completed the evaluation.");
  const reasons = result.reasons || [];

  const findingRows = reasons
    .map((reason) => {
      const icon = findingIcon(reason);
      return `
        <div class="reason finding">
          <span class="finding-icon ${escapeHtml(icon.kind)}">${escapeHtml(icon.label)}</span>
          <div>
            <strong>${escapeHtml(plainFinding(reason))}</strong>
            <div class="grounded">${escapeHtml(sourceTag(reason))}</div>
          </div>
        </div>
      `;
    })
    .join("");

  const rankRows = rankings
    .map(
      (row, index) => `
        <details class="rank-row">
          <summary>
            <strong class="rank-number">#${index + 1}</strong>
            <div class="rank-main">
              <strong>${escapeHtml(cleanText(row.label))}</strong>
              <div class="grounded">${escapeHtml(cleanText(row.reason))}</div>
            </div>
            <div class="rank-score-cell">
              <div class="rank-score">${escapeHtml(row.score)} / 100</div>
              <div class="score-bar"><span style="width:${scoreWidth(row.score)}"></span></div>
            </div>
          </summary>
          <div class="rank-evidence">
            ${rankEvidence(row)
              .map((reason) => {
                const icon = findingIcon(reason);
                return `
                  <div class="evidence-row">
                    <span class="finding-icon ${escapeHtml(icon.kind)}">${escapeHtml(icon.label)}</span>
                    <div>
                      <strong>${escapeHtml(plainFinding(reason))}</strong>
                      <div class="grounded">${escapeHtml(sourceTag(reason))}</div>
                    </div>
                  </div>
                `;
              })
              .join("")}
          </div>
        </details>
      `
    )
    .join("");

  els.resultPanel.innerHTML = `
    <article class="result-card">
      <div class="result-head">
        <div class="verdict-wrap">
          <span class="pill ${pillClass}">${escapeHtml(verdict)}</span>
          <span class="verdict-meaning">${escapeHtml(isCompare ? "Best sites ranked first." : verdictMeaning(verdict))}</span>
        </div>
        ${result.score !== null && result.score !== undefined ? `<div class="score"><span class="score-line">${escapeHtml(result.score)} <span class="score-denom">/ 100</span></span><small>Site screening score</small><div class="score-bar"><span style="width:${scoreWidth(result.score)}"></span></div></div>` : ""}
      </div>
      <div class="summary">${escapeHtml(summary)}</div>
      ${isCompare ? `<div class="rank-grid"><div class="why-order"><strong>Why this order:</strong> ${escapeHtml(rankWhy(rankings))}</div>${rankRows}</div>` : `<div><p class="section-title">Key findings</p><div class="findings-grid">${findingRows}</div></div>`}
      ${result.needs_human_review ? `<div class="human-review"><strong>What to verify:</strong> ${escapeHtml(cleanText(result.needs_human_review))}</div>` : ""}
    </article>
  `;
}

function traceLabel(event) {
  if (event.type === "model_call") return "Model decided next step";
  if (event.type === "final") return "Prepared final result";
  const labels = {
    get_site_data: "Pulled flood, grid, elevation, and power context",
    search_zoning: "Searched zoning ordinance",
    score_site: "Scored site",
    compare_sites: "Ranked sites",
    save_site_brief: "Saved site to memory",
    recall_site_briefs: "Checked saved sites",
  };
  return labels[event.tool] || "Ran tool";
}

function statusFor(event) {
  if (event.tool === "get_site_data") return "Pulling flood, grid, and elevation data";
  if (event.tool === "search_zoning") return "Searching the zoning ordinance";
  if (event.tool === "score_site") return "Scoring the site";
  if (event.tool === "compare_sites") return "Ranking sites";
  if (event.type === "final") return "Preparing the result";
  return "Thinking";
}

function resetTrace() {
  els.traceList.innerHTML = "";
  els.loopCount.textContent = "0";
}

function appendTrace(event) {
  const empty = els.traceList.querySelector(".trace-empty");
  if (empty) empty.remove();
  els.loopCount.textContent = event.loop || els.loopCount.textContent;
  const item = document.createElement("li");
  item.className = "trace-item";
  const hasDetails = event.tool || Object.keys(event.args || {}).length > 0;
  item.innerHTML = `
    <div class="trace-line">
      <span class="trace-type">Loop ${escapeHtml(event.loop)} - ${escapeHtml(event.type)}</span>
      <span>${escapeHtml(event.duration_ms)}ms</span>
    </div>
    <div class="trace-tool">${escapeHtml(traceLabel(event))}</div>
    <div class="trace-preview">${escapeHtml(`${traceLabel(event)} - ${event.duration_ms}ms`)}</div>
    ${hasDetails ? `<details>
      <summary>details</summary>
      <div class="trace-args">${escapeHtml(JSON.stringify({ tool: event.tool, args: event.args || {} }))}</div>
    </details>` : ""}
  `;
  els.traceList.appendChild(item);
  els.traceList.scrollTop = els.traceList.scrollHeight;
  updateWorking(statusFor(event));
}

function setMode(mode) {
  state.modeHint = state.modeHint === mode ? "auto" : mode;
  els.modeBtns.forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === state.modeHint));
}

async function readStream(res) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      if (event.type === "trace") {
        appendTrace(event.event);
      } else if (event.type === "done" || event.type === "error") {
        finishTurn(event.data);
      }
    }
  }
}

function finishTurn(data) {
  stopWorking();
  const reply = shortSummary(data.reply || "Recon finished.");
  const chatReply = data.result ? `${reply} Full result below.` : reply;
  addMessage("assistant", chatReply);
  state.history.push({ role: "assistant", content: reply });
  renderResult(data.result);
  if (data.result) {
    els.resultSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  els.loopCount.textContent = data.loops || els.loopCount.textContent;
  els.historyCount.textContent = data.history_count || 0;
}

async function sendChat(event) {
  event.preventDefault();
  const raw = els.chatInput.value.trim();
  if (!raw) return;

  addMessage("user", raw);
  state.history.push({ role: "user", content: raw });
  els.chatInput.value = "";
  els.sendBtn.disabled = true;
  resetTrace();
  startWorking();

  try {
    const res = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: raw,
        sites: state.sites,
        mode_hint: state.modeHint,
        history: state.history.slice(-10),
      }),
    });
    await readStream(res);
  } catch (err) {
    stopWorking();
    addMessage("assistant", `The local app could not reach the API: ${err}`);
  } finally {
    els.sendBtn.disabled = false;
  }
}

function bindEvents() {
  els.geocodeBtn.addEventListener("click", geocodeAddress);
  els.addressInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      geocodeAddress();
    }
  });
  els.confirmPinBtn.addEventListener("click", confirmPin);
  els.clearPinsBtn.addEventListener("click", clearPins);
  els.siteList.addEventListener("click", (event) => {
    const button = event.target.closest(".site-remove");
    if (!button) return;
    removeSite(Number(button.dataset.index));
  });
  els.chatForm.addEventListener("submit", sendChat);
  els.modeBtns.forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.mode)));
}

async function main() {
  bindEvents();
  renderEmptyStates();
  const config = await loadConfig();
  initMap(config.mapbox_token);
}

main();
