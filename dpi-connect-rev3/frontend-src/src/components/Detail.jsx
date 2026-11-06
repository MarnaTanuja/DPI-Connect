import React, { useEffect, useState } from "react";
import { apiCall, downloadJson } from "../api.js";
import { ErrorText, Empty, StatusPill, Icon, pct } from "../ui.jsx";
import { AdminReviewBox, AuditTimeline, DataTable, DatasetView, PerOrgTable, ReportTable, SummaryCards } from "./Report.jsx";
import { PageHeader } from "./Views.jsx";

const PREVIEW_ROW_LIMIT = 200;

function ConsentForm({ id, onDone }) {
  const [signatory, setSignatory] = useState("");
  const [text, setText] = useState("We agree to share the requested dataset for the stated purpose, under our organization’s data-sharing policy.");
  const [error, setError] = useState("");
  async function decide(e) {
    e.preventDefault();
    const decision = e.nativeEvent.submitter?.dataset?.decision || "approve";
    setError("");
    try {
      await apiCall(`/api/connections/${id}/consent`, { method: "POST", body: { agreed: decision === "approve", signatory_name: signatory.trim(), agreement_text: text.trim() } });
      onDone();
    } catch (err) { setError(err.message); }
  }
  return (
    <form className="card accent" onSubmit={decide}>
      <h3>Your consent is needed</h3>
            <label htmlFor="consent-signatory">Your name / title</label>
      <input id="consent-signatory" value={signatory} onChange={(e) => setSignatory(e.target.value)} placeholder="e.g. Grace Hopper, Data Protection Officer" required />
      <label htmlFor="consent-text">Agreement</label>
      <textarea id="consent-text" rows={3} value={text} onChange={(e) => setText(e.target.value)} required />
      <div className="row-actions">
        <button data-decision="approve" className="btn btn-primary btn-small">Approve &amp; sign</button>
        <button data-decision="reject" className="btn btn-danger btn-small">Decline</button>
      </div>
      <ErrorText>{error}</ErrorText>
    </form>
  );
}

function ExchangeBox({ id, onExchanged }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  async function pull() {
    setBusy(true); setError("");
    try { setResult(await apiCall(`/api/connections/${id}/exchange`, { method: "POST" })); onExchanged(); }
    catch (e) { setError(e.message); }
    setBusy(false);
  }
  return (
    <div className="card accent">
      <h3>Combined dataset</h3>
            <div className="row-actions"><button className="btn btn-primary btn-small" onClick={pull} disabled={busy}>{busy ? "Pulling…" : "Pull combined dataset"}</button></div>
      <ErrorText>{error}</ErrorText>
      {result && (
        <div className="exchange-result">
          <p>
            <strong>{result.combined_record_count} records</strong> combined (full outer join: {result.matched_count} matched,{" "}
            {result.unmatched_requester_count} only in your data, {result.unmatched_counterparty_count} only in theirs), signed with envelope{" "}
            <code>{result.xroad_envelope.signature.slice(0, 24)}…</code>
          </p>
          {result.combined_record_count > PREVIEW_ROW_LIMIT && <p className="note">Showing the first {PREVIEW_ROW_LIMIT} rows — download the JSON for all {result.combined_record_count}.</p>}
          <DataTable records={result.combined_dataset.slice(0, PREVIEW_ROW_LIMIT)} lineage={result.column_lineage} />
          <div className="row-actions"><button className="btn btn-ghost btn-small" onClick={() => downloadJson(result.combined_dataset, `combined-dataset-${id.slice(0, 8)}.json`)}>Download as JSON</button></div>
        </div>
      )}
    </div>
  );
}

export function ConnectionDetail({ id, session, onBack }) {
  const [state, setState] = useState({ loading: true });
  const [auditKey, setAuditKey] = useState(0);
  const [audit, setAudit] = useState(null);

  const load = () => {
    setState({ loading: true });
    Promise.all([apiCall(`/api/connections/${id}`), apiCall(`/api/connections/${id}/audit`)])
      .then(([conn, a]) => { setState({ conn }); setAudit(a); })
      .catch((e) => setState({ error: e.message }));
  };
  useEffect(load, [id]);
  useEffect(() => { if (auditKey) apiCall(`/api/connections/${id}/audit`).then(setAudit).catch(() => {}); }, [auditKey]);

  const back = <button className="back" onClick={onBack}><Icon name="back" size={16} /> Back</button>;
  if (state.loading) return <>{back}<Empty>Loading…</Empty></>;
  if (state.error) return <>{back}<ErrorText>{state.error}</ErrorText></>;

  const { conn } = state;
  const isRequester = conn.requester_org_id === session.id;
  const isCounterparty = conn.counterparty_org_id === session.id;
  return (
    <>
      {back}
      <div className="detail-head">
        <h2>{conn.requester_org_name} <span className="arrow">→</span> {conn.counterparty_org_name}</h2>
        <StatusPill status={conn.status} />
      </div>
      <p className="lead-sm">{conn.purpose}</p>
      <AdminReviewBox r={conn.admin_review} />
      <div className="card"><h3>Mapping report</h3><SummaryCards sum={conn.scoring_summary} /><ReportTable conn={conn} /></div>
      {conn.status === "PENDING_ADMIN_REVIEW" && (
        <div className="card notice"><span className="pill pill-review"><i />Waiting for the platform admin</span></div>
      )}
      {conn.status === "PENDING_CONSENT" && isCounterparty && <ConsentForm id={id} onDone={load} />}
      {conn.status === "ACTIVE" && isRequester && <ExchangeBox id={id} onExchanged={() => setAuditKey((k) => k + 1)} />}
      <div className="card"><h3>Audit trail</h3><AuditTimeline entries={audit} /></div>
    </>
  );
}

/* ---------------- Admin ---------------- */
export function AdminQueue({ onOpen }) {
  const [filter, setFilter] = useState("PENDING_ADMIN_REVIEW");
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setRows(null);
    apiCall("/api/admin/connections" + (filter ? `?status=${filter}` : "")).then(setRows).catch((e) => setError(e.message));
  }, [filter]);
  return (
    <>
      <PageHeader title="Review queue" />
      <div className="segmented segmented-inline">
        <button className={filter ? "active" : ""} onClick={() => setFilter("PENDING_ADMIN_REVIEW")}>Awaiting review</button>
        <button className={!filter ? "active" : ""} onClick={() => setFilter("")}>All</button>
      </div>
      <ErrorText>{error}</ErrorText>
      <div className="card flush">
        {!rows && <Empty>Loading…</Empty>}
        {rows && !rows.length && <Empty>Nothing here.</Empty>}
        {rows && rows.map((c) => {
          const s = c.scoring_summary || {};
          return (
            <div className="row clickable" key={c.id} onClick={() => onOpen(c.id)}>
              <div className="row-icon"><Icon name="shield" /></div>
              <div className="row-main">
                <span className="row-title">{c.requester_org_name} → {c.counterparty_org_name}</span>
                <span className="row-sub">{c.purpose.slice(0, 90)}</span>
              </div>
              <div className="row-meta">
                <span className="row-sub">
                  {s.mean_match_confidence != null && `${pct(s.mean_match_confidence)} avg. confidence`}
                  {s.needs_review ? <> · <strong className="warn">{s.needs_review} flagged</strong></> : null}
                </span>
                <StatusPill status={c.status} />
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

export function AdminReview({ id, onBack }) {
  const [c, setC] = useState(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [formError, setFormError] = useState("");

  const load = () => apiCall(`/api/admin/connections/${id}`).then(setC).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [id]);

  const back = <button className="back" onClick={onBack}><Icon name="back" size={16} /> Back</button>;
  if (error) return <>{back}<ErrorText>{error}</ErrorText></>;
  if (!c) return <>{back}<Empty>Loading…</Empty></>;

  const flagged = c.scoring_summary.needs_review || 0;
  async function decide(e) {
    e.preventDefault();
    const decision = e.nativeEvent.submitter?.dataset?.decision || "approve";
    if (decision === "approve" && flagged && !window.confirm(`${flagged} flagged column(s) remain. Approve anyway?`)) return;
    try {
      await apiCall(`/api/admin/connections/${id}/decision`, { method: "POST", body: { decision, reviewer_name: name.trim(), notes: notes.trim() } });
      load();
    } catch (err) { setFormError(err.message); }
  }

  return (
    <>
      {back}
      <div className="detail-head">
        <h2>{c.requester_org_name} <span className="arrow">→</span> {c.counterparty_org_name}</h2>
        <StatusPill status={c.status} />
      </div>
      <p className="lead-sm">{c.purpose}</p>
      <AdminReviewBox r={c.admin_review} />
      <div className="card"><h3>Confidence overview</h3><SummaryCards sum={c.scoring_summary} /></div>
      <div className="card"><h3>Field mappings by output column</h3>
                <ReportTable conn={c} /></div>
      <div className="card"><h3>Mappings per organization</h3>
        <div className="two-col">
          <PerOrgTable report={c.reconciliation_report} side="a" orgName={c.requester_org_name} />
          <PerOrgTable report={c.reconciliation_report} side="b" orgName={c.counterparty_org_name} />
        </div></div>
      <div className="card"><h3>Source datasets</h3>
        <div className="two-col"><DatasetView d={c.requester_dataset} /><DatasetView d={c.counterparty_dataset} /></div></div>
      {c.status === "PENDING_ADMIN_REVIEW" && (
        <form className="card accent" onSubmit={decide}>
          <h3>Your decision</h3>
          {flagged > 0 && <p><strong className="warn">{flagged} column{flagged === 1 ? " is" : "s are"} flagged.</strong></p>}
          <label htmlFor="admin-name">Your name</label>
          <input id="admin-name" value={name} onChange={(e) => setName(e.target.value)} required placeholder="e.g. Platform Data Steward" />
          <label htmlFor="admin-notes">Notes <span className="faint">(visible to both organizations)</span></label>
          <textarea id="admin-notes" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
          <div className="row-actions">
            <button data-decision="approve" className="btn btn-primary btn-small">Approve connection</button>
            <button data-decision="reject" className="btn btn-danger btn-small">Reject</button>
          </div>
          <ErrorText>{formError}</ErrorText>
        </form>
      )}
      <div className="card"><h3>Audit trail</h3><AuditTimeline entries={c.audit} /></div>
    </>
  );
}
