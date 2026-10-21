"use client";

import { type Dispatch, type FormEvent, type RefObject, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";

type User = { user_id: string; email: string };
type Activity = { id?: number; at?: string; node?: string; status?: string; message?: string; error?: string };
type Run = {
  run_id: string; request_id: string; status: string;
  request: { industry: string; region: string; target_leads: number; qualification_criteria?: string[] };
  approval: null | { proposal_hash: string; candidate_count: number; changes: { candidate_id: string; company_name: string; domain: string; source_urls: string[] }[] };
  candidates: { candidate_id: string; company_name: string; legal_name?: string | null; domain: string; website_url: string; decision: string; confidence: number; rationale: string; website_status: string; import_status: string; source_evidence: { evidence_id: string; source_url: string; source_type: string; excerpt: string; retrieved_at: string }[]; verification_notes: string[] }[];
  report: null | { requested_target: number; target_reached: boolean; target_shortfall: number; imported: number; candidates: Run["candidates"]; notes: string[] };
  error: string | null; created_at: string;
};
type HistoryItem = Pick<Run, "run_id" | "request_id" | "status" | "request" | "report" | "error" | "created_at">;
const API = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
const csrf = () => document.cookie.split("; ").map((item) => item.trim()).find((item) => item.startsWith("lead_csrf="))?.split("=")[1] || "";
async function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (init.method && init.method !== "GET" && path !== "/auth/login" && path !== "/auth/register" && path !== "/auth/forgot-password" && path !== "/auth/reset-password" && path !== "/auth/verify-email" && path !== "/auth/resend-verification") headers.set("X-CSRF-Token", decodeURIComponent(csrf()));
  const response = await fetch(`${API}${path}`, { ...init, headers, credentials: "include", cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.message || `Request failed (${response.status})`);
  return data;
}
const label = (value: string) => value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
function Brand() { return <div className="brand"><span className="brand-symbol">L</span><span>Leadspace<span className="brand-dot">.</span></span></div>; }
function Status({ value }: { value: string }) { return <span className={`status status-${value.replaceAll("_", "-")}`}><i />{label(value)}</span>; }

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authMode, setAuthMode] = useState<"login" | "register" | "forgot" | "reset">("login");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [actionToken, setActionToken] = useState("");
  const [authMessage, setAuthMessage] = useState("");
  const [authError, setAuthError] = useState("");
  const [view, setView] = useState<"overview" | "new" | "history">("overview");
  const [target, setTarget] = useState(50);
  const [industry, setIndustry] = useState("AI software");
  const [region, setRegion] = useState("Bengaluru");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");
  const [criteria, setCriteria] = useState("B2B AI software company");
  const [run, setRun] = useState<Run | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPage, setHistoryPage] = useState(0);
  const [historyFilter, setHistoryFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [events, setEvents] = useState<Activity[]>([]);
  const [streamState, setStreamState] = useState<"connecting" | "live" | "reconnecting" | "closed">("closed");
  const [logPaused, setLogPaused] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const token = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("verify-email_token") : null;
  const resetToken = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("reset-password_token") : null;

  const refreshRun = useCallback(async (runId: string) => {
    const result = await api(`/runs/${encodeURIComponent(runId)}`) as Run;
    setRun(result);
    return result;
  }, []);

  const loadHistory = useCallback(async (page = 0, filter = historyFilter) => {
    const status = filter === "all" ? "" : `&status_filter=${encodeURIComponent(filter)}`;
    const result = await api(`/runs?limit=10&offset=${page * 10}${status}`);
    setHistory(result.results || []); setHistoryTotal(result.total || 0); setHistoryPage(page);
  }, [historyFilter]);

  useEffect(() => {
    api("/config").then((data) => setTarget(data.number_of_leads || 50)).catch(() => undefined);
    api("/auth/me").then((data) => setUser(data)).catch(() => setUser(null)).finally(() => setAuthReady(true));
    if (token) {
      window.history.replaceState(null, "", location.pathname);
      api("/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) }).then((data) => { setAuthMessage(data.message); setAuthMode("login"); }).catch((reason) => setAuthError(reason.message));
    }
    if (resetToken) { setActionToken(resetToken); setAuthMode("reset"); setAuthMessage("Choose a new password to finish resetting your account."); window.history.replaceState(null, "", location.pathname); }
  }, []);

  useEffect(() => { if (user) void loadHistory(0, "all"); }, [user]);
  useEffect(() => {
    if (!user) return;
    const runId = new URLSearchParams(window.location.search).get("run_id");
    if (runId) { setView("history"); void refreshRun(runId).catch((reason) => setError(reason.message)); }
  }, [user, refreshRun]);
  useEffect(() => {
    if (!run) { setStreamState("closed"); return; }
    let cancelled = false;
    let source: EventSource | null = null;
    setStreamState(["queued", "running"].includes(run.status) ? "connecting" : "closed");
    void api(`/runs/${encodeURIComponent(run.run_id)}/events/history`).then((data) => {
      if (cancelled) return;
      const saved = (data.results || []).map((row: { id: number; event: Activity }) => ({ ...row.event, id: row.id }));
      setEvents(saved);
      if (!["queued", "running"].includes(run.status)) return;
      source = new EventSource(`${API}/runs/${encodeURIComponent(run.run_id)}/events?after_id=${data.last_event_id || 0}`, { withCredentials: true });
      source.onopen = () => setStreamState("live");
      source.onerror = () => setStreamState("reconnecting");
      source.addEventListener("progress", (raw) => {
        const event = JSON.parse((raw as MessageEvent).data) as Activity;
        setEvents((current) => [...current.slice(-199), { ...event, id: Number((raw as MessageEvent).lastEventId) }]);
        void refreshRun(run.run_id).catch(() => undefined);
      });
      source.addEventListener("end", () => { source?.close(); setStreamState("closed"); void refreshRun(run.run_id).catch(() => undefined); });
    }).catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load run activity."); });
    const timer = ["queued", "running"].includes(run.status) ? window.setInterval(() => void refreshRun(run.run_id).catch(() => undefined), 4000) : undefined;
    return () => { cancelled = true; source?.close(); if (timer) window.clearInterval(timer); };
  }, [run?.run_id, run?.status, refreshRun]);
  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) return;
    setElapsed(Math.max(0, Math.floor((Date.now() - new Date(run.created_at).getTime()) / 1000)));
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.status]);
  useEffect(() => {
    if (!logPaused && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [events, logPaused]);

  const candidates = run?.report?.candidates || run?.candidates || [];
  const filteredHistory = useMemo(() => history.filter((item) => `${item.request.industry} ${item.request.region} ${item.status}`.toLowerCase().includes(search.toLowerCase())), [history, search]);
  const latestEvent = events.at(-1);
  const stageByNode: Record<string, number> = { queued: 0, validate_input: 0, discover_candidates: 1, research_candidates: 2, qualify_candidates: 3, deduplicate: 3, verify_websites: 3, prepare_proposal: 4, approval_gate: 4, execute_import: 5, verify_import: 5, build_report: 6, complete: 6 };
  const activeStage = run?.status === "completed" ? 6 : run?.status === "awaiting_approval" ? 4 : stageByNode[latestEvent?.node || run?.status || "queued"] ?? 0;
  const statusCounts = useMemo(() => ({ completed: history.filter((item) => item.status === "completed").length, active: history.filter((item) => ["queued", "running"].includes(item.status)).length, review: history.filter((item) => item.status === "awaiting_approval").length }), [history]);

  async function submitAuth(event: FormEvent) {
    event.preventDefault(); setAuthError(""); setAuthMessage(""); setBusy(true);
    try {
      if (authMode === "register") {
        const result = await api("/auth/register", { method: "POST", body: JSON.stringify({ email: authEmail, password: authPassword }) });
        setUser(result.user); setAuthMode("login");
      } else if (authMode === "forgot") {
        const result = await api("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email: authEmail }) }); setAuthMessage(result.message);
      } else if (authMode === "reset") {
        const result = await api("/auth/reset-password", { method: "POST", body: JSON.stringify({ token: actionToken, password: authPassword }) }); setUser(null); setAuthMessage(result.message); setAuthMode("login"); setActionToken("");
      } else {
        const result = await api("/auth/login", { method: "POST", body: JSON.stringify({ email: authEmail, password: authPassword }) }); setUser(result.user);
      }
    } catch (reason) { setAuthError(reason instanceof Error ? reason.message : "Could not complete this request."); }
    finally { setBusy(false); }
  }

  async function signOut() {
    try { await api("/auth/logout", { method: "POST", body: "{}" }); } finally { setUser(null); setRun(null); }
  }
  async function startResearch(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setEvents([]); setSelected([]); setElapsed(0);
    try {
      const result = await api("/runs", { method: "POST", body: JSON.stringify({ request_id: `lead-run-${crypto.randomUUID()}`, industry, region, company_size: { min_employees: minimum ? Number(minimum) : null, max_employees: maximum ? Number(maximum) : null }, qualification_criteria: criteria.split("\n").map((item) => item.trim()).filter(Boolean), target_leads: target }) });
      window.history.replaceState(null, "", `?run_id=${encodeURIComponent(result.run_id)}`);
      setView("history"); await refreshRun(result.run_id); await loadHistory(0, "all");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not start research."); }
    finally { setBusy(false); }
  }
  async function submitApproval(approved: boolean) {
    if (!run?.approval) return; setBusy(true); setError("");
    try {
      await api(`/runs/${run.run_id}/approval`, { method: "POST", body: JSON.stringify({ approved, proposal_hash: run.approval.proposal_hash, approved_candidate_ids: approved ? selected : [], comment: approved ? "Reviewed the lead proposal." : "Rejected the proposal." }) });
      await refreshRun(run.run_id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Could not submit review."); }
    finally { setBusy(false); }
  }
  async function openRun(runId: string) {
    setError(""); setEvents([]); setView("history"); window.history.replaceState(null, "", `?run_id=${encodeURIComponent(runId)}`);
    try { await refreshRun(runId); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load the run."); }
  }

  if (!authReady) return <main className="auth-screen"><div className="loading-mark"><Brand /><span className="spinner" /></div></main>;
  if (!user || resetToken || authMode === "reset") return <main className="auth-screen"><div className="auth-brand"><Brand /></div><section className="auth-card"><div className="auth-icon">✦</div><p className="eyebrow">YOUR RESEARCH WORKSPACE</p><h1>{authMode === "register" ? "Create your account" : authMode === "forgot" ? "Reset your password" : authMode === "reset" ? "Choose a new password" : "Welcome back"}</h1><p className="auth-sub">{authMode === "register" ? "Save research runs and keep every decision close." : authMode === "forgot" ? "We’ll send a secure reset link if the account exists." : authMode === "reset" ? authMessage : "Sign in to continue your company research."}</p>
    {authMessage && authMode !== "reset" && <div className="notice">{authMessage}</div>}{authError && <div className="error-box" role="alert">{authError}</div>}
    <form onSubmit={submitAuth}><label>Email address<input autoComplete="email" type="email" required value={authEmail} onChange={(e) => setAuthEmail(e.target.value)} placeholder="you@company.com" /></label>{authMode !== "forgot" && <label>Password<input autoComplete={authMode === "login" ? "current-password" : "new-password"} type="password" minLength={10} required value={authPassword} onChange={(e) => setAuthPassword(e.target.value)} placeholder="At least 10 characters" /></label>}<button className="button primary auth-submit" disabled={busy}>{busy ? "Please wait…" : authMode === "register" ? "Create account" : authMode === "forgot" ? "Send reset link" : authMode === "reset" ? "Update password" : "Sign in"}<span>→</span></button></form>
    <div className="auth-links">{authMode === "login" ? <><button onClick={() => setAuthMode("forgot")}>Forgot password?</button><span>New here? <button onClick={() => setAuthMode("register")}>Create account</button></span></> : <button onClick={() => { setAuthMode("login"); setAuthMessage(""); setAuthError(""); }}>← Back to sign in</button>}</div>
    <div className="auth-security"><span>⌑</span> Private by design · your runs belong to you</div>
  </section><div className="auth-footer">LEADSPACE <span>·</span> RESEARCH WITH CLARITY</div></main>;

  return <div className="app-frame">
    <aside className="sidebar"><div className="sidebar-top"><Brand /><div className="workspace-pill"><span className="avatar">{user.email.slice(0, 1).toUpperCase()}</span><span><b>My workspace</b><small>Personal account</small></span><span className="chevron">⌄</span></div><p className="nav-caption">WORKSPACE</p><nav className="side-nav"><button className={view === "overview" ? "selected" : ""} onClick={() => { setView("overview"); setRun(null); }}><span>⌂</span> Overview</button><button className={view === "new" ? "selected" : ""} onClick={() => { setView("new"); setRun(null); }}><span>＋</span> New research</button><button className={view === "history" ? "selected" : ""} onClick={() => { setView("history"); setRun(null); void loadHistory(0); }}><span>▤</span> Research history <em>{historyTotal}</em></button></nav></div><div className="sidebar-bottom"><div className="help-card"><span className="help-spark">✧</span><b>Evidence first</b><p>Every decision stays linked to its source.</p></div><button className="profile-button" onClick={() => void signOut()}><span className="avatar avatar-small">{user.email.slice(0, 1).toUpperCase()}</span><span className="profile-text"><b>{user.email}</b><small>Personal account</small></span><span className="signout">↪</span></button></div></aside>
    <main className="workspace"><header className="topbar"><div className="breadcrumbs"><span>Workspace</span><b>/</b><strong>{run ? "Run details" : view === "overview" ? "Overview" : view === "new" ? "New research" : "Research history"}</strong></div><div className="top-actions"><span className="secure-label"><i /> Secure workspace</span><button className="avatar top-avatar" aria-label="Sign out" onClick={() => void signOut()}>{user.email.slice(0, 1).toUpperCase()}</button></div></header>
    <div className="content">
      {view === "overview" && !run && <>
        <div className="welcome-row"><div><p className="eyebrow">YOUR RESEARCH DESK</p><h1>Good research starts<br /><span>with a clear question.</span></h1><p className="lede">Find, verify, and review the companies that fit your next opportunity.</p></div><button className="button primary" onClick={() => { setView("new"); setRun(null); }}>Start new research <span>↗</span></button></div>
        <div className="stats-row"><div className="metric-card"><span className="metric-icon lilac">▤</span><div><small>Total research runs</small><strong>{historyTotal}</strong></div><span className="metric-note">All time</span></div><div className="metric-card"><span className="metric-icon mint">◷</span><div><small>In progress</small><strong>{statusCounts.active}</strong></div><span className="metric-note">Recent 10 runs</span></div><div className="metric-card"><span className="metric-icon amber">✓</span><div><small>Ready for review</small><strong>{statusCounts.review}</strong></div><span className="metric-note">Recent 10 runs</span></div><div className="metric-card"><span className="metric-icon blue">✦</span><div><small>Completed</small><strong>{statusCounts.completed}</strong></div><span className="metric-note">Recent 10 runs</span></div></div>
        <section className="section-card"><div className="section-heading"><div><h2>Recent research</h2><p>Your latest lead searches and their current status.</p></div><button className="text-button" onClick={() => { setView("history"); void loadHistory(0); }}>View all runs <span>→</span></button></div>{history.length ? <HistoryTable items={history.slice(0, 5)} openRun={openRun} /> : <EmptyHistory onStart={() => setView("new")} />}</section>
        <div className="bottom-note"><span>✧</span><div><b>Every result has a reason.</b><p>Review source evidence and approve records before they reach your CRM.</p></div></div>
      </>}

      {view === "new" && !run && <><div className="page-heading"><p className="eyebrow">RESEARCH SETUP</p><h1>Build your next lead list</h1><p>Tell us who you’re looking for. We’ll research public sources and prepare a reviewable proposal.</p></div><div className="request-layout"><form className="section-card request-card" onSubmit={startResearch}><div className="card-heading"><span className="step-bubble">01</span><div><h2>Define your market</h2><p>Start broad, then add the criteria that matter.</p></div></div><div className="form-grid"><label>Industry or category<input value={industry} onChange={(e) => setIndustry(e.target.value)} minLength={2} required placeholder="e.g. AI software" /></label><label>City or region<input value={region} onChange={(e) => setRegion(e.target.value)} minLength={2} required placeholder="e.g. Bengaluru" /></label><label>Minimum employees <small>Optional</small><input type="number" min="1" value={minimum} onChange={(e) => setMinimum(e.target.value)} placeholder="No minimum" /></label><label>Maximum employees <small>Optional</small><input type="number" min="1" value={maximum} onChange={(e) => setMaximum(e.target.value)} placeholder="No maximum" /></label></div><label className="criteria-field">Qualification criteria <small>One per line</small><textarea rows={4} value={criteria} onChange={(e) => setCriteria(e.target.value)} required placeholder="B2B software company&#10;Has a public product website" /></label>{error && <div className="error-box" role="alert">{error}</div>}<div className="request-footer"><span>Target list <b>{target} companies</b> <small>set by workspace configuration</small></span><button className="button primary" disabled={busy || !industry.trim() || !region.trim() || !criteria.trim()}>{busy ? "Starting research…" : "Start research run"}<span>↗</span></button></div></form><aside className="section-card guide-card"><span className="guide-icon">✧</span><h3>What happens next?</h3><ol><li><b>Discover</b><span>Gather potential companies from public sources.</span></li><li><b>Research</b><span>Check websites and match your criteria.</span></li><li><b>Review</b><span>See the evidence before any CRM import.</span></li></ol><div className="guide-foot">No CRM records are created without your approval.</div></aside></div></>}

      {view === "history" && !run && <><div className="page-heading page-heading-row"><div><p className="eyebrow">YOUR WORKSPACE</p><h1>Research history</h1><p>Pick up where you left off or review a finished lead list.</p></div><button className="button primary" onClick={() => setView("new")}>＋ New research</button></div><section className="section-card history-card"><div className="history-controls"><div className="search-field"><span>⌕</span><input aria-label="Search history" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by industry or region…" /></div><select aria-label="Filter by status" value={historyFilter} onChange={(e) => { setHistoryFilter(e.target.value); void loadHistory(0, e.target.value); }}><option value="all">All statuses</option><option value="running">In progress</option><option value="awaiting_approval">Needs review</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="queued">Queued</option></select></div>{filteredHistory.length ? <HistoryTable items={filteredHistory} openRun={openRun} /> : <EmptyHistory onStart={() => setView("new")} search={!!search || historyFilter !== "all"} />}<div className="pagination"><span>Showing {historyTotal ? historyPage * 10 + 1 : 0}–{Math.min((historyPage + 1) * 10, historyTotal)} of {historyTotal}</span><div><button disabled={historyPage === 0} onClick={() => void loadHistory(historyPage - 1)}>← Previous</button><button disabled={(historyPage + 1) * 10 >= historyTotal} onClick={() => void loadHistory(historyPage + 1)}>Next →</button></div></div></section></>}

      {run && <RunDetail run={run} events={events} latestEvent={latestEvent} activeStage={activeStage} streamState={streamState} elapsed={elapsed} logPaused={logPaused} setLogPaused={setLogPaused} logRef={logRef} selected={selected} setSelected={setSelected} busy={busy} error={error} submitApproval={submitApproval} onBack={() => { setRun(null); setView("history"); void loadHistory(0); window.history.replaceState(null, "", location.pathname); }} />}
    </div></main>
  </div>;
}

function HistoryTable({ items, openRun }: { items: HistoryItem[]; openRun: (id: string) => void }) {
  return <div className="table-scroll"><table className="history-table"><thead><tr><th>RESEARCH REQUEST</th><th>CREATED</th><th>LEADS</th><th>STATUS</th><th /></tr></thead><tbody>{items.map((item) => <tr key={item.run_id} onClick={() => openRun(item.run_id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && openRun(item.run_id)}><td><span className="table-title">{item.request.industry}</span><small>{item.request.region} · {item.run_id.slice(0, 8)}</small></td><td>{new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</td><td>{item.report ? item.report.candidates.length : item.request.target_leads}</td><td><Status value={item.status} /></td><td><button className="row-open" aria-label="Open run">↗</button></td></tr>)}</tbody></table></div>;
}
function EmptyHistory({ onStart, search = false }: { onStart: () => void; search?: boolean }) { return <div className="empty-state"><div className="empty-illustration">⌕</div><h3>{search ? "No matching research" : "Your research starts here"}</h3><p>{search ? "Try a different search or status filter." : "Create a lead search and it will be saved here for next time."}</p>{!search && <button className="button secondary" onClick={onStart}>Create your first run <span>→</span></button>}</div>; }

function RunDetail({ run, events, latestEvent, activeStage, streamState, elapsed, logPaused, setLogPaused, logRef, selected, setSelected, busy, error, submitApproval, onBack }: { run: Run; events: Activity[]; latestEvent?: Activity; activeStage: number; streamState: string; elapsed: number; logPaused: boolean; setLogPaused: (v: boolean) => void; logRef: RefObject<HTMLDivElement | null>; selected: string[]; setSelected: Dispatch<SetStateAction<string[]>>; busy: boolean; error: string; submitApproval: (approved: boolean) => void; onBack: () => void }) {
  const candidates = run.report?.candidates || run.candidates;
  const approval = run.approval?.changes || [];
  const progress = run.status === "completed" ? 100 : run.status === "awaiting_approval" ? 82 : Math.min(86, Math.max(8, Math.round(((activeStage + 1) / 6) * 86)));
  const toggle = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  return <><div className="run-head"><div><button className="back-link" onClick={onBack}>← Research history</button><p className="eyebrow">RUN {run.run_id.slice(0, 8).toUpperCase()}</p><h1>{run.request.industry} <span>in {run.request.region}</span></h1><p className="run-created">Started {new Date(run.created_at).toLocaleString()}</p></div><Status value={run.status} /></div>
    {error && <div className="error-box" role="alert">{error}</div>}
    <section className="section-card progress-card"><div className="progress-top"><div><p className="eyebrow">RESEARCH PROGRESS</p><h2>{run.status === "awaiting_approval" ? "Your review is ready" : run.status === "completed" ? "Research complete" : run.status === "failed" ? "Run needs attention" : latestEvent?.message || "Preparing your research…"}</h2><p>{run.status === "awaiting_approval" ? "Review the proposed companies and select the records to import." : run.status === "completed" ? "Your results and source evidence are ready to review." : run.error || `${run.candidates.length} candidate records found · target ${run.request.target_leads} leads`}</p></div><div className="run-timer"><span className={streamState === "live" ? "pulse-dot" : ""} />{streamState === "live" ? "Live" : streamState === "reconnecting" ? "Reconnecting" : run.status === "completed" ? "Finished" : run.status === "awaiting_approval" ? "Awaiting review" : "Connecting"}<b>{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}</b></div></div><div className="progress-rail"><span style={{ width: `${progress}%` }} /></div><div className="stage-rail">{["Queue", "Discover", "Research", "Qualify", "Review", "Import"].map((stage, i) => <div key={stage} className={i < activeStage ? "stage done" : i === activeStage ? "stage active" : "stage"}><span>{i < activeStage ? "✓" : String(i + 1).padStart(2, "0")}</span><b>{stage}</b></div>)}</div></section>
    {(run.status === "queued" || run.status === "running" || events.length > 0) && <section className="section-card activity-card"><div className="section-heading"><div><h2>Worker activity</h2><p>Live updates from the research workflow.</p></div><div className="activity-tools"><span className={`connection ${streamState}`}>{streamState === "live" ? "● Live stream" : streamState === "reconnecting" ? "↻ Reconnecting" : "○ Stream paused"}</span><button className="text-button" onClick={() => setLogPaused(!logPaused)}>{logPaused ? "Resume auto-scroll" : "Pause auto-scroll"}</button></div></div><div className="activity-log" ref={logRef} aria-live="polite">{events.length ? events.map((event, index) => <div className="log-entry" key={`${event.id || index}-${index}`}><span className={`log-indicator ${event.status || ""}`} /><time>{event.at ? new Date(event.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Now"}</time><b>{label(event.node || "worker")}</b><span>{event.message || event.error || "Working…"}</span></div>) : <div className="log-wait"><span className="spinner" />Connecting to worker updates…</div>}</div><div className="log-footer"><span>{events.length} updates · newest at bottom</span><span>{streamState === "reconnecting" ? "Trying to reconnect automatically…" : "Progress is saved with this run."}</span></div></section>}
    {run.report && <section className={`result-banner ${run.report.target_reached ? "good" : "warning"}`}><span>{run.report.target_reached ? "✓" : "!"}</span><div><b>{run.report.target_reached ? `Target reached — ${run.report.requested_target} qualified companies` : `${Math.max(0, run.report.requested_target - run.report.target_shortfall)} of ${run.report.requested_target} companies qualified`}</b><p>{run.report.imported} records imported · {run.report.target_shortfall} target shortfall</p></div></section>}
    {run.status === "awaiting_approval" && <section className="section-card approval-panel"><div className="section-heading"><div><p className="eyebrow">HUMAN REVIEW</p><h2>Review your CRM proposal</h2><p>Select the companies you want to import. Nothing is written before your approval.</p></div><Status value="awaiting_approval" /></div><div className="proposal-list">{approval.map((item) => <label className="proposal-row" key={item.candidate_id}><input type="checkbox" checked={selected.includes(item.candidate_id)} onChange={() => toggle(item.candidate_id)} /><span className="proposal-check" /><span className="proposal-info"><b>{item.company_name}</b><small>{item.domain}</small></span>{item.source_urls[0] && <a href={item.source_urls[0]} target="_blank" rel="noreferrer">Source ↗</a>}</label>)}</div><div className="approval-actions"><button className="button secondary" disabled={busy} onClick={() => submitApproval(false)}>Reject proposal</button><button className="button primary" disabled={busy || !selected.length} onClick={() => submitApproval(true)}>{busy ? "Submitting…" : `Approve ${selected.length} selected`} <span>→</span></button></div></section>}
    {candidates.length > 0 && <section className="section-card evidence-card"><div className="section-heading"><div><h2>Company evidence</h2><p>{candidates.length} researched candidates · source details retained</p></div><div className="decision-legend"><span>Qualified</span><span>Needs review</span><span>Not a match</span></div></div><div className="lead-grid">{candidates.map((candidate) => <article className="lead-card" key={candidate.candidate_id}><div className="lead-card-top"><span className="lead-avatar">{candidate.company_name.slice(0, 1).toUpperCase()}</span><div><h3>{candidate.legal_name || candidate.company_name}</h3><a href={candidate.website_url} target="_blank" rel="noreferrer">{candidate.domain || "Website not established"} ↗</a></div><Status value={candidate.decision} /></div><p className="lead-rationale">{candidate.rationale}</p><div className="lead-meta"><span>{Math.round(candidate.confidence * 100)}% confidence</span><span>Website {candidate.website_status}</span><span>{candidate.source_evidence.length} sources</span></div>{candidate.source_evidence.map((source) => <div className="source-box" key={source.evidence_id}><a href={source.source_url} target="_blank" rel="noreferrer">{source.source_url.replace(/^https?:\/\//, "").split("/")[0]} ↗</a><p>{source.excerpt}</p></div>)}</article>)}</div></section>}
  </>;
}
