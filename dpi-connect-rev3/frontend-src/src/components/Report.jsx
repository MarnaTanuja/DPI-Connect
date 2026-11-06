import React from "react";
import { Confidence, StatCards, pct, fmtDate } from "../ui.jsx";

const KIND_LABELS = { join_key: "Join key", matched: "Matched", only_a: "Only in requester", only_b: "Only in counterparty", unmapped: "Unmapped" };
const ORDER = { join_key: 0, matched: 1, only_a: 2, only_b: 3, unmapped: 4 };

export function SummaryCards({ sum }) {
  if (!sum || sum.mean_match_confidence === undefined) return null;
  const jk = sum.join_key ? `${sum.join_key.requester_field} ↔ ${sum.join_key.counterparty_field}` : "none found — all rows kept";
  return (
    <>
      <StatCards
        items={[
          { value: pct(sum.schema_overlap), label: "Schema overlap" },
          { value: pct(sum.mean_match_confidence), label: "Avg. match confidence" },
          { value: sum.matched_columns, label: "Shared columns" },
          { value: sum.needs_review, label: "Need a human look", warn: sum.needs_review > 0 },
        ]}
      />
      <p className="note">Join key: <code>{jk}</code></p>
    </>
  );
}

function Evidence({ c }) {
  const comp = c.components || {};
  const bits = [];
  if (comp.lexical !== undefined) {
    bits.push(`name spelling ${pct(comp.lexical)}`, `meaning ${pct(comp.semantic)}`, `value shape ${pct(comp.structural)}`);
    if (comp.value_agreement !== null && comp.value_agreement !== undefined)
      bits.push(`values agree ${pct(comp.value_agreement)} (n=${comp.joined_compared})`);
  }
  return (
    <details className="why">
      <summary>{(c.evidence || [])[0] || "details"}</summary>
      {bits.length > 0 && <div className="chips">{bits.map((b) => <span className="chip" key={b}>{b}</span>)}</div>}
      <ul>{(c.evidence || []).map((e, i) => <li key={i}>{e}</li>)}</ul>
    </details>
  );
}

export function ReportTable({ conn }) {
  const report = conn.reconciliation_report || [];
  if (!report.length) return <div className="empty">No field mappings generated.</div>;
  const rows = [...report].sort((x, y) => (ORDER[x.kind] - ORDER[y.kind]) || (y.confidence - x.confidence));
  return (
    <div className="table-wrap">
      <table className="table table-report">
        <thead>
          <tr>
            <th>Output column</th>
            <th>{conn.requester_org_name} field</th>
            <th>{conn.counterparty_org_name} field</th>
            <th>Kind</th>
            <th>Confidence</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c, i) => (
            <tr key={i} className={c.needs_review ? "row-flag" : ""}>
              <td><code>{c.output_field}</code></td>
              <td>{c.field_a ? <code>{c.field_a}</code> : <em className="faint">—</em>}</td>
              <td>{c.field_b ? <code>{c.field_b}</code> : <em className="faint">—</em>}</td>
              <td>
                <span className={`kind kind-${c.kind}`}>{KIND_LABELS[c.kind] || c.kind}</span>
                {c.declared_alias && <span className="kind"> declared</span>}
              </td>
              <td><Confidence c={c} /></td>
              <td><Evidence c={c} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PerOrgTable({ report, side, orgName }) {
  const key = side === "a" ? "field_a" : "field_b";
  return (
    <div className="ds-view">
      <h4>{orgName} mappings</h4>
      <div className="table-wrap"><table className="table table-compact">
        <thead><tr><th>Source field</th><th>Output column</th><th>Confidence</th></tr></thead>
        <tbody>
          {report.filter((c) => c[key]).map((c, i) => (
            <tr key={i}>
              <td><code>{c[key]}</code></td>
              <td>→ <code>{c.output_field}</code></td>
              <td><Confidence c={c} /></td>
            </tr>
          ))}
        </tbody>
      </table></div>
    </div>
  );
}

export function DatasetView({ d }) {
  return (
    <div className="ds-view">
      <h4>{d.org_name} — <code>{d.dataset_name}</code></h4>
      <p className="note">{d.record_count} records</p>
      <div className="chips">
        {d.fields.map((f) => (
          <span className="chip" key={f.name} title={`${f.distinct} distinct, ${f.non_null} filled`}>
            {f.name} <em>{f.value_kind}</em>
          </span>
        ))}
      </div>
    </div>
  );
}

export function AdminReviewBox({ r }) {
  if (!r) return null;
  const ok = r.decision === "approved";
  return (
    <div className="card">
      <h3>Admin decision <span className={`pill pill-${ok ? "active" : "rejected"}`}><i />{ok ? "Approved" : "Rejected"}</span></h3>
      <p>Reviewed by <strong>{r.reviewer_name}</strong> on {fmtDate(r.decided_at)}.</p>
      {r.notes && <blockquote>“{r.notes}”</blockquote>}
    </div>
  );
}

const AUDIT_LABELS = {
  connection_requested: "Connection requested",
  admin_review_approved: "Admin approved the mappings",
  admin_review_rejected: "Admin rejected the connection",
  consent_approved: "Consent approved",
  consent_rejected: "Consent declined",
  xroad_secure_handshake: "Secure channel established (X-Road)",
  xroad_secure_exchange: "Data exchanged over secure channel (X-Road)",
};

export function AuditTimeline({ entries }) {
  if (!entries || entries.length === 0) return <div className="empty">No events yet.</div>;
  return (
    <ul className="timeline">
      {entries.map((e, i) => (
        <li className="timeline-item" key={i}>
          <div className="timeline-event">{AUDIT_LABELS[e.event_type] || e.event_type}</div>
          <div className="timeline-time">{fmtDate(e.created_at)}</div>
          <div className="timeline-sig">sig: {e.xroad_signature.slice(0, 32)}…</div>
        </li>
      ))}
    </ul>
  );
}

export function DataTable({ records, lineage }) {
  if (!records.length) return <div className="empty">No records.</div>;
  const lin = {};
  (lineage || []).forEach((l) => { lin[l.output_field] = l; });
  const cols = Array.from(records.reduce((s, r) => { Object.keys(r).forEach((k) => s.add(k)); return s; }, new Set()));
  return (
    <div className="table-wrap">
      <table className="table data-table">
        <thead>
          <tr>
            {cols.map((c) => {
              const l = lin[c];
              const sub = l && l.field_a && l.field_b && l.field_a !== l.field_b;
              return (
                <th key={c}>
                  {c}
                  {sub && <div className="th-sub">A: {l.field_a} · B: {l.field_b} · {Math.round(l.confidence * 100)}%</div>}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {records.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{c === "_source_org" ? <span className="source-tag">{String(r[c] ?? "")}</span> : String(r[c] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
