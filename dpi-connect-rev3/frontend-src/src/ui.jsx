// Small shared presentational pieces
import React from "react";

const STATUS = {
  PENDING_ADMIN_REVIEW: ["review", "Awaiting admin review"],
  REJECTED_BY_ADMIN: ["rejected", "Rejected by admin"],
  PENDING_CONSENT: ["pending", "Pending consent"],
  APPROVED: ["pending", "Approved"],
  ACTIVE: ["active", "Active"],
  REJECTED: ["rejected", "Declined"],
};

export function StatusPill({ status }) {
  const [cls, label] = STATUS[status] || ["pending", status];
  return (
    <span className={`pill pill-${cls}`}>
      <i />
      {label}
    </span>
  );
}

export const pct = (v) => (v === null || v === undefined ? "—" : Math.round(v * 100) + "%");
export const fmtDate = (d) => {
  const str = typeof d === "string" && !/(Z|[+-]\d\d:?\d\d)$/.test(d) ? d + "Z" : d; // API times are UTC
  return new Date(str).toLocaleString();
};

export function Icon({ name, size = 18 }) {
  const p = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" };
  const paths = {
    home: <><path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" /></>,
    db: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>,
    link: <><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" /><path d="M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.7l1-1" /></>,
    send: <><path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4 20-7z" /></>,
    inbox: <><path d="M22 12h-6l-2 3h-4l-2-3H2" /><path d="M5.5 5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z" /></>,
    shield: <><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><path d="M9 12l2 2 4-4" /></>,
    out: <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></>,
    back: <><path d="M19 12H5" /><path d="M12 19l-7-7 7-7" /></>,
    check: <path d="M5 12l5 5L20 7" />,
    key: <><circle cx="8" cy="15" r="4" /><path d="M10.8 12.2L20 3" /><path d="M16 7l3 3" /></>,
  };
  return <svg {...p}>{paths[name]}</svg>;
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>;
}

export function ErrorText({ children }) {
  return children ? <p className="form-error">{children}</p> : null;
}

export function Confidence({ c }) {
  const p = Math.round(c.confidence * 100);
  const cls = c.needs_review ? (c.level === "low" ? "low" : "review") : "high";
  return (
    <div
      className={`conf conf-${cls}`}
      title={c.kind.startsWith("only") ? "certainty that no counterpart exists" : "confidence these fields are the same concept"}
    >
      <div className="conf-track"><div className="conf-fill" style={{ width: `${p}%` }} /></div>
      <span>{p}%</span>
    </div>
  );
}

export function StatCards({ items }) {
  return (
    <div className="stats">
      {items.map((s) => (
        <div className={`stat ${s.warn ? "stat-warn" : ""}`} key={s.label}>
          <span className="stat-value">{s.value}</span>
          <span className="stat-label">{s.label}</span>
        </div>
      ))}
    </div>
  );
}
