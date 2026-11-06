import React, { useState } from "react";
import { apiCall, whoami, setSession } from "../api.js";
import { Icon, ErrorText } from "../ui.jsx";

export default function Auth({ onEnter }) {
  const [tab, setTab] = useState("login");
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [newKey, setNewKey] = useState(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  async function login(e) {
    e.preventDefault();
    setError(""); setBusy(true);
    try {
      const d = await whoami(key.trim());
      setSession({ id: d.id, name: d.name, api_key: key.trim(), role: d.role });
      onEnter();
    } catch (err) { setError(err.message); }
    setBusy(false);
  }

  async function register(e) {
    e.preventDefault();
    setError(""); setBusy(true);
    try {
      const d = await apiCall("/api/orgs/register", { method: "POST", body: { name: name.trim(), contact_email: email.trim() }, auth: false });
      setNewKey({ key: d.api_key, session: { id: d.org_id, name: d.name, api_key: d.api_key, role: "org" } });
    } catch (err) { setError(err.message); }
    setBusy(false);
  }

  return (
    <div className="auth">
      <section className="auth-hero">
        <div className="brand brand-light">
          <span className="brand-mark">D</span>
          <span className="brand-name">DPI Connect</span>
        </div>
        <div className="auth-hero-body">
          <p className="eyebrow">Digital Public Infrastructure</p>
          <h1>Connect data between organizations, with consent.</h1>
          <ul className="auth-points">
            <li><Icon name="check" /> Automatic schema inference and field mapping</li>
            <li><Icon name="check" /> Transparent confidence scores for every column</li>
            <li><Icon name="check" /> Administrator review and a signed audit trail</li>
          </ul>
        </div>
        <p className="auth-foot">Secure data exchange between organizations</p>
      </section>

      <section className="auth-panel">
        <div className="auth-card">
          <h2>{tab === "login" ? "Welcome back" : "Register your organization"}</h2>

          <div className="segmented">
            <button className={tab === "login" ? "active" : ""} onClick={() => { setTab("login"); setError(""); setNewKey(null); }}>Sign in</button>
            <button className={tab === "register" ? "active" : ""} onClick={() => { setTab("register"); setError(""); }}>Register</button>
          </div>

          {tab === "login" && (
            <form onSubmit={login}>
              <label htmlFor="login-key">API key</label>
              <input id="login-key" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Paste the key you saved at registration" required autoComplete="off" />
              <button className="btn btn-primary btn-block" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
              <ErrorText>{error}</ErrorText>
            </form>
          )}

          {tab === "register" && !newKey && (
            <form onSubmit={register}>
              <label htmlFor="reg-name">Organization name</label>
              <input id="reg-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Land Registry Authority" required />
              <label htmlFor="reg-email">Contact email</label>
              <input id="reg-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="data-office@example.gov" required />
              <button className="btn btn-primary btn-block" disabled={busy}>{busy ? "Registering…" : "Register organization"}</button>
              <ErrorText>{error}</ErrorText>
            </form>
          )}

          {tab === "register" && newKey && (
            <div className="key-banner">
              <div className="key-title"><Icon name="key" /> Save this API key now — it won’t be shown again.</div>
              <code>{newKey.key}</code>
              <div className="row-actions">
                <button className="btn btn-ghost btn-small" onClick={() => { navigator.clipboard?.writeText(newKey.key); setCopied(true); }}>{copied ? "Copied" : "Copy key"}</button>
                <button className="btn btn-primary btn-small" onClick={() => { setSession(newKey.session); onEnter(); }}>Continue to DPI Connect</button>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
