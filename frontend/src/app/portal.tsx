"use client";

import { type Dispatch, type FormEvent, type ReactNode, type RefObject, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Activity as ActivityIcon, ArrowDownToLine, ArrowLeft, ArrowUpRight, Building2, Check, ChevronDown, ChevronRight, CircleAlert, CircleCheck, CirclePlus, Clock3, Copy, ExternalLink, History, LayoutDashboard, ListTree, LoaderCircle, PanelLeftClose, Search, ShieldCheck, Sparkles, X } from "lucide-react";

type User = { user_id: string; email: string };
type Activity = { id?: number; at?: string; node?: string; status?: string; message?: string; error?: string; [key: string]: unknown };
type Run = {
  run_id: string; request_id: string; status: string;
  request: { industry: string; region: string; target_leads: number; test_mode?: boolean; qualification_criteria?: string[] };
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
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authMode, setAuthMode] = useState<"login" | "register" | "forgot" | "reset">(pathname === "/register" ? "register" : pathname === "/forgot-password" ? "forgot" : pathname === "/reset-password" ? "reset" : "login");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [actionToken, setActionToken] = useState("");
  const [authMessage, setAuthMessage] = useState("");
  const [authError, setAuthError] = useState("");
  const [view, setView] = useState<"overview" | "new" | "history">(pathname.includes("/research/new") ? "new" : pathname.includes("/history") || pathname.includes("/runs/") ? "history" : "overview");
  const [target, setTarget] = useState(50);
  const [testBrowserbaseLimit, setTestBrowserbaseLimit] = useState<number | null>(null);
  const [testLlmLimit, setTestLlmLimit] = useState<number | null>(null);
  const [testCandidateUrlLimit, setTestCandidateUrlLimit] = useState<number | null>(null);
  const [testMode, setTestMode] = useState(false);
  const [industry, setIndustry] = useState("AI software");
  const [region, setRegion] = useState("Bengaluru");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");
  const [criteria, setCriteria] = useState("B2B AI software company");
  const [run, setRun] = useState<Run | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [overviewHistory, setOverviewHistory] = useState<HistoryItem[]>([]);
  const [overviewHistoryLoading, setOverviewHistoryLoading] = useState(false);
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
  const token = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("verify-email_token") || new URLSearchParams(window.location.search).get("token") : null;
  const resetToken = typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("reset-password_token") || (pathname === "/reset-password" ? new URLSearchParams(window.location.search).get("token") : null) : null;
  const navigateView = (next: "overview" | "new" | "history") => {
    setView(next);
    router.push(next === "overview" ? "/dashboard" : next === "new" ? "/dashboard/research/new" : "/dashboard/history");
  };

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
  const loadAllHistory = useCallback(async () => {
    const pageSize = 100;
    setOverviewHistoryLoading(true);
    try {
    const first = await api(`/runs?limit=${pageSize}&offset=0`);
    const all = [...(first.results || [])] as HistoryItem[];
    const offsets = Array.from({ length: Math.ceil(Math.max(0, (first.total || 0) - all.length) / pageSize) }, (_, index) => all.length + index * pageSize);
    const remaining = await Promise.all(offsets.map((offset) => api(`/runs?limit=${pageSize}&offset=${offset}`)));
    for (const page of remaining) all.push(...(page.results || []));
    setOverviewHistory(all);
    setHistoryTotal(first.total || 0);
    } finally { setOverviewHistoryLoading(false); }
  }, []);

  useEffect(() => {
    api("/config").then((data) => { setTarget(data.number_of_leads || 50); setTestBrowserbaseLimit(data.test_mode_browserbase_call_limit ?? null); setTestLlmLimit(data.test_mode_llm_call_limit ?? null); setTestCandidateUrlLimit(data.test_mode_candidate_url_limit ?? null); }).catch(() => undefined);
    api("/auth/me").then((data) => setUser(data)).catch(() => setUser(null)).finally(() => setAuthReady(true));
    if (token && pathname === "/verify-email") {
      window.history.replaceState(null, "", location.pathname);
      api("/auth/verify-email", { method: "POST", body: JSON.stringify({ token }) }).then((data) => { setAuthMessage(data.message); setAuthMode("login"); router.replace("/login"); }).catch((reason) => setAuthError(reason.message));
    }
    if (resetToken) { setActionToken(resetToken); setAuthMode("reset"); setAuthMessage("Choose a new password to finish resetting your account."); }
  }, []);

  useEffect(() => {
    if (!user) return;
    if (pathname === "/dashboard") void loadAllHistory().catch((reason) => setError(reason instanceof Error ? reason.message : "Could not load research history."));
  }, [user, pathname, loadAllHistory]);
  useEffect(() => { if (user && pathname.endsWith("/history")) void loadHistory(0, historyFilter); }, [user, pathname, historyFilter, loadHistory]);
  useEffect(() => { if (!authReady) return; if (pathname.startsWith("/dashboard") && !user) router.replace(`/login?next=${encodeURIComponent(pathname + window.location.search)}`); else if (user && ["/login", "/register", "/forgot-password"].includes(pathname)) router.replace("/dashboard"); }, [authReady, user, pathname, router]);
  useEffect(() => {
    if (!user) return;
    const runId = pathname.match(/\/dashboard\/runs\/([^/]+)/)?.[1];
    if (runId) { setView("history"); void refreshRun(decodeURIComponent(runId)).catch((reason) => setError(reason.message)); }
  }, [user, refreshRun, pathname]);
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
  const candidates = run?.report?.candidates || run?.candidates || [];
  const filteredHistory = useMemo(() => history.filter((item) => `${item.request.industry} ${item.request.region} ${item.status}`.toLowerCase().includes(search.toLowerCase())), [history, search]);
  const latestEvent = events.at(-1);
  const statusCounts = useMemo(() => ({ completed: overviewHistory.filter((item) => item.status === "completed").length, active: overviewHistory.filter((item) => ["queued", "running"].includes(item.status)).length, review: overviewHistory.filter((item) => item.status === "awaiting_approval").length }), [overviewHistory]);
  const employeeRangeInvalid = Boolean(minimum && maximum && Number(minimum) > Number(maximum));

  useEffect(() => {
    if (pathname === "/login") setAuthMode("login");
    if (pathname === "/register") setAuthMode("register");
    if (pathname === "/forgot-password") setAuthMode("forgot");
    if (pathname === "/reset-password") setAuthMode("reset");
    if (pathname === "/dashboard") setView("overview");
    else if (pathname.endsWith("/research/new")) setView("new");
    else if (pathname.endsWith("/history") || pathname.includes("/runs/")) setView("history");
    if (!pathname.includes("/runs/")) setRun(null);
  }, [pathname]);

  async function submitAuth(event: FormEvent) {
    event.preventDefault(); setAuthError(""); setAuthMessage(""); setBusy(true);
    try {
      if (authMode === "register") {
        const result = await api("/auth/register", { method: "POST", body: JSON.stringify({ email: authEmail, password: authPassword }) });
        setUser(result.user); router.push("/dashboard");
      } else if (authMode === "forgot") {
        const result = await api("/auth/forgot-password", { method: "POST", body: JSON.stringify({ email: authEmail }) }); setAuthMessage(result.message);
      } else if (authMode === "reset") {
        const result = await api("/auth/reset-password", { method: "POST", body: JSON.stringify({ token: actionToken, password: authPassword }) }); setUser(null); setAuthMessage(result.message); setAuthMode("login"); setActionToken(""); router.replace("/login");
      } else {
        const result = await api("/auth/login", { method: "POST", body: JSON.stringify({ email: authEmail, password: authPassword }) }); setUser(result.user); router.replace(new URLSearchParams(window.location.search).get("next") || "/dashboard");
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
      const result = await api("/runs", { method: "POST", body: JSON.stringify({ request_id: `lead-run-${crypto.randomUUID()}`, industry, region, company_size: { min_employees: minimum ? Number(minimum) : null, max_employees: maximum ? Number(maximum) : null }, qualification_criteria: criteria.split("\n").map((item) => item.trim()).filter(Boolean), target_leads: target, test_mode: testMode }) });
      router.push(`/dashboard/runs/${encodeURIComponent(result.run_id)}`);
      setView("history");
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
    setError(""); setEvents([]); setView("history"); router.push(`/dashboard/runs/${encodeURIComponent(runId)}`);
    // The route effect loads the run, including when this URL is opened directly.
  }

  if (!authReady) return <main className="auth-screen"><div className="loading-mark"><Brand /><span className="spinner" /></div></main>;
  if (!pathname.startsWith("/dashboard") || !user || resetToken || authMode === "reset") return <main className="auth-screen"><div className="auth-brand"><Brand /></div><section className="auth-card"><div className="auth-icon">✦</div><p className="eyebrow">YOUR RESEARCH WORKSPACE</p><h1>{authMode === "register" ? "Create your account" : authMode === "forgot" ? "Reset your password" : authMode === "reset" ? "Choose a new password" : "Welcome back"}</h1><p className="auth-sub">{authMode === "register" ? "Save research runs and keep every decision close." : authMode === "forgot" ? "We’ll send a secure reset link if the account exists." : authMode === "reset" ? authMessage : "Sign in to continue your company research."}</p>
    {authMessage && authMode !== "reset" && <div className="notice">{authMessage}</div>}{authError && <div className="error-box" role="alert">{authError}</div>}
    <form onSubmit={submitAuth}><label>Email address<input autoComplete="email" type="email" required value={authEmail} onChange={(e) => setAuthEmail(e.target.value)} placeholder="you@company.com" /></label>{authMode !== "forgot" && <label>Password<input autoComplete={authMode === "login" ? "current-password" : "new-password"} type="password" minLength={10} required value={authPassword} onChange={(e) => setAuthPassword(e.target.value)} placeholder="At least 10 characters" /></label>}<button className="button primary auth-submit" disabled={busy}>{busy ? "Please wait…" : authMode === "register" ? "Create account" : authMode === "forgot" ? "Send reset link" : authMode === "reset" ? "Update password" : "Sign in"}<span>→</span></button></form>
    <div className="auth-links">{authMode === "login" ? <><button onClick={() => { setAuthMode("forgot"); router.push("/forgot-password"); }}>Forgot password?</button><span>New here? <button onClick={() => { setAuthMode("register"); router.push("/register"); }}>Create account</button></span></> : <button onClick={() => { setAuthMode("login"); router.push("/login"); setAuthMessage(""); setAuthError(""); }}>← Back to sign in</button>}</div>
    <div className="auth-security"><span>⌑</span> Private by design · your runs belong to you</div>
  </section><div className="auth-footer">LEADSPACE <span>·</span> RESEARCH WITH CLARITY</div></main>;

  return <div className="app-frame">
    <aside className="sidebar"><div className="sidebar-top"><Brand /><div className="workspace-pill"><span className="avatar">{user.email.slice(0, 1).toUpperCase()}</span><span><b>My workspace</b><small>Personal account</small></span><span className="chevron"><ChevronDown size={14} /></span></div><p className="nav-caption">WORKSPACE</p><nav className="side-nav"><button aria-label="Overview" title="Overview" className={view === "overview" ? "selected" : ""} onClick={() => { navigateView("overview"); setRun(null); }}><span><LayoutDashboard size={16} strokeWidth={1.7} /></span> Overview</button><button aria-label="New research" title="New research" className={view === "new" ? "selected" : ""} onClick={() => { navigateView("new"); setRun(null); }}><span><CirclePlus size={16} strokeWidth={1.7} /></span> New research</button><button aria-label="Research history" title="Research history" className={view === "history" ? "selected" : ""} onClick={() => { navigateView("history"); setRun(null); }}><span><History size={16} strokeWidth={1.7} /></span> Research history <em>{historyTotal}</em></button></nav></div><div className="sidebar-bottom"><div className="help-card"><span className="help-spark"><Sparkles size={16} /></span><b>Evidence first</b><p>Every decision stays linked to its source.</p></div><button className="profile-button" onClick={() => void signOut()}><span className="avatar avatar-small">{user.email.slice(0, 1).toUpperCase()}</span><span className="profile-text"><b>{user.email}</b><small>Personal account</small></span><span className="signout">↪</span></button></div></aside>
    <main className="workspace"><header className="topbar"><div className="breadcrumbs"><span>Workspace</span><b>/</b><strong>{run ? "Run details" : view === "overview" ? "Overview" : view === "new" ? "New research" : "Research history"}</strong></div><div className="top-actions"><span className="secure-label"><ShieldCheck size={14} strokeWidth={1.8} /> Secure workspace</span><button className="avatar top-avatar" aria-label="Sign out" onClick={() => void signOut()}>{user.email.slice(0, 1).toUpperCase()}</button></div></header>
    <div className="content">
      {view === "overview" && !run && <>
        <div className="welcome-row"><div><p className="eyebrow">YOUR RESEARCH DESK</p><h1>Good research starts<br /><span>with a clear question.</span></h1><p className="lede">Find, verify, and review the companies that fit your next opportunity.</p></div><button className="button primary" onClick={() => { navigateView("new"); setRun(null); }}>Start new research <span>↗</span></button></div>
        <div className="stats-row"><div className="metric-card"><span className="metric-icon lilac">▤</span><div><small>Total research runs</small><strong>{historyTotal}</strong></div><span className="metric-note">All time</span></div><div className="metric-card"><span className="metric-icon mint">◷</span><div><small>In progress</small><strong>{statusCounts.active}</strong></div><span className="metric-note">Recent 10 runs</span></div><div className="metric-card"><span className="metric-icon amber">✓</span><div><small>Ready for review</small><strong>{statusCounts.review}</strong></div><span className="metric-note">Recent 10 runs</span></div><div className="metric-card"><span className="metric-icon blue">✦</span><div><small>Completed</small><strong>{statusCounts.completed}</strong></div><span className="metric-note">Recent 10 runs</span></div></div>
        <section className="section-card"><div className="section-heading"><div><h2>All research</h2><p>Every lead search saved in your workspace.</p></div><button className="text-button" onClick={() => navigateView("history")}>Open paginated history <span>→</span></button></div>{overviewHistoryLoading ? <div className="log-wait"><span className="spinner"/>Loading your research history…</div> : overviewHistory.length ? <HistoryTable items={overviewHistory} openRun={openRun} /> : <EmptyHistory onStart={() => navigateView("new")} />}</section>
        <div className="bottom-note"><span>✧</span><div><b>Every result has a reason.</b><p>Review source evidence and approve records before they reach your CRM.</p></div></div>
      </>}

      {view === "new" && !run && <><div className="page-heading"><p className="eyebrow">RESEARCH SETUP</p><h1>Build your next lead list</h1><p>Tell us who you’re looking for. We’ll research public sources and prepare a reviewable proposal.</p></div><div className="request-layout"><form className="section-card request-card" onSubmit={startResearch}><div className="card-heading"><span className="step-bubble">01</span><div><h2>Define your market</h2><p>Start broad, then add the criteria that matter.</p></div></div><div className="form-grid"><label>Industry or category<input value={industry} onChange={(e) => setIndustry(e.target.value)} minLength={2} required placeholder="e.g. AI software" /></label><label>City or region<input value={region} onChange={(e) => setRegion(e.target.value)} minLength={2} required placeholder="e.g. Bengaluru" /></label><label>Minimum employees <small>Optional</small><input type="number" min="1" value={minimum} onChange={(e) => setMinimum(e.target.value)} placeholder="No minimum" /></label><label>Maximum employees <small>Optional</small><input type="number" min="1" value={maximum} onChange={(e) => setMaximum(e.target.value)} placeholder="No maximum" /></label></div>{employeeRangeInvalid && <p className="field-error" role="alert">Minimum employee count must be less than or equal to the maximum.</p>}<label className="criteria-field">Qualification criteria <small>One per line</small><textarea rows={4} value={criteria} onChange={(e) => setCriteria(e.target.value)} required placeholder="B2B software company&#10;Has a public product website" /></label><label className="test-mode-option"><input type="checkbox" checked={testMode} onChange={(event) => setTestMode(event.target.checked)} /><span><b>Low-call test mode</b><small>{testBrowserbaseLimit === null || testLlmLimit === null || testCandidateUrlLimit === null ? "Uses this workspace’s configured request and candidate limits. " : `Uses live sources for one discovery round and up to ${testCandidateUrlLimit} candidate URLs, ${testBrowserbaseLimit} Browserbase requests, and ${testLlmLimit} LLM calls. `}Additional candidates use the rule-based qualifier.</small></span></label>{error && <div className="error-box" role="alert">{error}</div>}<div className="request-footer"><span>Target list <b>{target} companies</b> <small>set by workspace configuration</small></span><button className="button primary" disabled={busy || employeeRangeInvalid || !industry.trim() || !region.trim() || !criteria.trim()}>{busy ? "Starting research…" : "Start research run"}<span>↗</span></button></div></form><aside className="section-card guide-card"><span className="guide-icon">✧</span><h3>What happens next?</h3><ol><li><b>Discover</b><span>Gather potential companies from public sources.</span></li><li><b>Research</b><span>Check websites and match your criteria.</span></li><li><b>Review</b><span>See the evidence before any CRM import.</span></li></ol><div className="guide-foot">No CRM records are created without your approval.</div></aside></div></>}

      {view === "history" && !run && <><div className="page-heading page-heading-row"><div><p className="eyebrow">YOUR WORKSPACE</p><h1>Research history</h1><p>Pick up where you left off or review a finished lead list.</p></div><button className="button primary" onClick={() => navigateView("new")}>＋ New research</button></div><section className="section-card history-card"><div className="history-controls"><div className="search-field"><span>⌕</span><input aria-label="Search history" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by industry or region…" /></div><select aria-label="Filter by status" value={historyFilter} onChange={(e) => setHistoryFilter(e.target.value)}><option value="all">All statuses</option><option value="running">In progress</option><option value="awaiting_approval">Needs review</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="queued">Queued</option></select></div>{filteredHistory.length ? <HistoryTable items={filteredHistory} openRun={openRun} /> : <EmptyHistory onStart={() => navigateView("new")} search={!!search || historyFilter !== "all"} />}<div className="pagination"><span>Showing {historyTotal ? historyPage * 10 + 1 : 0}–{Math.min((historyPage + 1) * 10, historyTotal)} of {historyTotal}</span><div><button disabled={historyPage === 0} onClick={() => void loadHistory(historyPage - 1)}>← Previous</button><button disabled={(historyPage + 1) * 10 >= historyTotal} onClick={() => void loadHistory(historyPage + 1)}>Next →</button></div></div></section></>}

      {run && <RunDetail run={run} events={events} latestEvent={latestEvent} streamState={streamState} elapsed={elapsed} logPaused={logPaused} setLogPaused={setLogPaused} logRef={logRef} selected={selected} setSelected={setSelected} busy={busy} error={error} submitApproval={submitApproval} onBack={() => { setRun(null); navigateView("history"); }} />}
    </div></main>
  </div>;
}

function HistoryTable({ items, openRun }: { items: HistoryItem[]; openRun: (id: string) => void }) {
  const [sortBy, setSortBy] = useState<"date" | "industry" | "leads">("date");
  const [descending, setDescending] = useState(true);
  const sortedItems = [...items].sort((a, b) => {
    const result = sortBy === "date" ? Date.parse(a.created_at) - Date.parse(b.created_at) : sortBy === "industry" ? a.request.industry.localeCompare(b.request.industry) : (a.report?.candidates.length ?? a.request.target_leads) - (b.report?.candidates.length ?? b.request.target_leads);
    return descending ? -result : result;
  });
  const sort = (field: "date" | "industry" | "leads") => { if (sortBy === field) setDescending(!descending); else { setSortBy(field); setDescending(field !== "industry"); } };
  return <div className="table-scroll"><table className="history-table"><thead><tr><th><button className="table-sort" onClick={() => sort("industry")}>RESEARCH REQUEST ↕</button></th><th><button className="table-sort" onClick={() => sort("date")}>CREATED ↕</button></th><th><button className="table-sort" onClick={() => sort("leads")}>LEADS ↕</button></th><th>STATUS</th><th /></tr></thead><tbody>{sortedItems.map((item) => <tr key={item.run_id} onClick={() => openRun(item.run_id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && openRun(item.run_id)}><td><span className="table-title">{item.request.industry}</span><small>{item.request.region} · {item.run_id.slice(0, 8)}</small></td><td>{new Date(item.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</td><td>{item.report ? item.report.candidates.length : item.request.target_leads}</td><td><Status value={item.status} /></td><td><button className="row-open" aria-label={`Open ${item.request.industry} run`}>↗</button></td></tr>)}</tbody></table></div>;
}
function EmptyHistory({ onStart, search = false }: { onStart: () => void; search?: boolean }) { return <div className="empty-state"><div className="empty-illustration">⌕</div><h3>{search ? "No matching research" : "Your research starts here"}</h3><p>{search ? "Try a different search or status filter." : "Create a lead search and it will be saved here for next time."}</p>{!search && <button className="button secondary" onClick={onStart}>Create your first run <span>→</span></button>}</div>; }

type TraceSelection =
  | { kind: "run" }
  | { kind: "stage"; stageId: string }
  | { kind: "node"; stageId: string; node: string }
  | { kind: "event"; event: Activity };

function RunDetail({ run, events, latestEvent, streamState, elapsed, logPaused, setLogPaused, logRef, selected, setSelected, busy, error, submitApproval, onBack }: { run: Run; events: Activity[]; latestEvent?: Activity; streamState: string; elapsed: number; logPaused: boolean; setLogPaused: (v: boolean) => void; logRef: RefObject<HTMLDivElement | null>; selected: string[]; setSelected: Dispatch<SetStateAction<string[]>>; busy: boolean; error: string; submitApproval: (approved: boolean) => void; onBack: () => void }) {
  const candidates = run.report?.candidates || run.candidates;
  const approval = run.approval?.changes || [];
  const [candidateFilter, setCandidateFilter] = useState("all");
  const [websiteFilter, setWebsiteFilter] = useState("all");
  const [candidateSearch, setCandidateSearch] = useState("");
  const [sortConfidence, setSortConfidence] = useState(false);
  const [expandedCompanies, setExpandedCompanies] = useState<string[]>([]);
  const [expandedStages, setExpandedStages] = useState<string[]>([]);
  const [expandedNodes, setExpandedNodes] = useState<string[]>([]);
  const [expandedRunInfo, setExpandedRunInfo] = useState(true);
  const [activityQuery, setActivityQuery] = useState("");
  const [activityStatus, setActivityStatus] = useState("all");
  const [activityOrder, setActivityOrder] = useState<"oldest" | "newest">("oldest");
  const [traceSelection, setTraceSelection] = useState<TraceSelection>({ kind: "run" });
  const [inspectorTab, setInspectorTab] = useState("overview");
  const [copiedDomain, setCopiedDomain] = useState("");
  const [copiedPayload, setCopiedPayload] = useState(false);
  const [traceOpen, setTraceOpen] = useState(true);
  const [activityOpen, setActivityOpen] = useState(true);
  const reduceMotion = useReducedMotion() ?? false;
  const animationDuration = reduceMotion ? 0 : .2;
  const visibleCandidates = useMemo(() => candidates.filter((candidate) => {
    const matchesDecision = candidateFilter === "all" || candidate.decision === candidateFilter;
    const matchesWebsite = websiteFilter === "all" || candidate.website_status === websiteFilter;
    const sourceText = candidate.source_evidence.map((source) => `${source.source_url} ${source.source_type} ${source.excerpt}`).join(" ");
    const matchesSearch = `${candidate.legal_name || ""} ${candidate.company_name} ${candidate.domain} ${candidate.decision} ${candidate.website_status} ${candidate.rationale} ${sourceText}`.toLowerCase().includes(candidateSearch.toLowerCase());
    return matchesDecision && matchesWebsite && matchesSearch;
  }).slice().sort((a, b) => sortConfidence ? b.confidence - a.confidence : a.company_name.localeCompare(b.company_name)), [candidates, candidateFilter, websiteFilter, candidateSearch, sortConfidence]);
  const toggleApprovalCandidate = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const stageDefinitions = [
    { id: "queue", title: "Queue", caption: "Request validation and scheduling", nodes: ["queued", "worker", "validate_input"] },
    { id: "discover", title: "Discover", caption: "Find and deduplicate candidate companies", nodes: ["discover_candidates", "deduplicate", "extract_directory_links"] },
    { id: "research", title: "Research", caption: "Collect and qualify public company evidence", nodes: ["fast_verify", "research_candidates", "cheap_filter", "deep_qualify", "targeted_research"] },
    { id: "qualify", title: "Qualify", caption: "Verify fit and company websites", nodes: ["verify_websites"] },
    { id: "review", title: "Review", caption: "Prepare the proposal for approval", nodes: ["prepare_proposal", "approval_gate"] },
    { id: "import", title: "Import", caption: "Write approved records and build the report", nodes: ["execute_import", "verify_import", "build_report", "complete", "test_mode_budget"] },
  ];
  const stageForNode = (node: string) => stageDefinitions.findIndex((stage) => stage.nodes.includes(node));
  const groupedStages = stageDefinitions.map((stage, index) => {
    const matching = events.filter((event) => {
      const eventStage = stageForNode(event.node || "");
      return eventStage === index || (eventStage < 0 && index === 2);
    });
    const nodeNames = [...stage.nodes, ...new Set(matching.map((event) => event.node || "worker").filter((node) => !stage.nodes.includes(node)))];
    const nodes = nodeNames.map((node) => ({ node, events: matching.filter((event) => (event.node || "worker") === node) }));
    const failed = matching.some((event) => ["failed", "error", "blocked", "partial_failure"].includes(event.status || ""));
    const waitingReview = matching.some((event) => event.status === "awaiting_approval");
    const lastObservedStage = latestEvent ? stageForNode(latestEvent.node || "") : -1;
    const state = failed ? "failed" : waitingReview ? "awaiting" : index === lastObservedStage && ["queued", "running"].includes(run.status) ? "latest" : matching.length ? "recorded" : "pending";
    return { ...stage, index, events: matching, nodes, state };
  });
  const activityItems = useMemo(() => events.filter((event) => {
    const query = activityQuery.trim().toLowerCase();
    const matchesQuery = !query || `${label(event.node || "")} ${event.node || ""} ${event.message || ""} ${label(event.status || "")} ${event.status || ""} ${event.error || ""}`.toLowerCase().includes(query);
    const matchesStatus = activityStatus === "all" || (event.status || "info") === activityStatus;
    return matchesQuery && matchesStatus;
  }).slice().sort((a, b) => {
    const aTime = a.at ? Date.parse(a.at) : a.id || 0;
    const bTime = b.at ? Date.parse(b.at) : b.id || 0;
    return activityOrder === "oldest" ? aTime - bTime : bTime - aTime;
  }), [events, activityQuery, activityStatus, activityOrder]);
  const allStagesExpanded = groupedStages.every((stage) => expandedStages.includes(stage.id));
  const allCompaniesExpanded = visibleCandidates.length > 0 && visibleCandidates.every((candidate) => expandedCompanies.includes(candidate.candidate_id));
  const currentStage = traceSelection.kind === "stage" ? groupedStages.find((stage) => stage.id === traceSelection.stageId) : null;
  const currentNode = traceSelection.kind === "node" ? groupedStages.find((stage) => stage.id === traceSelection.stageId)?.nodes.find((node) => node.node === traceSelection.node) : null;
  const selectedEvent = traceSelection.kind === "event" ? traceSelection.event : null;
  const uniqueActivityStatuses = [...new Set(events.map((event) => event.status || "info"))];
  const progressStatus = run.status === "completed" ? "Completed" : run.status === "failed" ? "Failed" : run.status === "awaiting_approval" ? "Awaiting review" : run.status === "queued" ? "Queued" : "Running";
  const qualifiedCount = candidates.filter((candidate) => candidate.decision === "qualified").length;
  const uncertainCount = candidates.filter((candidate) => candidate.decision === "uncertain").length;
  const rejectedCount = candidates.filter((candidate) => candidate.decision === "rejected" || candidate.decision === "duplicate").length;
  const sourceCount = candidates.reduce((total, candidate) => total + candidate.source_evidence.length, 0);
  const toggleStage = (id: string) => setExpandedStages((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const toggleNode = (id: string) => setExpandedNodes((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const toggleCompany = (id: string) => setExpandedCompanies((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  const selectEvent = (event: Activity) => { setTraceSelection({ kind: "event", event }); setInspectorTab("details"); };
  const selectNode = (stageId: string, node: string) => { setTraceSelection({ kind: "node", stageId, node }); setInspectorTab("overview"); };
  const selectStage = (stageId: string) => { setTraceSelection({ kind: "stage", stageId }); setInspectorTab("overview"); };
  const jumpToOldest = () => { if (logRef.current) logRef.current.scrollTop = activityOrder === "oldest" ? 0 : logRef.current.scrollHeight; };
  const jumpToLatest = () => { if (logRef.current) logRef.current.scrollTop = activityOrder === "oldest" ? logRef.current.scrollHeight : 0; };
  const copyDomain = async (domain: string) => {
    if (!navigator.clipboard) return;
    try { await navigator.clipboard.writeText(domain); setCopiedDomain(domain); window.setTimeout(() => setCopiedDomain(""), 1600); } catch { setCopiedDomain(""); }
  };
  const copyEventPayload = async (event: Activity) => {
    if (!navigator.clipboard) return;
    try { await navigator.clipboard.writeText(JSON.stringify(event, null, 2)); setCopiedPayload(true); window.setTimeout(() => setCopiedPayload(false), 1600); } catch { setCopiedPayload(false); }
  };
  const exportCsv = () => {
    const quote = (value: unknown) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const rows = [["Company", "Domain", "Decision", "Confidence", "Website status", "Rationale", "Sources"], ...visibleCandidates.map((candidate) => [candidate.legal_name || candidate.company_name, candidate.domain, candidate.decision, Math.round(candidate.confidence * 100), candidate.website_status, candidate.rationale, candidate.source_evidence.map((source) => source.source_url).join(" | ")])];
    const blob = new Blob([rows.map((row) => row.map(quote).join(",")).join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = `leadspace-${run.run_id.slice(0, 8)}.csv`; anchor.click(); URL.revokeObjectURL(url);
  };
  useEffect(() => {
    if (logPaused || !logRef.current) return;
    logRef.current.scrollTo({ top: activityOrder === "oldest" ? logRef.current.scrollHeight : 0, behavior: reduceMotion ? "auto" : "smooth" });
  }, [events, logPaused, activityOrder, activityItems.length, reduceMotion, logRef]);

  const selectionTitle = selectedEvent ? label(selectedEvent.node || "Worker event") : traceSelection.kind === "node" ? label(traceSelection.node) : traceSelection.kind === "stage" ? currentStage?.title || "Workflow step" : "Run overview";
  const selectionStatus = selectedEvent?.status || (traceSelection.kind === "node" ? currentNode?.events.at(-1)?.status : traceSelection.kind === "stage" ? currentStage?.state : run.status);
  const detailEntries = selectedEvent ? Object.entries(selectedEvent).filter(([key]) => !["id", "node", "status", "message", "at", "error"].includes(key)) : [];
  const detailValue = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const inspectorTabs = selectedEvent ? ["details", "payload"] : traceSelection.kind === "node" ? ["overview", "events"] : traceSelection.kind === "stage" ? ["overview", "nodes"] : ["overview", "request", "results"];
  const expandInspectorSection = (open: boolean, content: ReactNode, className = "inspector-body") => <AnimatePresence initial={false}>{open && <motion.div key="inspector-panel" className={className} initial={reduceMotion ? false : { opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: animationDuration }} style={{ overflow: "hidden" }}>{content}</motion.div>}</AnimatePresence>;
  const RunStatusIcon = run.status === "failed" ? CircleAlert : run.status === "completed" ? CircleCheck : run.status === "awaiting_approval" ? Clock3 : ActivityIcon;

  return <div className="run-console">
    <div className="run-console-head">
      <div className="run-title-block"><button className="back-link" onClick={onBack}><ArrowLeft size={14} /> <span>Research history</span></button><div className="run-title-line"><div><p className="eyebrow">RESEARCH TRACE <span className="mono-divider">/</span> {run.run_id.slice(0, 8).toUpperCase()}{run.request.test_mode ? " · LOW-CALL MODE" : ""}</p><h1>{run.request.industry}<span> · </span>{run.request.region}</h1></div><Status value={run.status} /></div><p className="run-created">Started {new Date(run.created_at).toLocaleString()} <span>·</span> Target {run.request.target_leads} companies</p></div>
      <div className="run-head-actions"><span className={`run-connection ${streamState}`}><i />{streamState === "live" ? "Live updates" : streamState === "reconnecting" ? "Reconnecting" : run.status === "completed" ? "Trace complete" : run.status === "failed" ? "Run stopped" : "Waiting for updates"}</span>{run.report && <button className="button secondary export-button" onClick={exportCsv}><ArrowDownToLine size={14} /> Export CSV</button>}</div>
    </div>
    {error && <div className="error-box" role="alert"><CircleAlert size={15} />{error}</div>}
    <section className="run-overview-strip">
      <div className="run-state-block"><span className={`run-state-icon ${run.status}`}><RunStatusIcon size={16} /></span><div><small>RUN STATUS</small><b>{progressStatus}</b></div></div>
      <div className="overview-divider" />
      <div className="run-stat"><small>WORKER EVENTS</small><b>{events.length}</b></div>
      <div className="run-stat"><small>CANDIDATES</small><b>{candidates.length}</b></div>
      <div className="run-stat"><small>QUALIFIED</small><b className="success-value">{qualifiedCount}</b></div>
      <div className="run-stat"><small>UNCERTAIN</small><b className="warning-value">{uncertainCount}</b></div>
      <div className="run-stat"><small>SOURCES</small><b>{sourceCount}</b></div>
      {(["queued", "running"].includes(run.status)) && <div className="run-elapsed"><Clock3 size={13} /><span>{Math.floor(elapsed / 60)}:{String(elapsed % 60).padStart(2, "0")}</span></div>}
    </section>
    <section className="workflow-progress" aria-label="Workflow checkpoints"><div className="workflow-progress-head"><span>Workflow checkpoints</span><small>Statuses reflect recorded worker events</small></div><div className="stage-rail">{groupedStages.map((stage) => <button key={stage.id} className={`stage ${stage.state} ${traceSelection.kind === "stage" && traceSelection.stageId === stage.id ? "selected" : ""}`} aria-pressed={traceSelection.kind === "stage" && traceSelection.stageId === stage.id} onClick={() => { selectStage(stage.id); setExpandedStages((current) => current.includes(stage.id) ? current : [...current, stage.id]); }}><span className="stage-icon">{stage.state === "recorded" || stage.state === "awaiting" ? <Check size={12} /> : stage.state === "failed" ? <X size={12} /> : stage.state === "latest" ? <i /> : String(stage.index + 1).padStart(2, "0")}</span><b>{stage.title}</b></button>)}</div></section>

    <div className="trace-workbench">
      <section className={`console-pane activity-pane ${activityOpen ? "is-open" : "is-closed"}`}>
        <header className="pane-heading"><div><div className="pane-title-line"><ActivityIcon size={15} /><h2>Activity</h2><span className="pane-count">{events.length}</span></div><p>Worker events, in run order</p></div><button className="pane-collapse" aria-label={activityOpen ? "Collapse activity feed" : "Expand activity feed"} aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)}><ChevronDown size={15} /></button></header>
        {expandInspectorSection(activityOpen, <>
          <div className="activity-controls"><label className="console-search"><Search size={14} /><input aria-label="Search worker events" value={activityQuery} onChange={(event) => setActivityQuery(event.target.value)} placeholder="Filter events…" /></label><select aria-label="Filter worker events by status" value={activityStatus} onChange={(event) => setActivityStatus(event.target.value)}><option value="all">All statuses</option>{uniqueActivityStatuses.map((status) => <option key={status} value={status}>{label(status)}</option>)}</select></div>
          <div className="activity-toolbar"><select aria-label="Activity order" value={activityOrder} onChange={(event) => setActivityOrder(event.target.value as "oldest" | "newest")}><option value="oldest">Oldest first</option><option value="newest">Newest first</option></select><button className="quiet-control" onClick={jumpToOldest}>Oldest</button><button className="quiet-control latest-control" onClick={jumpToLatest}>Latest <ArrowDownToLine size={12} /></button></div>
          <div className="activity-log" ref={logRef} aria-live="polite">{activityItems.length ? activityItems.map((event, index) => <ActivityEvent event={event} key={`${event.id ?? index}-${event.node ?? "worker"}-${index}`} variant="feed" onInspect={() => selectEvent(event)} selected={traceSelection.kind === "event" && traceSelection.event === event} reduceMotion={reduceMotion} />) : events.length ? <div className="pane-empty"><Search size={17} /><b>No matching events</b><span>Try a different search or status.</span></div> : <div className="log-wait"><LoaderCircle size={15} className={run.status === "running" || run.status === "queued" ? "spin-icon" : ""} />{run.status === "failed" ? run.error || "No worker events were recorded." : "Waiting for the first worker update…"}</div>}</div>
          <footer className="activity-footer"><button className="quiet-control" onClick={() => setLogPaused(!logPaused)}>{logPaused ? "Resume auto-scroll" : "Pause auto-scroll"}</button>{logPaused && <button className="quiet-control" onClick={jumpToLatest}>Jump to latest <ArrowDownToLine size={12} /></button>}<span>{activityItems.length} shown</span></footer>
        </>, "activity-pane-body")}
      </section>

      <section className={`console-pane execution-pane ${traceOpen ? "is-open" : "is-closed"}`}>
        <header className="pane-heading"><div><div className="pane-title-line"><ListTree size={15} /><h2>Execution tree</h2><span className="pane-count">{groupedStages.reduce((count, stage) => count + stage.nodes.length, 0)}</span></div><p>LangGraph workflow hierarchy</p></div><div className="pane-heading-actions"><button className="quiet-control" onClick={() => setExpandedStages(allStagesExpanded ? [] : groupedStages.map((stage) => stage.id))}>{allStagesExpanded ? "Collapse all" : "Expand all"}</button><button className="pane-collapse" aria-label={traceOpen ? "Collapse execution tree" : "Expand execution tree"} aria-expanded={traceOpen} onClick={() => setTraceOpen(!traceOpen)}><ChevronDown size={15} /></button></div></header>
        {expandInspectorSection(traceOpen, <div className="trace-tree">{groupedStages.map((stage) => {
          const stageOpen = expandedStages.includes(stage.id);
          const stageSelected = traceSelection.kind === "stage" && traceSelection.stageId === stage.id;
          return <div className={`trace-stage state-${stage.state} ${stageSelected ? "selected" : ""}`} key={stage.id}>
            <button className="trace-stage-button" aria-expanded={stageOpen} aria-pressed={stageSelected} onClick={() => { toggleStage(stage.id); selectStage(stage.id); }}><span className={`trace-state-dot ${stage.state}`} />{stage.state === "recorded" || stage.state === "awaiting" ? <Check size={11} className="stage-check" /> : stage.state === "failed" ? <X size={11} className="stage-failed" /> : <span className="trace-step-number">{String(stage.index + 1).padStart(2, "0")}</span>}<span className="trace-stage-copy"><b>{stage.title}</b><small>{stage.caption}</small></span><span className={`trace-stage-meta ${stage.state}`}>{stage.state === "latest" ? "Latest event" : stage.state === "awaiting" ? "Review" : stage.state === "failed" ? "Error" : stage.events.length ? `${stage.events.length} events` : "No events"}</span><ChevronDown size={14} className={`tree-chevron ${stageOpen ? "open" : ""}`} />
            </button>
            <AnimatePresence initial={false}>{stageOpen && <motion.div key={`${stage.id}-children`} className="trace-stage-content" initial={reduceMotion ? false : { opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: animationDuration }} style={{ overflow: "hidden" }}>{stage.nodes.map(({ node, events: nodeEvents }) => {
              const nodeKey = `${stage.id}:${node}`;
              const nodeOpen = expandedNodes.includes(nodeKey);
              const nodeSelected = traceSelection.kind === "node" && traceSelection.stageId === stage.id && traceSelection.node === node;
              const mostRecent = nodeEvents.at(-1);
              const eventStatus = mostRecent?.status || "not_recorded";
              return <div className={`trace-node ${nodeSelected ? "selected" : ""}`} key={nodeKey}>
                <button className="trace-node-button" aria-expanded={nodeOpen} aria-pressed={nodeSelected} onClick={() => { toggleNode(nodeKey); selectNode(stage.id, node); }}><span className={`node-status-mark ${eventStatus}`} /><span className="trace-node-name">{label(node)}</span><span className="trace-node-summary">{mostRecent?.message || mostRecent?.error || (nodeEvents.length ? `${nodeEvents.length} recorded ${nodeEvents.length === 1 ? "event" : "events"}` : "No event recorded")}</span><span className="trace-node-count">{nodeEvents.length}</span><ChevronDown size={13} className={`tree-chevron ${nodeOpen ? "open" : ""}`} /></button>
                <AnimatePresence initial={false}>{nodeOpen && <motion.div key={`${nodeKey}-events`} className="node-events" initial={reduceMotion ? false : { opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: animationDuration }} style={{ overflow: "hidden" }}>{nodeEvents.length ? nodeEvents.map((event, eventIndex) => <ActivityEvent event={event} key={`${event.id ?? eventIndex}-${eventIndex}`} variant="trace" onInspect={() => selectEvent(event)} selected={traceSelection.kind === "event" && traceSelection.event === event} reduceMotion={reduceMotion} />) : <div className="trace-empty"><span className="trace-empty-dot" />No event was recorded for this node.</div>}</motion.div>}</AnimatePresence>
              </div>;
            })}</motion.div>}</AnimatePresence>
          </div>;
        })}</div>, "execution-pane-body")}
      </section>

      <aside className="console-pane inspector-pane">
        <header className="pane-heading inspector-heading"><div><div className="pane-title-line"><PanelLeftClose size={14} /><h2>Details</h2></div><p>Inspect the selected run data</p></div>{selectedEvent && <button className="pane-icon-action" aria-label={copiedPayload ? "Payload copied" : "Copy event payload"} onClick={() => void copyEventPayload(selectedEvent)}>{copiedPayload ? <Check size={14} /> : <Copy size={14} />}</button>}</header>
        <div className="inspector-scroll">
          <div className="inspector-selection"><div className="selection-icon">{selectedEvent ? <ActivityIcon size={15} /> : traceSelection.kind === "node" ? <ListTree size={15} /> : traceSelection.kind === "stage" ? <ChevronRight size={15} /> : <Building2 size={15} />}</div><div className="selection-copy"><small>{selectedEvent ? "WORKER EVENT" : traceSelection.kind === "node" ? "WORKFLOW NODE" : traceSelection.kind === "stage" ? "WORKFLOW STAGE" : "RESEARCH RUN"}</small><b>{selectionTitle}</b></div>{selectionStatus && <Status value={selectionStatus} />}</div>
          <div className="inspector-tabs" role="tablist" aria-label="Selected trace details">{inspectorTabs.map((tab) => <button role="tab" aria-selected={inspectorTab === tab} className={inspectorTab === tab ? "selected" : ""} key={tab} onClick={() => setInspectorTab(tab)}>{label(tab)}</button>)}</div>
          {selectedEvent && <div className="inspector-content">
            {inspectorTab === "payload" ? <><div className="inspector-section-title"><b>Full event payload</b><button className="quiet-control" onClick={() => void copyEventPayload(selectedEvent)}>{copiedPayload ? "Copied" : "Copy"}</button></div><pre className="payload-code">{JSON.stringify(selectedEvent, null, 2)}</pre></> : <>
              {selectedEvent.message && <p className="inspector-message">{selectedEvent.message}</p>}
              {selectedEvent.error && <div className="inspector-error"><CircleAlert size={14} /><p>{selectedEvent.error}</p></div>}
              <div className="inspector-fields"><div><small>Node</small><b>{label(selectedEvent.node || "worker")}</b></div><div><small>Status</small><Status value={selectedEvent.status || "info"} /></div><div><small>Timestamp</small><b>{selectedEvent.at ? new Date(selectedEvent.at).toLocaleString() : "Not provided"}</b></div>{selectedEvent.id !== undefined && <div><small>Event ID</small><b className="mono-value">{selectedEvent.id}</b></div>}</div>
              {detailEntries.length > 0 && <div className="inspector-data-section"><div className="inspector-section-title"><b>Recorded event data</b><span>{detailEntries.length} fields</span></div>{detailEntries.map(([key, value]) => <div className="inspector-data-row" key={key}><small>{label(key)}</small><pre>{detailValue(value)}</pre></div>)}</div>}
              <details className="inspector-raw"><summary>View full event payload</summary><pre className="payload-code">{JSON.stringify(selectedEvent, null, 2)}</pre></details>
            </>}
          </div>}
          {!selectedEvent && traceSelection.kind === "run" && <div className="inspector-content">{inspectorTab === "request" ? <><div className="inspector-section-title"><b>Research request</b></div><div className="inspector-fields"><div><small>Industry</small><b>{run.request.industry}</b></div><div><small>Region</small><b>{run.request.region}</b></div><div><small>Target</small><b>{run.request.target_leads} companies</b></div><div><small>Low-call mode</small><b>{run.request.test_mode ? "Enabled" : "Disabled"}</b></div></div><div className="inspector-data-section"><div className="inspector-section-title"><b>Qualification criteria</b><span>{run.request.qualification_criteria?.length || 0}</span></div>{run.request.qualification_criteria?.length ? run.request.qualification_criteria.map((criterion, index) => <div className="criteria-item" key={`${criterion}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span>{criterion}</div>) : <p className="inspector-muted">No qualification criteria were recorded.</p>}</div></> : inspectorTab === "results" ? <><div className="inspector-section-title"><b>Qualification summary</b></div><div className="decision-summary"><div><b>{qualifiedCount}</b><span>Qualified</span></div><div><b>{uncertainCount}</b><span>Uncertain</span></div><div><b>{rejectedCount}</b><span>Rejected / duplicate</span></div></div>{run.report && <p className="inspector-message">{run.report.imported} records imported · {run.report.target_shortfall} target shortfall</p>}</> : <><div className="inspector-overview"><div className="overview-status"><span className={`overview-status-icon ${run.status}`}>{run.status === "completed" ? <CircleCheck size={16} /> : run.status === "failed" ? <CircleAlert size={16} /> : <ActivityIcon size={16} />}</span><div><small>Current status</small><b>{progressStatus}</b></div></div><div className="inspector-fact"><span>Created</span><b>{new Date(run.created_at).toLocaleString()}</b></div><div className="inspector-fact"><span>Latest event</span><b>{latestEvent ? label(latestEvent.node || "worker") : "No events yet"}</b></div></div><button className="inspector-disclosure" aria-expanded={expandedRunInfo} onClick={() => setExpandedRunInfo(!expandedRunInfo)}><span>Run configuration</span><ChevronDown size={14} className={expandedRunInfo ? "open" : ""} /></button>{expandInspectorSection(expandedRunInfo, <div className="run-config"><div><small>Industry</small><b>{run.request.industry}</b></div><div><small>Region</small><b>{run.request.region}</b></div><div><small>Lead target</small><b>{run.request.target_leads}</b></div><div><small>Test mode</small><b>{run.request.test_mode ? "Enabled" : "Disabled"}</b></div><div className="run-config-criteria"><small>Qualification criteria</small>{run.request.qualification_criteria?.length ? run.request.qualification_criteria.map((criterion, index) => <span key={`${index}-${criterion}`}>{criterion}</span>) : <b>No criteria recorded</b>}</div></div>, "inspector-config-body")}</>}</div>}
          {!selectedEvent && traceSelection.kind === "stage" && <div className="inspector-content">{inspectorTab === "nodes" ? currentStage?.nodes.map(({ node, events: nodeEvents }) => <button className="inspector-list-row" key={node} onClick={() => { selectNode(traceSelection.stageId, node); setExpandedNodes((current) => current.includes(`${traceSelection.stageId}:${node}`) ? current : [...current, `${traceSelection.stageId}:${node}`]); }}><span className={`node-status-mark ${nodeEvents.at(-1)?.status || "not_recorded"}`} /><span><b>{label(node)}</b><small>{nodeEvents.length ? `${nodeEvents.length} recorded events` : "No event recorded"}</small></span><ChevronRight size={14} /></button>) : <><div className="inspector-overview"><div className="overview-status"><span className={`overview-status-icon ${currentStage?.state}`}><ListTree size={15} /></span><div><small>Workflow stage</small><b>{currentStage?.title}</b></div></div><p className="inspector-message">{currentStage?.caption}</p><div className="inspector-fact"><span>Recorded events</span><b>{currentStage?.events.length || 0}</b></div><div className="inspector-fact"><span>Workflow nodes</span><b>{currentStage?.nodes.length || 0}</b></div></div></>}</div>}
          {!selectedEvent && traceSelection.kind === "node" && <div className="inspector-content">{inspectorTab === "events" ? currentNode?.events.map((event, index) => <button className="inspector-list-row" key={`${event.id ?? index}-${index}`} onClick={() => selectEvent(event)}><span className={`log-indicator ${event.status || "info"}`} /><span><b>{event.message || label(event.status || "event")}</b><small>{event.at ? new Date(event.at).toLocaleTimeString() : "Timestamp unavailable"}</small></span><ChevronRight size={14} /></button>) : <><div className="inspector-overview"><div className="overview-status"><span className={`overview-status-icon ${currentNode?.events.at(-1)?.status || "pending"}`}><ListTree size={15} /></span><div><small>Workflow node</small><b>{label(traceSelection.node)}</b></div></div><div className="inspector-fact"><span>Parent stage</span><b>{groupedStages.find((stage) => stage.id === traceSelection.stageId)?.title}</b></div><div className="inspector-fact"><span>Recorded events</span><b>{currentNode?.events.length || 0}</b></div>{currentNode?.events.at(-1)?.message && <p className="inspector-message">{currentNode.events.at(-1)?.message}</p>}</div>{currentNode?.events.length ? <button className="inspector-disclosure" onClick={() => setInspectorTab("events")}><span>Inspect node events</span><ChevronRight size={14} /></button> : <p className="inspector-muted">No event payload has been recorded for this node.</p>}</>}</div>}
        </div>
      </aside>

      {run.report && <section className={`result-banner ${run.report.target_reached ? "good" : "warning"}`}><span>{run.report.target_reached ? <Check size={15} /> : <CircleAlert size={15} />}</span><div><b>{run.report.target_reached ? `Target reached — ${run.report.requested_target} qualified companies` : `${Math.max(0, run.report.requested_target - run.report.target_shortfall)} of ${run.report.requested_target} companies qualified`}</b><p>{run.report.imported} records imported · {run.report.target_shortfall} target shortfall</p></div></section>}
      {run.status === "awaiting_approval" && <section className="console-pane approval-panel"><div className="pane-heading"><div><p className="eyebrow">HUMAN REVIEW</p><h2>Review your CRM proposal</h2><p>Select the companies you want to import. Nothing is written before your approval.</p></div><Status value="awaiting_approval" /></div><div className="approval-selection"><span>{selected.length} of {approval.length} selected</span><button className="text-button" onClick={() => setSelected(selected.length === approval.length ? [] : approval.map((item) => item.candidate_id))}>{selected.length === approval.length ? "Clear selection" : "Select all"}</button></div><div className="proposal-list">{approval.map((item) => <label className="proposal-row" key={item.candidate_id}><input type="checkbox" checked={selected.includes(item.candidate_id)} onChange={() => toggleApprovalCandidate(item.candidate_id)} /><span className="proposal-check" /><span className="proposal-info"><b>{item.company_name}</b><small>{item.domain}</small></span>{item.source_urls[0] && <a href={item.source_urls[0]} target="_blank" rel="noreferrer">Source <ExternalLink size={11} /></a>}</label>)}</div><div className="approval-actions"><button className="button secondary" disabled={busy} onClick={() => submitApproval(false)}>Reject proposal</button><button className="button primary" disabled={busy || !selected.length} onClick={() => submitApproval(true)}>{busy ? "Submitting…" : `Approve ${selected.length} selected`} <ArrowUpRight size={14} /></button></div></section>}

      {candidates.length > 0 && <section className="console-pane evidence-card"><header className="pane-heading evidence-heading"><div><div className="pane-title-line"><Building2 size={15} /><h2>Company evidence</h2><span className="pane-count">{visibleCandidates.length} / {candidates.length}</span></div><p>{sourceCount} source records · decisions linked to provenance</p></div><div className="evidence-actions"><label className="console-search company-search"><Search size={14} /><input aria-label="Search companies" value={candidateSearch} onChange={(event) => setCandidateSearch(event.target.value)} placeholder="Search companies…" /></label><select aria-label="Filter companies by decision" value={candidateFilter} onChange={(event) => setCandidateFilter(event.target.value)}><option value="all">All decisions</option>{[...new Set(candidates.map((candidate) => candidate.decision))].map((decision) => <option key={decision} value={decision}>{label(decision)}</option>)}</select><select aria-label="Filter companies by website status" value={websiteFilter} onChange={(event) => setWebsiteFilter(event.target.value)}><option value="all">All website statuses</option>{[...new Set(candidates.map((candidate) => candidate.website_status))].map((status) => <option key={status} value={status}>{label(status)}</option>)}</select><button className="quiet-control" onClick={() => setSortConfidence(!sortConfidence)}>{sortConfidence ? "Name order" : "Confidence order"}</button><button className="icon-action" aria-label={allCompaniesExpanded ? "Collapse all company records" : "Expand all company records"} onClick={() => setExpandedCompanies(allCompaniesExpanded ? [] : visibleCandidates.map((candidate) => candidate.candidate_id))}><ChevronDown size={14} className={allCompaniesExpanded ? "open" : ""} /></button><button className="icon-action" aria-label="Export visible companies as CSV" onClick={exportCsv}><ArrowDownToLine size={14} /></button></div></header>
        <div className="evidence-summary"><div><b>{qualifiedCount}</b><span>Qualified</span></div><div><b>{uncertainCount}</b><span>Uncertain</span></div><div><b>{rejectedCount}</b><span>Rejected or duplicate</span></div><div><b>{sourceCount}</b><span>Sources</span></div></div>
        {visibleCandidates.length ? <div className="lead-grid"><AnimatePresence initial={false} mode="popLayout">{visibleCandidates.map((candidate) => {
          const expanded = expandedCompanies.includes(candidate.candidate_id);
          const title = candidate.legal_name || candidate.company_name;
          return <motion.article layout key={candidate.candidate_id} className={`lead-card ${expanded ? "expanded" : "collapsed"}`} initial={reduceMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 5 }} transition={{ duration: animationDuration }}>
            <button className="lead-card-summary" aria-expanded={expanded} onClick={() => toggleCompany(candidate.candidate_id)}><span className="lead-avatar">{candidate.company_name.slice(0, 1).toUpperCase()}</span><span className="lead-summary-copy"><b>{title}</b><span>{candidate.domain || "Website not established"}</span></span><Status value={candidate.decision} /><ChevronDown size={14} className={`tree-chevron ${expanded ? "open" : ""}`} /></button>
            <div className="lead-summary-meta"><span className="confidence-mark"><b>{Math.round(candidate.confidence * 100)}%</b> confidence</span><span>{candidate.source_evidence.length} {candidate.source_evidence.length === 1 ? "source" : "sources"}</span><span className={`website-chip website-${candidate.website_status.replaceAll("_", "-")}`}>{label(candidate.website_status)}</span></div>
            <p className="lead-summary-rationale">{candidate.rationale}</p>
            <AnimatePresence initial={false}>{expanded && <motion.div className="lead-details" initial={reduceMotion ? false : { opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: animationDuration }} style={{ overflow: "hidden" }}><div className="lead-detail-heading"><b>Qualification rationale</b>{candidate.domain && <div className="lead-card-actions"><a href={candidate.website_url} target="_blank" rel="noreferrer" aria-label={`Open ${candidate.domain}`}><ExternalLink size={13} /></a><button className="icon-action" aria-label={`Copy ${candidate.domain}`} onClick={() => void copyDomain(candidate.domain)}>{copiedDomain === candidate.domain ? <Check size={13} /> : <Copy size={13} />}</button></div>}</div><p className="lead-rationale">{candidate.rationale}</p>{candidate.verification_notes.length > 0 && <div className="verification-notes"><b>Verification notes</b><ul>{candidate.verification_notes.map((note, index) => <li key={`${index}-${note}`}>{note}</li>)}</ul></div>}<div className="source-list-heading"><b>Evidence provenance</b><span>{candidate.source_evidence.length} records</span></div>{candidate.source_evidence.length ? candidate.source_evidence.map((source) => <div className="source-box" key={source.evidence_id}><a href={source.source_url} target="_blank" rel="noreferrer">{source.source_url.replace(/^https?:\/\//, "").split("/")[0]} <ExternalLink size={11} /></a><small>{label(source.source_type)} · retrieved {new Date(source.retrieved_at).toLocaleDateString()}</small><p>{source.excerpt}</p></div>) : <p className="no-evidence">No source excerpts were attached to this candidate.</p>}</motion.div>}</AnimatePresence>
          </motion.article>;
        })}</AnimatePresence></div> : <div className="pane-empty evidence-empty"><Search size={17} /><b>No companies match these filters</b><span>Adjust your search or decision filters.</span></div>}
      </section>}
    </div>
  </div>;
}

function ActivityEvent({ event, variant, onInspect, selected, reduceMotion }: { event: Activity; variant: "feed" | "trace"; onInspect: () => void; selected: boolean; reduceMotion: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const timestamp = event.at ? new Date(event.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "Time unavailable";
  const summary = event.message || event.error || "Worker update";
  const status = event.status || "info";
  const detailEntries = Object.entries(event).filter(([key]) => !["id", "node", "status", "message", "at", "error"].includes(key));
  return <motion.div className={`log-entry ${variant} ${expanded ? "expanded" : ""} ${selected ? "selected" : ""}`} initial={reduceMotion ? false : { opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: reduceMotion ? 0 : .16 }}>
    <button className="log-entry-trigger" aria-expanded={expanded} aria-pressed={selected} onClick={() => { setExpanded(!expanded); onInspect(); }}><span className={`log-indicator ${status}`} /><time>{timestamp}</time><b>{label(event.node || "worker")}</b><span className="log-message">{summary}</span><span className="log-status">{label(status)}</span><ChevronDown size={13} className={`tree-chevron ${expanded ? "open" : ""}`} /></button>
    <AnimatePresence initial={false}>{expanded && <motion.div className="log-event-detail" initial={reduceMotion ? false : { opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: reduceMotion ? 0 : .18 }} style={{ overflow: "hidden" }}>{event.error && <p className="event-error">{event.error}</p>}<div className="event-fields"><span><small>Node</small><b>{label(event.node || "worker")}</b></span><span><small>Status</small><b>{label(status)}</b></span><span><small>Timestamp</small><b>{event.at ? new Date(event.at).toLocaleString() : "Not provided"}</b></span>{event.id !== undefined && <span><small>Event ID</small><b>{event.id}</b></span>}</div>{detailEntries.length > 0 && <div className="event-extra-fields">{detailEntries.map(([key, value]) => <div key={key}><small>{label(key)}</small><pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></div>)}</div>}<details className="event-payload"><summary>View full event payload</summary><pre>{JSON.stringify(event, null, 2)}</pre></details></motion.div>}</AnimatePresence>
  </motion.div>;
}
