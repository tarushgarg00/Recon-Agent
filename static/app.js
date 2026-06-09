const state = {
  map: null,
  draftMarker: null,
  draftLngLat: null,
  sites: [],
  history: [],
  modeHint: "auto",
  activeMode: "auto",
  workingTimer: null,
  workingId: null,
};

const MODE_PROMPTS = {
  brief: "Run a full diligence evaluation of this site for a 100MW data center, covering flood, grid, elevation, power, and zoning.",
  screen: "Give me a fast go / no-go screen of this site for a data center.",
  compare: "Compare my confirmed sites and rank them for a 100MW data center, explaining the order.",
  diligence: "Do a risk deep dive on this site: identify the biggest risks and what must be verified.",
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
  shortcutHelp: document.querySelector(".shortcut-help"),
  traceList: document.querySelector("#traceList"),
  loopCount: document.querySelector("#loopCount"),
  eventCount: document.querySelector("#eventCount"),
  artifactKicker: document.querySelector("#artifactKicker"),
  artifactTitle: document.querySelector("#artifactTitle"),
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
    .replace(/\b(get_site_data|search_zoning|score_site|compare_sites|save_site_brief|recall_site_briefs)\b/g, "source evidence")
    .replace(/\b(in_fema_flood_zone|nearest_substation_distance_mi|elevation_m|grid_proximity|regional_power)\b/g, "site signal")
    .replace(/\bsubstantiation\b/gi, "substation")
    .replace(/\b(\d+)\.\s+(\d+)\b/g, "$1.$2")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function displayText(value) {
  return cleanText(value)
    .replace(/\b(\d+)\.\s+(\d+)\b/g, "$1.$2")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function displayScore(score) {
  const value = Number(displayText(score));
  return Number.isFinite(value) ? value.toFixed(1) : displayText(score);
}

function fmtNumber(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return displayText(value);
  return number.toFixed(digits).replace(/\.?0+$/, "");
}

function sentenceSummary(value, limit = 2) {
  const cleaned = displayText(value);
  const sentences = [];
  let start = 0;
  for (let i = 0; i < cleaned.length; i += 1) {
    const char = cleaned[i];
    const prev = cleaned[i - 1] || "";
    const next = cleaned[i + 1] || "";
    if (".!?".includes(char) && !(prev >= "0" && prev <= "9" && next >= "0" && next <= "9")) {
      sentences.push(cleaned.slice(start, i + 1).trim());
      start = i + 1;
    }
    if (sentences.length >= limit) break;
  }
  return sentences.length ? sentences.join(" ") : cleaned;
}

function artifactKind(result) {
  const kind = displayText(result && result.kind).toLowerCase();
  const mode = state.activeMode || state.modeHint;
  const rankingCount = (result && Array.isArray(result.rankings) ? result.rankings.length : 0);
  if (mode === "compare") return state.sites.length >= 2 && rankingCount >= 2 ? "compare" : "analysis";
  if (mode === "screen") return "screen";
  if (mode === "diligence") return "diligence";
  if (mode === "brief") return "brief";
  if (rankingCount >= 2 || kind === "compare") return "compare";
  if (["screen", "diligence", "brief"].includes(kind)) return kind;
  return "analysis";
}

function workflowTitle(result) {
  const kind = artifactKind(result);
  if (kind === "compare") return "Site Comparison";
  if (kind === "screen") return "Quick Go / No-Go";
  if (kind === "diligence") return "Risk Deep Dive";
  if (kind === "brief") return "Full Site Evaluation";
  return "Recon Analysis";
}

function setArtifactTitle(result) {
  els.artifactKicker.textContent = "ARTIFACT READY";
  els.artifactTitle.textContent = workflowTitle(result); // Keeps the below-fold title aligned with the selected workflow.
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

  const nextSite = {
    lat: Number(state.draftLngLat.lat.toFixed(6)),
    lon: Number(state.draftLngLat.lon.toFixed(6)),
    label: state.draftLngLat.label || null,
  };
  if (state.sites.some((site) => shortCoords(site) === shortCoords(nextSite))) {
    setStatus("That site is already confirmed.");
    return;
  }

  state.sites.push(nextSite);
  state.draftLngLat = null;

  if (state.draftMarker) {
    state.draftMarker.setDraggable(false);
    state.draftMarker = null;
  }

  renderSites();
  updateCompareAvailability();
  setStatus(`${state.sites.length} confirmed site${state.sites.length === 1 ? "" : "s"} ready for chat.`);
}

function clearPins() {
  state.sites = [];
  if (state.draftMarker) state.draftMarker.remove();
  state.draftMarker = null;
  state.draftLngLat = null;
  renderSites();
  updateCompareAvailability();
  setStatus("Pins cleared.");
}

function siteLabel(site, index) {
  return displayText(site.label || `Site ${index + 1}`) || `Site ${index + 1}`;
}

function shortCoords(site) {
  return `${Number(site.lat).toFixed(4)}, ${Number(site.lon).toFixed(4)}`;
}

function removeSite(index) {
  if (index < 0 || index >= state.sites.length) return;
  state.sites.splice(index, 1);
  renderSites();
  updateCompareAvailability();
  setStatus(`${state.sites.length} confirmed site${state.sites.length === 1 ? "" : "s"} ready for chat.`);
}

function renderSites() {
  els.siteList.innerHTML = state.sites
    .map(
      (site, index) => `
        <li>
          <span class="site-main">
            <strong>${escapeHtml(index + 1)}. ${escapeHtml(siteLabel(site, index))}</strong>
            <span>${escapeHtml(shortCoords(site))}</span>
          </span>
          <button class="site-remove" type="button" data-index="${index}" aria-label="Remove ${escapeHtml(siteLabel(site, index))}">x</button>
        </li>
      `
    )
    .join("");
}

function renderEmptyStates() {
  els.messages.innerHTML = `<div class="empty">Confirm a site to start analysis.</div>`;
  els.traceList.innerHTML = `<li class="trace-empty">No agent loops yet. Run an analysis to see each step.</li>`;
}

function clearChatEmpty() {
  const empty = els.messages.querySelector(".empty");
  if (empty) empty.remove();
}

function addMessage(role, content) {
  clearChatEmpty();
  const row = document.createElement("div");
  row.className = `message ${role}`;
  row.innerHTML = `<div class="bubble">${escapeHtml(displayText(content))}</div>`;
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

  const statuses = workingStatuses();
  let index = 0;
  state.workingTimer = setInterval(() => {
    const label = row.querySelector(".working-text");
    if (label) label.textContent = statuses[index % statuses.length];
    index += 1;
  }, 1800);
}

function workingStatuses() {
  if (state.activeMode === "screen") return ["Running a quick screen...", "Checking flood, grid, and elevation thresholds...", "Scoring the site..."];
  if (state.activeMode === "diligence") return ["Assessing flood and interconnection risks...", "Reviewing zoning and public data gaps...", "Preparing the risk register..."];
  if (state.activeMode === "compare") return ["Comparing confirmed sites...", "Scoring each site...", "Ranking the sites..."];
  if (state.activeMode === "brief") return ["Running full diligence...", "Gathering flood, grid, elevation, power, and zoning context...", "Preparing the artifact..."];
  return ["Thinking", "Pulling flood, grid, and elevation data", "Searching the zoning ordinance", "Scoring the site"];
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
  if (content.includes("deterministic score")) return "Deterministic score";
  return "Deterministic score";
}

function findingIcon(reason) {
  const source = sourceTag(reason);
  if (source.includes("FEMA")) return { label: "FL", kind: "flood" };
  if (source.includes("HIFLD")) return { label: "KV", kind: "grid" };
  if (source.includes("USGS")) return { label: "EL", kind: "elevation" };
  if (source.includes("Zoning")) return { label: "ZO", kind: "zoning" };
  return { label: "SC", kind: "data" };
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
      : displayText(reason.point);
  }
  if (combined.includes("elevation_m") || combined.includes("elevation")) {
    return displayText(reason.point).replace(/elevation_m/gi, "elevation");
  }
  if (combined.includes("chunk") || combined.includes("zoning")) {
    return displayText(reason.point).replace(/\[chunk \d+\]/gi, "the ordinance");
  }
  const point = displayText(reason.point);
  if (point && point !== "Recon has a structured result ready below.") return point;
  return "Recon found a relevant site signal that should be reviewed.";
}

function shortSummary(value) {
  return sentenceSummary(value, 2);
}

function scoreWidth(score) {
  const value = Math.max(0, Math.min(100, Number(displayText(score)) || 0));
  return `${value}%`;
}

function rankWhy(rankings) {
  if (rankings.length < 2) return "Recon ranked the confirmed sites by deterministic screening score.";
  const first = displayText(rankings[0].label) || "The first site";
  const second = displayText(rankings[1].label) || "the second site";
  const firstScore = Number(rankings[0].score);
  const secondScore = Number(rankings[1].score);
  if (firstScore === secondScore) return `${first} and ${second} are tied at ${displayScore(firstScore)}.`;
  const base = `${first} ranks first because its deterministic score is higher than ${second}: ${displayScore(firstScore)} vs ${displayScore(secondScore)}.`;
  if (rankings.length <= 2) return base;
  const lowerNotes = rankings
    .slice(1)
    .map((row) => {
      const signals = weakSignals(row);
      return signals.length ? `${displayText(row.label)} trails on ${signals.join(" and ")}` : `${displayText(row.label)} trails on lower deterministic score`;
    })
    .slice(0, 3);
  return `${base} ${lowerNotes.join("; ")}.`;
}

function weakSignals(row) {
  const findings = rankEvidence(row);
  const signals = [];
  findings.forEach((finding) => {
    const point = displayText(finding.point).toLowerCase();
    const floodMatch = point.match(/flood zone ([a-z0-9]+)/i);
    const gridMatch = point.match(/nearest hifld substation is ([\d.]+) miles/i);
    const elevationMatch = point.match(/usgs elevation is ([\d.]+) meters/i);
    if (floodMatch && !point.includes("do not show")) signals.push(`flood zone ${floodMatch[1].toUpperCase()}`);
    if (point.includes("flood data is unavailable")) signals.push("unavailable flood data");
    if (gridMatch && Number(gridMatch[1]) > 5) signals.push(`${fmtNumber(gridMatch[1], 1)} mi grid distance`);
    if (point.includes("did not return a usable") || point.includes("grid distance is unavailable")) signals.push("unavailable grid data");
    if (elevationMatch && Number(elevationMatch[1]) < 10) signals.push("low elevation");
    if (point.includes("elevation is unavailable")) signals.push("unavailable elevation");
  });
  return [...new Set(signals)].slice(0, 2);
}

function rankEvidence(row) {
  if (Array.isArray(row.findings) && row.findings.length > 0) return row.findings;
  const reason = displayText(row.reason);
  if (!reason || reason === "Recon has a structured result ready below.") {
    return [{ point: "Scored by deterministic site screen.", grounded_in: "Deterministic score" }];
  }
  // Split only existing reason text into auditable rows; this avoids fabricating missing site signals.
  const parts = reason.split(/[.;]\s+/).map((part) => part.trim()).filter(Boolean);
  return parts.map((part) => ({ point: part, grounded_in: sourceTag({ point: part, grounded_in: "" }) }));
}

function scoreBreakdownRows(components) {
  const labels = {
    grid_proximity: "Grid proximity",
    flood: "Flood",
    elevation: "Elevation",
    zoning: "Zoning",
  };
  return Object.entries(labels)
    .filter(([key]) => components && components[key] !== null && components[key] !== undefined)
    .map(([key, label]) => `
      <div class="breakdown-row">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(displayScore(components[key]))}</strong>
      </div>
    `)
    .join("");
}

function renderChecks(checks) {
  if (!Array.isArray(checks) || checks.length === 0) return "";
  return `
    <div class="screen-checks">
      ${checks
        .map((check) => `
          <div class="check-row ${escapeHtml(displayText(check.status).toLowerCase())}">
            <span>${escapeHtml(displayText(check.status || "check"))}</span>
            <div>
              <strong>${escapeHtml(displayText(check.label))}</strong>
              <p>${escapeHtml(displayText(check.detail))}</p>
              <small>${escapeHtml(displayText(check.grounded_in))}</small>
            </div>
          </div>
        `)
        .join("")}
    </div>
  `;
}

function renderRisks(risks) {
  if (!Array.isArray(risks) || risks.length === 0) return "";
  return `
    <div>
      <p class="section-title">Risk register</p>
      <div class="risk-grid">
        ${risks
          .map((risk) => `
            <div class="risk-row ${escapeHtml(displayText(risk.severity).toLowerCase())}">
              <span>${escapeHtml(displayText(risk.severity))}</span>
              <div>
                <strong>${escapeHtml(displayText(risk.risk))}</strong>
                <p>${escapeHtml(displayText(risk.why))}</p>
                <small>${escapeHtml(displayText(risk.grounded_in))}</small>
              </div>
            </div>
          `)
          .join("")}
      </div>
    </div>
  `;
}

function renderNextSteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) return "";
  return `
    <div>
      <p class="section-title">Recommended next steps</p>
      <ol class="next-steps">
        ${steps.map((step) => `<li>${escapeHtml(displayText(step))}</li>`).join("")}
      </ol>
    </div>
  `;
}

function renderHistoryNote(note) {
  const cleaned = displayText(note);
  return cleaned ? `<div class="history-note"><strong>Compared to your history:</strong> ${escapeHtml(cleaned)}</div>` : "";
}

function scoreLabel(kind) {
  if (kind === "screen") return "Initial viability score";
  if (kind === "diligence") return "Site screening score";
  if (kind === "compare") return "Composite ranking score";
  return "Development readiness score";
}

function renderResult(result) {
  setArtifactTitle(result);
  if (!result) {
    els.resultPanel.innerHTML = `<div class="result-empty">Your generated output will appear here when Recon finishes.</div>`;
    return;
  }

  const rankings = result.rankings || [];
  const kind = artifactKind(result);
  const isCompare = kind === "compare";
  const verdict = result.verdict || (isCompare ? "COMPARE" : kind.toUpperCase());
  const pillClass = displayText(verdict).toLowerCase().replaceAll(" ", "-");
  const summary = shortSummary(result.reply || "Recon completed the evaluation.");
  const reasons = result.reasons || [];
  const breakdown = scoreBreakdownRows(result.score_components || {});

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
              <strong>${escapeHtml(displayText(row.label))}</strong>
              <div class="grounded">${escapeHtml(displayText(row.reason))}</div>
            </div>
            <div class="rank-score-cell">
              <div class="rank-score">${escapeHtml(displayScore(row.score))} / 100</div>
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

  const singleSections = [
    kind === "screen" ? renderChecks(result.checks || []) : "",
    kind === "diligence" ? renderRisks(result.risks || []) : "",
    kind === "brief" && reasons.length > 0 ? `<div><p class="section-title">Key findings</p><div class="findings-grid">${findingRows}</div></div>` : "",
    kind === "brief" && breakdown ? `<div><p class="section-title">Score breakdown</p><div class="score-breakdown">${breakdown}</div></div>` : "",
    kind !== "screen" ? renderNextSteps(result.next_steps || []) : "",
    renderHistoryNote(result.history_note),
  ].join("");

  els.resultPanel.innerHTML = `
    <article class="result-card">
      <div class="result-head">
        <div class="verdict-wrap">
          <span class="pill ${pillClass}">${escapeHtml(verdict)}</span>
          <span class="verdict-meaning">${escapeHtml(isCompare ? "Best sites ranked first." : verdictMeaning(verdict))}</span>
        </div>
        ${result.score !== null && result.score !== undefined ? `<div class="score"><span class="score-line">${escapeHtml(displayScore(result.score))} <span class="score-denom">/ 100</span></span><small>${escapeHtml(scoreLabel(kind))}</small><div class="score-bar"><span style="width:${scoreWidth(result.score)}"></span></div></div>` : ""}
      </div>
      <div class="summary">${escapeHtml(summary)}</div>
      ${isCompare ? `<div class="rank-grid"><div class="why-order"><strong>Why this order:</strong> ${escapeHtml(rankWhy(rankings))}</div>${rankRows}</div>` : singleSections}
      ${result.needs_human_review ? `<div class="human-review"><strong>What to verify:</strong> ${escapeHtml(displayText(result.needs_human_review))}</div>` : ""}
    </article>
  `;
}

function traceLabel(event) {
  if (event.type === "model_call") {
    const names = modelToolNames(event);
    return names.length ? `Decided to call ${names.join(", ")}` : "Model responded without a tool call";
  }
  if (event.type === "final") return "Prepared final result";
  if (event.type === "tool_call") return toolSignature(event);
  const subject = siteSubject(event);
  const labels = {
    get_site_data: `Pulled site data${subject}`,
    search_zoning: `Searched zoning ordinance${subject}`,
    score_site: `Scored site${subject}`,
    compare_sites: "Ranked sites",
    save_site_brief: "Saved site to memory",
    recall_site_briefs: "Checked saved sites",
  };
  return labels[event.tool] || "Ran tool";
}

function modelToolNames(event) {
  const result = event.result;
  if (!Array.isArray(result)) return [];
  return result.map((call) => displayText(call.name || call.tool || call.function?.name)).filter(Boolean);
}

function toolSignature(event) {
  const args = event.args || {};
  const siteData = args.site_data || {};
  if (event.tool === "get_site_data") return `get_site_data(lat=${fmtNumber(args.lat, 5)}, lon=${fmtNumber(args.lon, 5)})`;
  if (event.tool === "search_zoning") return `search_zoning(query="${displayText(args.query).slice(0, 64)}")`;
  if (event.tool === "score_site") return `score_site(lat=${fmtNumber(siteData.lat, 5)}, lon=${fmtNumber(siteData.lon, 5)})`;
  if (event.tool === "compare_sites") return `compare_sites(sites=${(args.scored_sites || []).length})`;
  if (event.tool === "save_site_brief") return "save_site_brief(...)";
  if (event.tool === "recall_site_briefs") return "recall_site_briefs()";
  return event.tool || "tool_call";
}

function traceSummary(event) {
  if (event.type !== "tool_call") return "";
  const result = event.result || {};
  if (result.error) return `-> ${displayText(result.error)}${result.next_step ? `; ${displayText(result.next_step)}` : ""}`;
  if (event.tool === "get_site_data") return siteDataSummary(result);
  if (event.tool === "score_site") return result.score !== undefined ? `-> score ${displayScore(result.score)}` : "";
  if (event.tool === "compare_sites") return `-> ranked ${(result.rankings || []).length} sites`;
  if (event.tool === "search_zoning") return `-> returned ${(text(result).match(/\[chunk \d+\]/g) || []).length} zoning chunks`;
  if (event.tool === "save_site_brief") return result.saved ? `-> saved ${displayText(result.site_key)}` : `-> not saved: ${displayText(result.reason)}`;
  if (event.tool === "recall_site_briefs") return `-> recalled ${result.count || 0} saved sites`;
  return "";
}

function siteDataSummary(result) {
  const flood = result.flood || {};
  const grid = result.grid_proximity || {};
  const elevation = result.elevation || {};
  const floodText = flood.fallback ? "flood unavailable" : `flood zone ${displayText(flood.flood_zone || "unavailable")}`;
  const gridText = grid.fallback
    ? "nearest substation unavailable"
    : `nearest substation ${fmtNumber(grid.nearest_substation_distance_mi, 2)} mi @ ${displayText(grid.voltage_kv || "unavailable")} kV`;
  const elevationText = elevation.fallback ? "elevation unavailable" : `elevation ${fmtNumber(elevation.elevation_m, 1)} m`;
  return `-> ${floodText}, ${gridText}, ${elevationText}`;
}

function siteSubject(event) {
  const args = event.args || {};
  const siteData = args.site_data || {};
  const lat = args.lat ?? siteData.lat;
  const lon = args.lon ?? siteData.lon;
  if (lat === undefined || lon === undefined) return "";
  const match = state.sites.find((site) => Number(site.lat).toFixed(4) === Number(lat).toFixed(4) && Number(site.lon).toFixed(4) === Number(lon).toFixed(4));
  return match ? ` for ${siteLabel(match, state.sites.indexOf(match))}` : ` for ${Number(lat).toFixed(4)}, ${Number(lon).toFixed(4)}`;
}

function siteIndexForEvent(event) {
  const args = event.args || {};
  const siteData = args.site_data || {};
  const lat = args.lat ?? siteData.lat;
  const lon = args.lon ?? siteData.lon;
  const index = state.sites.findIndex((site) => Number(site.lat).toFixed(4) === Number(lat).toFixed(4) && Number(site.lon).toFixed(4) === Number(lon).toFixed(4));
  return index >= 0 ? index + 1 : null;
}

function statusFor(event) {
  const siteIndex = siteIndexForEvent(event);
  if (event.tool === "get_site_data") return state.activeMode === "diligence" ? "Assessing public risk signals..." : "Pulling flood, grid, and elevation data...";
  if (event.tool === "search_zoning") return "Searching the zoning ordinance...";
  if (event.tool === "score_site" && state.activeMode === "compare" && siteIndex) return `Scoring site ${siteIndex} of ${state.sites.length}...`;
  if (event.tool === "score_site") return state.activeMode === "screen" ? "Running a quick screen..." : "Scoring the site...";
  if (event.tool === "compare_sites") return "Ranking sites";
  if (event.type === "final") return "Preparing the result";
  return "Thinking";
}

function resetTrace() {
  els.traceList.innerHTML = "";
  els.loopCount.textContent = "0";
  els.eventCount.textContent = "0";
}

function appendTrace(event) {
  const empty = els.traceList.querySelector(".trace-empty");
  if (empty) empty.remove();
  els.loopCount.textContent = event.loop || els.loopCount.textContent;
  els.eventCount.textContent = String(Number(els.eventCount.textContent || 0) + 1);
  const item = document.createElement("li");
  item.className = "trace-item";
  const hasDetails = event.tool || Object.keys(event.args || {}).length > 0;
  const summary = traceSummary(event);
  item.innerHTML = `
    <div class="trace-line">
      <span class="trace-type">Loop ${escapeHtml(event.loop)} - ${escapeHtml(event.type)}</span>
      <span>${escapeHtml(event.duration_ms)}ms</span>
    </div>
    <div class="trace-tool">${escapeHtml(traceLabel(event))}</div>
    ${summary ? `<div class="trace-summary">${escapeHtml(summary)}</div>` : ""}
    ${hasDetails ? `<details>
      <summary>Show raw output</summary>
      <div class="trace-args">${escapeHtml(JSON.stringify({ tool: event.tool, args: event.args || {}, result: event.result || event.result_preview || null }))}</div>
    </details>` : ""}
  `;
  els.traceList.appendChild(item);
  els.traceList.scrollTop = els.traceList.scrollHeight;
  updateWorking(statusFor(event));
}

function setMode(mode) {
  if (mode === "compare" && state.sites.length < 2) {
    updateCompareAvailability();
    return;
  }
  state.modeHint = state.modeHint === mode ? "auto" : mode;
  if (state.modeHint !== "auto" && MODE_PROMPTS[state.modeHint]) {
    els.chatInput.value = MODE_PROMPTS[state.modeHint];
    els.chatInput.focus();
  }
  els.modeBtns.forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === state.modeHint));
}

function updateCompareAvailability() {
  const compareBtn = Array.from(els.modeBtns).find((btn) => btn.dataset.mode === "compare");
  if (!compareBtn) return;
  const disabled = state.sites.length < 2;
  compareBtn.disabled = disabled;
  compareBtn.title = disabled ? "Add 2+ sites to compare" : "Evaluate several confirmed sites and rank them.";
  if (disabled && state.modeHint === "compare") state.modeHint = "auto";
  els.modeBtns.forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === state.modeHint));
  els.shortcutHelp.textContent = disabled ? "Add 2+ sites to compare." : "Choose a mode or ask directly.";
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
  els.eventCount.textContent = data.event_count || els.eventCount.textContent;
}

async function sendChat(event) {
  event.preventDefault();
  const raw = els.chatInput.value.trim();
  if (!raw) return;

  state.activeMode = state.modeHint; // Freezes artifact routing for this run even if the user changes chips while it streams.
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
  updateCompareAvailability();
  const config = await loadConfig();
  initMap(config.mapbox_token);
}

main();
