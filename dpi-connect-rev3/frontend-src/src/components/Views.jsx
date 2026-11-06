import React, { useEffect, useState } from "react";
import { apiCall } from "../api.js";
import { Empty, ErrorText, StatCards, StatusPill, Icon } from "../ui.jsx";
import { DataTable, SummaryCards, ReportTable } from "./Report.jsx";

export function PageHeader({ title, children }) {
  return (
    <header className="page-header">
      <h2>{title}</h2>
      {children}
    </header>
  );
}

/* ---------------- Overview ---------------- */
export function Overview({ session, go, openConnection }) {
  const [s, setS] = useState({ d: "—", o: "—", i: "—" });
  const [recent, setRecent] = useState([]);
  useEffect(() => {
    Promise.all([apiCall("/api/datasets/mine"), apiCall("/api/connections/mine"), apiCall("/api/connections/incoming")])
      .then(([d, m, inc]) => {
        setS({ d: d.length, o: m.length, i: inc.filter((c) => c.status === "PENDING_CONSENT").length });
        setRecent([...m.map((c) => ({ ...c, _dir: "To " + c.counterparty_org_name })), ...inc.map((c) => ({ ...c, _dir: "From " + c.requester_org_name }))]
          .sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 5));
      })
      .catch(console.error);
  }, []);
  return (
    <>
      <div className="welcome">
        <div>
          <p className="eyebrow">Welcome back</p>
          <h2>{session.name}</h2>
        </div>
        <div className="row-actions" style={{ marginTop: 0 }}>
          <button className="btn btn-ghost btn-small" onClick={() => go("datasets")}>Add dataset</button>
          <button className="btn btn-primary btn-small" onClick={() => go("request")}>New request</button>
        </div>
      </div>
      <StatCards items={[
        { value: s.d, label: "Datasets" },
        { value: s.o, label: "Requests sent" },
        { value: s.i, label: "Awaiting your consent", warn: s.i > 0 },
      ]} />
      <h3 className="section-title">Recent activity</h3>
      <div className="card flush">
        {recent.length === 0 && <Empty>Nothing yet.</Empty>}
        {recent.map((c) => (
          <div className="row clickable" key={c.id} onClick={() => openConnection(c.id)}>
            <div className="row-icon"><Icon name="link" /></div>
            <div className="row-main">
              <span className="row-title">{c._dir}</span>
              <span className="row-sub">{c.purpose}</span>
            </div>
            <StatusPill status={c.status} />
          </div>
        ))}
      </div>
    </>
  );
}

/* ---------------- Datasets ---------------- */
export function Datasets() {
  const [list, setList] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [source, setSource] = useState("file");
  const [file, setFile] = useState(null);
  const [text, setText] = useState("");
  const [preview, setPreview] = useState(null);
  const [formError, setFormError] = useState("");

  const load = () => apiCall("/api/datasets/mine").then(setList).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const reset = () => { setPreview(null); setFormError(""); };
  function fd(withName) {
    const f = new FormData();
    if (withName) f.append("name", name.trim());
    if (source === "file") { if (!file) throw new Error("Choose a file first."); f.append("file", file); }
    else { if (!text.trim()) throw new Error("Paste some data first."); f.append("text", text); }
    return f;
  }
  async function doPreview() {
    setFormError("");
    try { setPreview(await apiCall("/api/datasets/preview", { method: "POST", body: fd(false) })); }
    catch (e) { setPreview(null); setFormError(e.message); }
  }
  async function save(e) {
    e.preventDefault();
    setFormError("");
    try {
      await apiCall("/api/datasets/import", { method: "POST", body: fd(true) });
      setName(""); setFile(null); setText(""); reset(); setOpen(false); load();
    } catch (err) { setFormError(err.message); }
  }

  return (
    <>
      <PageHeader title="My Datasets" />
      <div className="card flush">
        {error && <ErrorText>{error}</ErrorText>}
        {!list && !error && <Empty>Loading…</Empty>}
        {list && list.length === 0 && <Empty>No datasets yet. Add one below to get started.</Empty>}
        {list && list.map((d) => (
          <div className="row" key={d.id} style={{ cursor: "default" }}>
            <div className="row-icon"><Icon name="db" /></div>
            <div className="row-main">
              <span className="row-title">{d.name}</span>
              <span className="row-sub">{d.field_names.join(", ")}</span>
            </div>
            <span className="row-sub">{d.record_count} sample record{d.record_count === 1 ? "" : "s"}</span>
          </div>
        ))}
      </div>

      <div className="card">
        <button className="disclosure" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span>Add a new dataset</span><span className={`chev ${open ? "open" : ""}`}>›</span>
        </button>
        {open && (
          <form onSubmit={save}>
            <label htmlFor="ds-name">Dataset name</label>
            <input id="ds-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. resident_records" required />
            <div className="segmented segmented-inline">
              <button type="button" className={source === "file" ? "active" : ""} onClick={() => { setSource("file"); reset(); }}>Upload a file</button>
              <button type="button" className={source === "paste" ? "active" : ""} onClick={() => { setSource("paste"); reset(); }}>Paste data</button>
            </div>
            {source === "file" ? (
              <>
                <label htmlFor="ds-file">CSV, TSV, JSON, JSONL or Excel (.xlsx) — up to 8 MB</label>
                <input id="ds-file" type="file" accept=".csv,.tsv,.txt,.json,.jsonl,.ndjson,.xlsx" onChange={(e) => { setFile(e.target.files[0] || null); reset(); }} />
              </>
            ) : (
              <>
                <label htmlFor="ds-text">Paste rows — from a spreadsheet, or as CSV / JSON</label>
                <textarea id="ds-text" rows={8} value={text} onChange={(e) => { setText(e.target.value); reset(); }} placeholder={"citizen_id,full_name,dob\nC1,Ada Lovelace,1815-12-10\nC2,Alan Turing,1912-06-23"} />
                
              </>
            )}
            <div className="row-actions">
              <button type="button" className="btn btn-ghost btn-small" onClick={doPreview}>Preview</button>
              <button className="btn btn-primary btn-small" disabled={!preview}>Save dataset</button>
            </div>
            <ErrorText>{formError}</ErrorText>
            {preview && (
              <div className="preview">
                <h4>{preview.record_count} record{preview.record_count === 1 ? "" : "s"} · {preview.columns.length} columns · detected as {preview.format.toUpperCase()}</h4>
                {preview.warnings.map((w, i) => <p className="form-error" key={i}>{w}</p>)}
                <div className="chips">
                  {preview.columns.map((c) => <span className="chip" key={c.name} title={`${c.distinct} distinct values, ${c.non_null} filled`}>{c.name} <em>{c.value_kind}</em></span>)}
                </div>
                <p className="note">First {preview.preview.length} rows:</p>
                <DataTable records={preview.preview} />
              </div>
            )}
          </form>
        )}
      </div>
    </>
  );
}

/* ---------------- Request a connection ---------------- */
export function RequestConnection({ session, openConnection }) {
  const [orgs, setOrgs] = useState([]);
  const [mine, setMine] = useState([]);
  const [theirs, setTheirs] = useState(null);
  const [f, setF] = useState({ org: "", mine: "", theirs: "", purpose: "" });
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([apiCall("/api/orgs"), apiCall("/api/datasets/mine")])
      .then(([o, d]) => { setOrgs(o.filter((x) => x.id !== session.id)); setMine(d); })
      .catch((e) => setError(e.message));
  }, [session.id]);

  async function pickOrg(id) {
    setF({ ...f, org: id, theirs: "" });
    setTheirs(null);
    if (!id) return;
    try { setTheirs(await apiCall(`/api/orgs/${id}/datasets`)); } catch { setTheirs([]); }
  }

  async function submit(e) {
    e.preventDefault();
    setError("");
    if (!f.org || !f.mine || !f.theirs) { setError("Please fill in every field."); return; }
    setBusy(true);
    try {
      const conn = await apiCall("/api/connections", { method: "POST", body: {
        counterparty_org_id: f.org, requester_dataset_id: f.mine, counterparty_dataset_id: f.theirs, purpose: f.purpose.trim(),
      } });
      setResult(conn);
      setF({ org: "", mine: "", theirs: "", purpose: "" });
      setTheirs(null);
    } catch (err) { setError(err.message); }
    setBusy(false);
  }

  const label = (d) => `${d.name} (${d.field_count ?? d.field_names.length} fields)`;
  const selMine = mine.find((d) => d.id === f.mine);
  const selTheirs = (theirs || []).find((d) => d.id === f.theirs);
  const SchemaBox = ({ title, ds }) => ds && (
    <div className="card" style={{ marginTop: "0.8rem", padding: "0.8rem 1rem" }}>
      <strong>{title}: {ds.name}</strong> <span className="faint">({ds.field_names.length} fields)</span>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem", marginTop: "0.5rem" }}>
        {ds.field_names.map((n) => <code key={n} style={{ background: "#eef0f2", borderRadius: 6, padding: "2px 8px", fontSize: "0.85rem" }}>{n}</code>)}
      </div>
    </div>
  );
  return (
    <>
      <PageHeader title="Request a Connection" />
      <form className="card" onSubmit={submit}>
        <label htmlFor="req-org">Organization to connect with</label>
        <select id="req-org" value={f.org} onChange={(e) => pickOrg(e.target.value)} required>
          <option value="">{orgs.length ? "Select…" : "No other organizations registered yet"}</option>
          {orgs.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
        </select>
        <div className="two-col">
          <div>
            <label htmlFor="req-mine">Your dataset <span className="faint">(defines the shape you want back)</span></label>
            <select id="req-mine" value={f.mine} onChange={(e) => setF({ ...f, mine: e.target.value })} required>
              <option value="">{mine.length ? "Select…" : "Upload a dataset first"}</option>
              {mine.map((d) => <option key={d.id} value={d.id}>{label(d)}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="req-theirs">Their dataset <span className="faint">(the data you’re requesting)</span></label>
            <select id="req-theirs" value={f.theirs} onChange={(e) => setF({ ...f, theirs: e.target.value })} disabled={!theirs} required>
              <option value="">{!f.org ? "Select an organization first…" : theirs === null ? "Loading…" : theirs.length ? "Select…" : "That organization hasn’t uploaded a dataset yet"}</option>
              {(theirs || []).map((d) => <option key={d.id} value={d.id}>{label(d)}</option>)}
            </select>
          </div>
        </div>
        <SchemaBox title="Their schema" ds={selTheirs} />
        <SchemaBox title="Your schema" ds={selMine} />
        <label htmlFor="req-purpose">Purpose of this connection</label>
        <textarea id="req-purpose" rows={3} value={f.purpose} onChange={(e) => setF({ ...f, purpose: e.target.value })} placeholder="e.g. Enrich resident records with taxpayer identifiers to reduce duplicate registrations." required />
        <div className="row-actions">
          <button className="btn btn-primary" disabled={busy}>{busy ? "Scoring fit…" : "Send connection request"}</button>
        </div>
        <ErrorText>{error}</ErrorText>
      </form>

      {result && (
        <div className="card success">
          <h3>Request sent</h3>
          <p>Sent to <strong>{result.counterparty_org_name}</strong> <StatusPill status="PENDING_ADMIN_REVIEW" /></p>
          <SummaryCards sum={result.scoring_summary} />
          <ReportTable conn={result} />
          <div className="row-actions"><button className="btn btn-ghost btn-small" onClick={() => openConnection(result.id)}>View this request</button></div>
        </div>
      )}
    </>
  );
}

/* ---------------- Connection lists ---------------- */
function ConnectionList({ path, empty, label, onOpen }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    apiCall(path).then((c) => setRows([...c].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)))).catch((e) => setError(e.message));
  }, [path]);
  if (error) return <ErrorText>{error}</ErrorText>;
  if (!rows) return <div className="card flush"><Empty>Loading…</Empty></div>;
  if (!rows.length) return <div className="card flush"><Empty>{empty}</Empty></div>;
  return (
    <div className="card flush">
      {rows.map((c) => (
        <div className="row clickable" key={c.id} onClick={() => onOpen(c.id)}>
          <div className="row-icon"><Icon name="link" /></div>
          <div className="row-main">
            <span className="row-title">{label(c)}</span>
            <span className="row-sub">{c.purpose.length > 90 ? c.purpose.slice(0, 90) + "…" : c.purpose}</span>
          </div>
          <div className="row-meta">
            {c.similarity_score !== null && <span className="row-sub">{Math.round(c.similarity_score * 100)}% overlap</span>}
            <StatusPill status={c.status} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function Mine({ onOpen }) {
  return (<>
    <PageHeader title="My Requests" />
    <ConnectionList path="/api/connections/mine" empty="You haven’t requested any connections yet." label={(c) => `To ${c.counterparty_org_name}`} onOpen={onOpen} />
  </>);
}
export function Incoming({ onOpen }) {
  return (<>
    <PageHeader title="Incoming Requests" />
    <ConnectionList path="/api/connections/incoming" empty="No one has requested a connection with you yet." label={(c) => `From ${c.requester_org_name}`} onOpen={onOpen} />
  </>);
}
