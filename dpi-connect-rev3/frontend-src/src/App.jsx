import React, { useState } from "react";
import { getSession, clearSession } from "./api.js";
import { Icon } from "./ui.jsx";
import Auth from "./components/Auth.jsx";
import { Overview, Datasets, RequestConnection, Mine, Incoming } from "./components/Views.jsx";
import { ConnectionDetail, AdminQueue, AdminReview } from "./components/Detail.jsx";

const ORG_NAV = [
  ["overview", "Overview", "home"],
  ["datasets", "My Datasets", "db"],
  ["request", "Request a Connection", "send"],
  ["mine", "My Requests", "link"],
  ["incoming", "Incoming Requests", "inbox"],
];
const ADMIN_NAV = [["admin-queue", "Review queue", "shield"]];

export default function App() {
  const [session, setSession] = useState(getSession());
  const [view, setView] = useState({ name: null });

  if (!session) return <Auth onEnter={() => { const s = getSession(); setSession(s); setView({ name: s.role === "admin" ? "admin-queue" : "overview" }); }} />;

  const admin = session.role === "admin";
  const current = view.name || (admin ? "admin-queue" : "overview");
  const nav = admin ? ADMIN_NAV : ORG_NAV;
  const go = (name, id) => setView({ name, id });
  const openConn = (id) => go(admin ? "admin-review" : "detail", id);
  const activeNav = current === "detail" ? "mine" : current === "admin-review" ? "admin-queue" : current;
  const signOut = () => { clearSession(); setSession(null); setView({ name: null }); };

  let page;
  switch (current) {
    case "overview": page = <Overview session={session} go={go} openConnection={openConn} />; break;
    case "datasets": page = <Datasets />; break;
    case "request": page = <RequestConnection session={session} openConnection={openConn} />; break;
    case "mine": page = <Mine onOpen={openConn} />; break;
    case "incoming": page = <Incoming onOpen={openConn} />; break;
    case "admin-queue": page = <AdminQueue onOpen={openConn} />; break;
    case "admin-review": page = <AdminReview id={view.id} onBack={() => go("admin-queue")} />; break;
    case "detail": page = <ConnectionDetail id={view.id} session={session} onBack={() => go("mine")} />; break;
    default: page = null;
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">D</span><span className="brand-name">DPI Connect</span></div>
        <nav className="nav">
          {nav.map(([name, label, icon]) => (
            <button key={name} className={`nav-link ${activeNav === name ? "active" : ""}`} onClick={() => go(name)}>
              <Icon name={icon} /> {label}
            </button>
          ))}
        </nav>
        <div className="account">
          <div className="avatar">{(admin ? "A" : session.name || "?").charAt(0).toUpperCase()}</div>
          <div className="account-text">
            <span className="account-name">{admin ? "Platform admin" : session.name}</span>
            <span className="account-role">{admin ? "Administrator" : "Organization"}</span>
          </div>
          <button className="icon-btn" onClick={signOut} title="Sign out" aria-label="Sign out"><Icon name="out" /></button>
        </div>
      </aside>
      <main className="main" key={current + (view.id || "")}>
        <div className="main-inner">{page}</div>
      </main>
    </div>
  );
}
