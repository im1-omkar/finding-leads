"use client";

import { type FormEvent, type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

type Candidate = {
  candidate_id: string;
  company_name: string;
  legal_name?: string | null;
  domain: string;
  website_url: string;
  decision: string;
  confidence: number;
  rationale: string;
  website_status: string;
  import_status: string;
  source_evidence: { evidence_id: string; source_url: string; source_type: string; excerpt: string; retrieved_at: string }[];
  verification_notes: string[];
};

type RunData = {
  run_id: string;
  request_id: string;
  status: string;
  request: { industry: string; region: string; target_leads: number };
  approval: null | { proposal_hash: string; candidate_count: number; changes: { candidate_id: string; company_name: string; domain: string; source_urls: string[] }[] };
  candidates: Candidate[];
  report: null | { requested_target: number; target_reached: boolean; target_shortfall: number; imported: number; candidates: Candidate[]; notes: string[] };
  error: string | null;
};

const API = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
const statusLabels: Record<string, string> = {
  queued: "Queued", running: "Researching", awaiting_approval: "Needs review", completed: "Complete", failed: "Failed",
};

function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export default function Home() {
  const [target, setTarget] = useState(50);
  const [mode, setMode] = useState("mock");
  const [apiReady, setApiReady] = useState(false);
  const [industry, setIndustry] = useState("AI software");
  const [region, setRegion] = useState("Bengaluru");
  const [minimum, setMinimum] = useState("");
  const [maximum, setMaximum] = useState("");
  const [criteria, setCriteria] = useState("B2B AI software company");
  const [reviewer, setReviewer] = useState("sales-reviewer");
  const [run, setRun] = useState<RunData | null>(null);
  const [events, setEvents] = useState<{ node?: string; status?: string; message?: string }[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refreshRun = useCallback(async (runId: string) => {
    try {
      const response = await fetch(`${API}/runs/${runId}`, { cache: "no-store" });
      if (!response.ok) return;
      setRun(await response.json());
    } catch {
      setApiReady(false);
      setError("Could not reach the backend. Check that the services are running.");
    }
  }, []);

  useEffect(() => {
    fetch(`${API}/config`, { cache: "no-store" })
      .then((response) => response.json())
      .then((config) => { setTarget(config.number_of_leads || 50); setMode(config.mock_mode ? "mock" : "live"); setApiReady(true); })
      .catch(() => { setApiReady(false); setError("Backend configuration is unavailable. Start the project with Docker Compose."); });
  }, []);

  useEffect(() => {
    const savedRunId = new URLSearchParams(window.location.search).get("run_id");
    if (savedRunId) void refreshRun(savedRunId);
  }, [refreshRun]);

  useEffect(() => {
    if (!run?.run_id) return;
    const source = new EventSource(`${API}/runs/${run.run_id}/events`);
    const onProgress = (raw: Event) => {
      const message = JSON.parse((raw as MessageEvent).data);
      setEvents((previous) => [...previous.slice(-39), message]);
      void refreshRun(run.run_id);
    };
    source.addEventListener("progress", onProgress);
    source.addEventListener("end", () => { source.close(); void refreshRun(run.run_id); });
    const timer = window.setInterval(() => void refreshRun(run.run_id), 2500);
    return () => { source.close(); window.clearInterval(timer); };
  }, [run?.run_id, run?.status, refreshRun]);

  const candidates = run?.report?.candidates || run?.candidates || [];
  const approvalChanges = run?.approval?.changes || [];
  const selectedCount = selected.length;
  const counts = useMemo(() => ({
    qualified: candidates.filter((candidate) => candidate.decision === "qualified").length,
    rejected: candidates.filter((candidate) => candidate.decision === "rejected").length,
    uncertain: candidates.filter((candidate) => candidate.decision === "uncertain").length,
    inactive: candidates.filter((candidate) => candidate.website_status === "inactive").length,
  }), [candidates]);

  async function startResearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setError(""); setEvents([]); setSelected([]); setRun(null);
    const requestId = `lead-run-${crypto.randomUUID()}`;
    try {
      const response = await fetch(`${API}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: requestId,
          industry,
          region,
          company_size: { min_employees: minimum ? Number(minimum) : null, max_employees: maximum ? Number(maximum) : null },
          qualification_criteria: criteria.split("\n").map((line) => line.trim()).filter(Boolean),
          target_leads: target,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not start research.");
      window.history.replaceState(null, "", `?run_id=${encodeURIComponent(result.run_id)}`);
      await refreshRun(result.run_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start research.");
    } finally {
      setBusy(false);
    }
  }

  async function submitApproval(approved: boolean) {
    if (!run?.approval) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(`${API}/runs/${run.run_id}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          approved,
          proposal_hash: run.approval.proposal_hash,
          approved_candidate_ids: approved ? selected : [],
          reviewer_id: reviewer,
          comment: approved ? "Reviewed the lead proposal." : "Rejected the proposal.",
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not submit review.");
      await refreshRun(run.run_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not submit review.");
    } finally {
      setBusy(false);
    }
  }

  function toggleCandidate(candidateId: string) {
    setSelected((current) => current.includes(candidateId) ? current.filter((id) => id !== candidateId) : [...current, candidateId]);
  }

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#top"><span className="brand-mark">L</span><span>Lead Research<span className="muted"> / CRM Loader</span></span></a>
        <div className="top-meta"><Badge tone={mode === "mock" ? "blue" : "green"}>{mode} mode</Badge><span className={`service-dot ${apiReady ? "" : "offline"}`} />{apiReady ? "API connected" : "API unavailable"}</div>
      </header>

      <div className="page-grid" id="top">
        <section className="main-column">
          <div className="intro">
            <p className="eyebrow">SALES RESEARCH WORKSPACE</p>
            <h1>Find the right companies.<br /><span>Keep every decision checkable.</span></h1>
            <p className="intro-copy">Set your target market. The agent researches public sources, checks each website, and prepares a CRM proposal for your approval.</p>
          </div>

          <section className="panel request-panel">
            <div className="panel-heading"><div><p className="eyebrow">01 / RESEARCH REQUEST</p><h2>Define your lead list</h2></div><span className="locked-target"><strong>{target}</strong><span>leads target<br />from .env</span></span></div>
            <form onSubmit={startResearch}>
              <div className="form-grid">
                <label>Industry<input value={industry} onChange={(event) => setIndustry(event.target.value)} minLength={2} required placeholder="e.g. AI software" /></label>
                <label>City or region<input value={region} onChange={(event) => setRegion(event.target.value)} minLength={2} required placeholder="e.g. Bengaluru" /></label>
                <label>Minimum employees <span className="optional">optional</span><input inputMode="numeric" type="number" min="1" value={minimum} onChange={(event) => setMinimum(event.target.value)} placeholder="No minimum" /></label>
                <label>Maximum employees <span className="optional">optional</span><input inputMode="numeric" type="number" min="1" value={maximum} onChange={(event) => setMaximum(event.target.value)} placeholder="No maximum" /></label>
              </div>
              <label className="criteria-label">Qualification criteria <span className="optional">one per line</span><textarea value={criteria} onChange={(event) => setCriteria(event.target.value)} required rows={3} placeholder="B2B software company&#10;Has a public product website" /></label>
              {error && <div className="error-box" role="alert">{error}</div>}
              <div className="form-footer"><p>Research runs use public company sources and stop at the configured target or the search budget.</p><button className="primary-button" disabled={busy || !industry.trim() || !region.trim() || !criteria.trim()}>{busy && !run ? "Starting…" : "Start research run"}<span aria-hidden="true">↗</span></button></div>
            </form>
          </section>

          {run && <section className="panel results-panel">
            <div className="panel-heading result-heading">
              <div><p className="eyebrow">02 / LIVE RUN · {run.run_id.slice(0, 8)}</p><h2>{statusLabels[run.status] || run.status}</h2><p className="subheading">{run.request.industry} · {run.request.region} · target {target}</p></div>
              <Badge tone={run.status === "completed" ? "green" : run.status === "failed" ? "red" : run.status === "awaiting_approval" ? "amber" : "blue"}>{run.status.replaceAll("_", " ")}</Badge>
            </div>
            <div className="stat-grid">
              <div><strong>{counts.qualified}</strong><span>Qualified</span></div><div><strong>{counts.rejected}</strong><span>Rejected</span></div><div><strong>{counts.uncertain}</strong><span>Uncertain</span></div><div><strong>{counts.inactive}</strong><span>Inactive</span></div>
            </div>
            {run.error && <div className="error-box" role="alert">{run.error}</div>}
            {run.report && <div className={`target-result ${run.report.target_reached ? "success" : "warning"}`}><strong>{run.report.target_reached ? `Target reached: ${target} qualified leads found` : `${target - run.report.target_shortfall} of ${target} qualified leads found`}</strong><span>{run.report.target_shortfall ? `${run.report.target_shortfall} more could not be substantiated within this run’s search budget.` : `${run.report.imported} CRM records verified after review.`}</span></div>}
            {run.status === "running" || run.status === "queued" ? <div className="progress-box"><div className="progress-track"><span /></div><p>{events.at(-1)?.message || "Preparing research…"}</p>{events.length > 0 && <div className="event-list">{events.slice(-4).map((item, index) => <span key={`${item.node}-${index}`}>{item.message}</span>)}</div>}</div> : null}

            {run.status === "awaiting_approval" && run.approval && <div className="approval-box">
              <div className="approval-title"><div><p className="eyebrow">03 / HUMAN REVIEW</p><h3>Review the CRM proposal</h3></div><Badge tone="amber">{approvalChanges.length} proposed</Badge></div>
              <p className="approval-copy">Select the records to import. No CRM write happens until you approve this exact proposal.</p>
              <label className="reviewer-field">Reviewer name<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} required /></label>
              <div className="approval-list">{approvalChanges.map((change) => <label className="approval-item" key={change.candidate_id}><input type="checkbox" checked={selected.includes(change.candidate_id)} onChange={() => toggleCandidate(change.candidate_id)} /><span className="approval-company"><strong>{change.company_name}</strong><span>{change.domain}</span>{change.source_urls[0] && <a href={change.source_urls[0]} target="_blank" rel="noreferrer">Open source ↗</a>}</span></label>)}</div>
              <div className="approval-actions"><button className="secondary-button" disabled={busy} onClick={() => void submitApproval(false)}>Reject proposal</button><button className="primary-button" disabled={busy || selectedCount === 0 || !reviewer.trim()} onClick={() => void submitApproval(true)}>Approve {selectedCount} selected</button></div>
            </div>}

            {candidates.length > 0 && <div className="candidate-section"><div className="section-title"><h3>Lead evidence</h3><span>{candidates.length} candidates · source details retained</span></div><div className="candidate-list">{candidates.map((candidate) => <article className="candidate-card" key={candidate.candidate_id}>
              <div className="candidate-top"><div className="candidate-avatar">{candidate.company_name.slice(0, 1).toUpperCase()}</div><div className="candidate-name"><h4>{candidate.legal_name || candidate.company_name}</h4><span>{candidate.domain || "Official website not established"}</span></div><Badge tone={candidate.decision === "qualified" ? "green" : candidate.decision === "rejected" ? "red" : candidate.decision === "duplicate" ? "neutral" : "amber"}>{candidate.decision}</Badge></div>
              <p className="candidate-rationale">{candidate.rationale}</p>
              <div className="candidate-meta"><Badge tone={candidate.website_status === "active" ? "green" : candidate.website_status === "inactive" ? "red" : "neutral"}>website {candidate.website_status}</Badge><span>{candidate.source_evidence.length} source{candidate.source_evidence.length === 1 ? "" : "s"}</span><span>{Math.round(candidate.confidence * 100)}% confidence</span></div>
              {candidate.source_evidence.slice(0, 3).map((source) => <div className="evidence-row" key={source.evidence_id}><a href={source.source_url} target="_blank" rel="noreferrer">{source.source_url.replace(/^https?:\/\//, "")}</a><span>{source.excerpt}</span></div>)}
              {candidate.verification_notes.map((note, index) => <p className="candidate-note" key={index}>{note}</p>)}
            </article>)}</div></div>}
          </section>}
        </section>

        <aside className="side-column">
          <section className="side-card target-card"><p className="eyebrow">YOUR TARGET</p><div className="target-number">{target}<span>leads</span></div><p>Configured by <code>NUMBER_OF_LEADS</code> in the backend environment. The browser form cannot change this target.</p><div className="target-line"><span>Target progress</span><strong>{run?.report ? `${Math.max(0, target - run.report.target_shortfall)} / ${target}` : run?.approval ? `${run.approval.candidate_count} / ${target}` : "—"}</strong></div><div className="mini-track"><span style={{ width: `${run?.report ? Math.min(100, ((target - run.report.target_shortfall) / target) * 100) : run?.approval ? Math.min(100, (run.approval.candidate_count / target) * 100) : 0}%` }} /></div></section>
          <section className="side-card"><p className="eyebrow">RUN PIPELINE</p><div className="pipeline"><div className="pipeline-step active"><i>1</i><span><strong>Discover</strong><small>Search and collect sources</small></span></div><div className="pipeline-step active"><i>2</i><span><strong>Research & qualify</strong><small>Check evidence and company fit</small></span></div><div className={`pipeline-step ${run?.status === "awaiting_approval" ? "current" : "active"}`}><i>3</i><span><strong>Review proposal</strong><small>Human approval before CRM write</small></span></div><div className={`pipeline-step ${run?.status === "completed" ? "current" : ""}`}><i>4</i><span><strong>Verify import</strong><small>Read back and compare records</small></span></div></div></section>
          <section className="side-card scenarios"><p className="eyebrow">OFFICIAL SCENARIOS</p><div><Badge tone="blue">TC1</Badge><span>Unique niche leads with sources</span></div><div><Badge tone="blue">TC2</Badge><span>Two domains merge to one company</span></div><div><Badge tone="blue">TC3</Badge><span>Unresolving website marked inactive</span></div><div><Badge tone="blue">TC4</Badge><span>Login-gated contact skipped safely</span></div><p className="scenario-foot">Scenario fixtures are covered by the backend test suite.</p></section>
          {run?.report && <section className="side-card"><p className="eyebrow">RUN SUMMARY</p><div className="summary-row"><span>Verified CRM records</span><strong>{run.report.imported}</strong></div><div className="summary-row"><span>Target shortfall</span><strong>{run.report.target_shortfall}</strong></div><div className="summary-row"><span>Final state</span><Badge tone={run.status === "completed" ? "green" : "amber"}>{run.status}</Badge></div>{run.report.notes.map((note, index) => <p className="scenario-foot" key={index}>{note}</p>)}</section>}
        </aside>
      </div>
      <footer><span>Lead Research & CRM Loader</span><span>Evidence first · approval required · every import verified</span></footer>
    </main>
  );
}
