// DPI Connect — frontend app logic (vanilla JS, no build step)

const API = ""; // same origin

// ---------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------
function getSession() {
  const raw = localStorage.getItem("dpi_connect_org");
  return raw ? JSON.parse(raw) : null;
}
function setSession(org) {
  localStorage.setItem("dpi_connect_org", JSON.stringify(org));
}
function clearSession() {
  localStorage.removeItem("dpi_connect_org");
}

// ---------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------
async function apiCall(path, { method = "GET", body, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const session = getSession();
    if (session) headers["X-API-Key"] = session.api_key;
  }
  const res = await fetch(API + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const message = (data && (data.detail || data.message)) || res.statusText;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

// ---------------------------------------------------------------------
// Auth screen
// ---------------------------------------------------------------------
const authScreen = document.getElementById("auth-screen");
const appShell = document.getElementById("app-shell");

document.querySelectorAll(".auth-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".auth-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("login-form").classList.toggle("hidden", tab.dataset.tab !== "login");
    document.getElementById("register-form").classList.toggle("hidden", tab.dataset.tab !== "register");
  });
});

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = document.getElementById("login-key").value.trim();
  const errorEl = document.getElementById("login-error");
  errorEl.textContent = "";
  try {
    const org = await fetch(API + "/api/orgs/me", { headers: { "X-API-Key": key } });
    if (!org.ok) throw new Error("That key wasn't recognized.");
    const data = await org.json();
    setSession({ id: data.id, name: data.name, api_key: key });
    enterApp();
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

document.getElementById("register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("reg-name").value.trim();
  const contact_email = document.getElementById("reg-email").value.trim();
  const errorEl = document.getElementById("register-error");
  errorEl.textContent = "";
  try {
    const data = await apiCall("/api/orgs/register", {
      method: "POST", body: { name, contact_email }, auth: false,
    });
    document.getElementById("register-form").classList.add("hidden");
    document.querySelector('.auth-tab[data-tab="login"]').classList.remove("hidden");
    document.querySelector('.auth-tab[data-tab="register"]').classList.remove("hidden");
    const banner = document.getElementById("new-key-banner");
    banner.classList.remove("hidden");
    document.getElementById("new-key-value").textContent = data.api_key;
    banner._pendingSession = { id: data.org_id, name: data.name, api_key: data.api_key };
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

document.getElementById("copy-key-btn").addEventListener("click", () => {
  const key = document.getElementById("new-key-value").textContent;
  navigator.clipboard?.writeText(key);
});

document.getElementById("continue-with-key-btn").addEventListener("click", () => {
  const banner = document.getElementById("new-key-banner");
  if (banner._pendingSession) {
    setSession(banner._pendingSession);
    enterApp();
  }
});

document.getElementById("sign-out-btn").addEventListener("click", () => {
  clearSession();
  location.reload();
});

// ---------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------
document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", () => showView(link.dataset.view));
});
document.getElementById("detail-back-btn").addEventListener("click", () => showView("mine"));

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.querySelectorAll(".nav-link").forEach((l) => l.classList.remove("active"));
  const target = document.getElementById(`view-${name}`);
  if (target) target.classList.remove("hidden");
  const navLink = document.querySelector(`.nav-link[data-view="${name}"]`);
  if (navLink) navLink.classList.add("active");

  if (name === "overview") loadOverview();
  if (name === "datasets") loadDatasets();
  if (name === "request") loadRequestForm();
  if (name === "mine") loadMine();
  if (name === "incoming") loadIncoming();
}

function enterApp() {
  const session = getSession();
  authScreen.classList.add("hidden");
  appShell.classList.remove("hidden");
  document.getElementById("org-name-display").textContent = session.name;
  showView("overview");
}

// ---------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------
async function loadOverview() {
  try {
    const [datasets, mine, incoming] = await Promise.all([
      apiCall("/api/datasets/mine"),
      apiCall("/api/connections/mine"),
      apiCall("/api/connections/incoming"),
    ]);
    document.getElementById("stat-datasets").textContent = datasets.length;
    document.getElementById("stat-outgoing").textContent = mine.length;
    document.getElementById("stat-incoming").textContent =
      incoming.filter((c) => c.status === "PENDING_CONSENT").length;
  } catch (err) {
    console.error(err);
  }
}

// ---------------------------------------------------------------------
// Datasets
// ---------------------------------------------------------------------
async function loadDatasets() {
  const list = document.getElementById("dataset-list");
  list.innerHTML = `<p class="empty-state">Loading…</p>`;
  try {
    const datasets = await apiCall("/api/datasets/mine");
    if (datasets.length === 0) {
      list.innerHTML = `<p class="empty-state">No datasets yet. Upload one below to get started.</p>`;
      return;
    }
    list.innerHTML = datasets.map((d) => `
      <div class="row-item" style="cursor:default">
        <div class="row-main">
          <span class="row-title">${escapeHtml(d.name)}</span>
          <span class="row-sub">${d.field_names.map(escapeHtml).join(", ")}</span>
        </div>
        <div class="row-meta">
          <span class="row-sub">${d.record_count} sample record${d.record_count === 1 ? "" : "s"}</span>
        </div>
      </div>
    `).join("");
  } catch (err) {
    list.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

document.getElementById("dataset-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("ds-name").value.trim();
  const errorEl = document.getElementById("dataset-error");
  errorEl.textContent = "";
  let records;
  try {
    records = JSON.parse(document.getElementById("ds-json").value);
    if (!Array.isArray(records) || records.length === 0) throw new Error("");
  } catch {
    errorEl.textContent = "That doesn't look like a valid JSON array of objects. Check the example placeholder.";
    return;
  }
  try {
    await apiCall("/api/datasets", { method: "POST", body: { name, sample_records: records } });
    document.getElementById("dataset-form").reset();
    document.querySelector(".upload-panel").removeAttribute("open");
    loadDatasets();
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

// ---------------------------------------------------------------------
// Request a connection
// ---------------------------------------------------------------------
async function loadRequestForm() {
  const counterpartySelect = document.getElementById("req-counterparty");
  const myDatasetSelect = document.getElementById("req-my-dataset");
  const theirDatasetSelect = document.getElementById("req-their-dataset");
  document.getElementById("request-result").classList.add("hidden");
  document.getElementById("request-error").textContent = "";

  const session = getSession();
  try {
    const [orgs, myDatasets] = await Promise.all([
      apiCall("/api/orgs"),
      apiCall("/api/datasets/mine"),
    ]);
    const others = orgs.filter((o) => o.id !== session.id);
    counterpartySelect.innerHTML = others.length
      ? `<option value="">Select…</option>` + others.map((o) => `<option value="${o.id}">${escapeHtml(o.name)}</option>`).join("")
      : `<option value="">No other organizations registered yet</option>`;
    myDatasetSelect.innerHTML = myDatasets.length
      ? `<option value="">Select…</option>` + myDatasets.map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${d.field_names.join(", ")})</option>`).join("")
      : `<option value="">Upload a dataset first</option>`;
    theirDatasetSelect.innerHTML = `<option value="">Select an organization first…</option>`;
    theirDatasetSelect.disabled = true;
  } catch (err) {
    document.getElementById("request-error").textContent = err.message;
  }
}

document.getElementById("req-counterparty").addEventListener("change", async (e) => {
  const theirDatasetSelect = document.getElementById("req-their-dataset");
  const orgId = e.target.value;
  if (!orgId) {
    theirDatasetSelect.innerHTML = `<option value="">Select an organization first…</option>`;
    theirDatasetSelect.disabled = true;
    return;
  }
  theirDatasetSelect.innerHTML = `<option value="">Loading…</option>`;
  try {
    const datasets = await apiCall(`/api/orgs/${orgId}/datasets`);
    theirDatasetSelect.disabled = false;
    theirDatasetSelect.innerHTML = datasets.length
      ? `<option value="">Select…</option>` + datasets.map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${d.field_names.join(", ")})</option>`).join("")
      : `<option value="">That organization hasn't uploaded a dataset yet</option>`;
  } catch (err) {
    theirDatasetSelect.innerHTML = `<option value="">Failed to load</option>`;
  }
});

document.getElementById("request-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("request-error");
  errorEl.textContent = "";
  const body = {
    counterparty_org_id: document.getElementById("req-counterparty").value,
    requester_dataset_id: document.getElementById("req-my-dataset").value,
    counterparty_dataset_id: document.getElementById("req-their-dataset").value,
    purpose: document.getElementById("req-purpose").value.trim(),
  };
  if (!body.counterparty_org_id || !body.requester_dataset_id || !body.counterparty_dataset_id) {
    errorEl.textContent = "Please fill in every field.";
    return;
  }
  try {
    const conn = await apiCall("/api/connections", { method: "POST", body });
    const resultEl = document.getElementById("request-result");
    resultEl.classList.remove("hidden");
    resultEl.innerHTML = `
      <div class="section-box">
        <h3>Request sent</h3>
        <p>Your request to <strong>${escapeHtml(conn.counterparty_org_name)}</strong> is now
        <span class="pill pill-pending">Pending consent</span>. Here's the similarity report we
        sent along with it — they'll see the same thing before deciding.</p>
        ${similarityMeterHtml(conn.similarity_score)}
        ${mappingTableHtml(conn.mappings)}
        <button class="btn btn-ghost btn-small" onclick="openConnection('${conn.id}')">View this request</button>
      </div>
    `;
    document.getElementById("request-form").reset();
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

// ---------------------------------------------------------------------
// My Requests / Incoming Requests (list views)
// ---------------------------------------------------------------------
async function loadMine() {
  await renderConnectionList("mine-list", "/api/connections/mine", {
    emptyText: "You haven't requested any connections yet.",
    otherPartyLabel: (c) => `To ${c.counterparty_org_name}`,
  });
}
async function loadIncoming() {
  await renderConnectionList("incoming-list", "/api/connections/incoming", {
    emptyText: "No one has requested a connection with you yet.",
    otherPartyLabel: (c) => `From ${c.requester_org_name}`,
  });
}

async function renderConnectionList(elId, path, opts) {
  const el = document.getElementById(elId);
  el.innerHTML = `<p class="empty-state">Loading…</p>`;
  try {
    const conns = await apiCall(path);
    if (conns.length === 0) {
      el.innerHTML = `<p class="empty-state">${opts.emptyText}</p>`;
      return;
    }
    conns.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    el.innerHTML = conns.map((c) => `
      <div class="row-item" onclick="openConnection('${c.id}')">
        <div class="row-main">
          <span class="row-title">${escapeHtml(opts.otherPartyLabel(c))}</span>
          <span class="row-sub">${escapeHtml(c.purpose).slice(0, 90)}${c.purpose.length > 90 ? "…" : ""}</span>
        </div>
        <div class="row-meta">
          ${c.similarity_score !== null ? `<span class="row-sub">${Math.round(c.similarity_score * 100)}% match</span>` : ""}
          ${statusPillHtml(c.status)}
        </div>
      </div>
    `).join("");
  } catch (err) {
    el.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

// ---------------------------------------------------------------------
// Connection detail
// ---------------------------------------------------------------------
async function openConnection(id) {
  showView("detail");
  const content = document.getElementById("detail-content");
  content.innerHTML = `<p class="empty-state">Loading…</p>`;
  const session = getSession();

  try {
    const [conn, audit] = await Promise.all([
      apiCall(`/api/connections/${id}`),
      apiCall(`/api/connections/${id}/audit`),
    ]);
    const isRequester = conn.requester_org_id === session.id;
    const isCounterparty = conn.counterparty_org_id === session.id;

    let html = `
      <div class="detail-header">
        <h2>${escapeHtml(conn.requester_org_name)} &rarr; ${escapeHtml(conn.counterparty_org_name)}</h2>
        ${statusPillHtml(conn.status)}
      </div>
      <p class="detail-parties">${escapeHtml(conn.purpose)}</p>

      <div class="section-box">
        <h3>Similarity report</h3>
        ${similarityMeterHtml(conn.similarity_score)}
        ${mappingTableHtml(conn.mappings)}
      </div>
    `;

    if (conn.status === "PENDING_CONSENT" && isCounterparty) {
      html += consentFormHtml(id);
    }

    if (conn.status === "ACTIVE" && isRequester) {
      html += `
        <div class="section-box" id="exchange-box">
          <h3>Combined dataset</h3>
          <p>The connection is active. Pull the combined dataset — your records plus theirs, mapped into your shape, over the signed secure channel.</p>
          <button class="btn btn-primary btn-small" id="exchange-btn">Pull combined dataset</button>
          <div id="exchange-result"></div>
        </div>
      `;
    }

    html += `
      <div class="section-box">
        <h3>Audit trail</h3>
        <div id="audit-trail">${auditTimelineHtml(audit)}</div>
      </div>
    `;

    content.innerHTML = html;

    const consentForm = document.getElementById("consent-form");
    if (consentForm) attachConsentHandler(consentForm, id);

    const exchangeBtn = document.getElementById("exchange-btn");
    if (exchangeBtn) attachExchangeHandler(exchangeBtn, id);
  } catch (err) {
    content.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
  }
}

function consentFormHtml(connId) {
  return `
    <div class="section-box">
      <h3>Your consent is needed</h3>
      <p>Review the similarity report above, then approve or decline. Approving opens a signed
      secure channel and lets the requester pull your data, transformed into their schema.</p>
      <form id="consent-form">
        <label for="consent-signatory">Your name / title</label>
        <input id="consent-signatory" type="text" placeholder="e.g. Grace Hopper, Data Protection Officer" required>
        <label for="consent-text">Agreement</label>
        <textarea id="consent-text" rows="3" required>We agree to share the requested dataset for the stated purpose, under our organization's data-sharing policy.</textarea>
        <div class="consent-actions">
          <button type="submit" data-decision="approve" class="btn btn-primary btn-small">Approve &amp; sign</button>
          <button type="submit" data-decision="reject" class="btn btn-reject btn-small">Decline</button>
        </div>
        <p id="consent-error" class="form-error"></p>
      </form>
    </div>
  `;
}

function attachConsentHandler(form, connId) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const decision = e.submitter?.dataset?.decision || "approve";
    const errorEl = document.getElementById("consent-error");
    errorEl.textContent = "";
    try {
      await apiCall(`/api/connections/${connId}/consent`, {
        method: "POST",
        body: {
          agreed: decision === "approve",
          signatory_name: document.getElementById("consent-signatory").value.trim(),
          agreement_text: document.getElementById("consent-text").value.trim(),
        },
      });
      openConnection(connId);
    } catch (err) {
      errorEl.textContent = err.message;
    }
  });
}

function attachExchangeHandler(btn, connId) {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Pulling…";
    const resultEl = document.getElementById("exchange-result");
    try {
      const result = await apiCall(`/api/connections/${connId}/exchange`, { method: "POST" });
      resultEl.innerHTML = `
        <p style="margin-top:1rem"><strong>${result.combined_record_count} records</strong> combined,
        signed with envelope <code class="mono">${result.xroad_envelope.signature.slice(0, 24)}…</code></p>
        ${combinedTableHtml(result.combined_dataset)}
        <button class="btn btn-ghost btn-small" id="download-btn" style="margin-top:0.8rem">Download as JSON</button>
      `;
      document.getElementById("download-btn").addEventListener("click", () => {
        downloadJson(result.combined_dataset, `combined-dataset-${connId.slice(0, 8)}.json`);
      });
      // Refresh just the audit trail to show the new exchange event, without
      // re-rendering the whole panel (that would wipe the result above).
      const auditTrailEl = document.getElementById("audit-trail");
      if (auditTrailEl) {
        apiCall(`/api/connections/${connId}/audit`)
          .then((audit) => { auditTrailEl.innerHTML = auditTimelineHtml(audit); })
          .catch(() => {});
      }
      btn.disabled = false;
      btn.textContent = "Pull combined dataset";
    } catch (err) {
      resultEl.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
      btn.disabled = false;
      btn.textContent = "Pull combined dataset";
    }
  });
}

// ---------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------
function statusPillHtml(status) {
  const map = {
    PENDING_CONSENT: ["pill-pending", "Pending consent"],
    APPROVED: ["pill-approved", "Approved"],
    ACTIVE: ["pill-active", "Active"],
    REJECTED: ["pill-rejected", "Declined"],
  };
  const [cls, label] = map[status] || ["pill-pending", status];
  return `<span class="pill ${cls}">${label}</span>`;
}

function similarityMeterHtml(score) {
  const pct = Math.round((score || 0) * 100);
  return `
    <div class="similarity-meter">
      <div class="similarity-track"><div class="similarity-fill" style="width:${pct}%"></div></div>
      <div class="similarity-label">${pct}% of target fields could be confidently mapped from the source dataset</div>
    </div>
  `;
}

function mappingTableHtml(mappings) {
  if (!mappings || mappings.length === 0) return `<p class="empty-state">No field mappings generated.</p>`;
  const rows = mappings.map((m) => `
    <tr>
      <td><code>${escapeHtml(m.target_field)}</code></td>
      <td>${m.source_fields.length ? m.source_fields.map((f) => `<code>${escapeHtml(f)}</code>`).join(", ") : "<em>none</em>"}</td>
      <td>${escapeHtml(m.transform_type)}</td>
      <td class="${m.confidence >= 0.75 ? "confidence-ok" : "confidence-low"}">${Math.round(m.confidence * 100)}%</td>
    </tr>
  `).join("");
  return `
    <table class="mapping-table">
      <thead><tr><th>Target field</th><th>Source field(s)</th><th>Transform</th><th>Confidence</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function auditTimelineHtml(entries) {
  if (entries.length === 0) return `<p class="empty-state">No events yet.</p>`;
  const labels = {
    connection_requested: "Connection requested",
    consent_approved: "Consent approved",
    consent_rejected: "Consent declined",
    xroad_secure_handshake: "Secure channel established (X-Road)",
    xroad_secure_exchange: "Data exchanged over secure channel (X-Road)",
  };
  return `<ul class="timeline">${entries.map((e) => `
    <li class="timeline-item">
      <div class="timeline-event">${labels[e.event_type] || e.event_type}</div>
      <div class="timeline-time">${new Date(e.created_at).toLocaleString()}</div>
      <div class="timeline-sig">sig: ${e.xroad_signature.slice(0, 32)}…</div>
    </li>
  `).join("")}</ul>`;
}

function combinedTableHtml(records) {
  if (records.length === 0) return `<p class="empty-state">No records.</p>`;
  const cols = Array.from(records.reduce((set, r) => { Object.keys(r).forEach((k) => set.add(k)); return set; }, new Set()));
  const rows = records.map((r) => `
    <tr>${cols.map((c) => c === "_source_org"
      ? `<td><span class="source-org-tag">${escapeHtml(String(r[c] ?? ""))}</span></td>`
      : `<td>${escapeHtml(String(r[c] ?? ""))}</td>`).join("")}</tr>
  `).join("");
  return `
    <div class="data-table-wrap" style="margin-top:0.8rem">
      <table class="data-table">
        <thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function downloadJson(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
(function boot() {
  const session = getSession();
  if (session) enterApp();
})();
