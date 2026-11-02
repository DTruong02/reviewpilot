"use client";

import { useState } from "react";
import { api, ApiError, type ValidationPlan, type ValidationRun } from "@/lib/api";

export function ValidationPlanner({ findingId }: { findingId: string }) {
  const [token, setToken] = useState("");
  const [plan, setPlan] = useState<ValidationPlan | null>(null);
  const [run, setRun] = useState<ValidationRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function perform(action: () => Promise<ValidationPlan | ValidationRun>) {
    setBusy(true);
    setError("");
    api.setValidationToken(token);
    try {
      const result = await action();
      if ("plan_hash" in result) setPlan(result);
      else {
        setRun(result);
        setPlan(current => current ? { ...current, status: result.status } : current);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Validation planning request failed.");
    } finally {
      setBusy(false);
    }
  }

  return <details className="validation-planner">
    <summary>Validation plan <span className="muted">(execution blocked)</span></summary>
    <div className="validation-content">
      <p><b>No code is run.</b> This feature stores a bounded plan. The run request records a blocked, inconclusive result because no verified isolated executor is available.</p>
      <label>Single-operator API token <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} /></label>
      <small>The token is kept in this browser tab session only.</small>
      <button type="button" onClick={() => { api.clearValidationToken(); setToken(""); }}>Clear session token</button>
      <div className="validation-actions">
        <button disabled={busy || !token.trim()} onClick={() => void perform(() => api.validationPlan(findingId))}>Propose plan</button>
        {plan && <>
          <button disabled={busy} onClick={() => void perform(() => api.validationPlanDetail(plan.id))}>Refresh details</button>
          <button disabled={busy || plan.approved} onClick={() => void perform(() => api.approveValidationPlan(plan.id))}>{plan.approved ? "Approved" : "Approve exact plan"}</button>
          <button disabled={busy || !plan.approved || plan.status === "blocked"} onClick={() => void perform(() => api.checkValidationGate(plan.id))}>{plan.status === "blocked" ? "Execution blocked" : "Check execution gate"}</button>
        </>}
        {run && <button disabled={busy} onClick={() => void perform(() => api.validationRunDetail(run.id))}>Refresh blocked result</button>}
      </div>
      {error && <p role="alert" className="validation-error">{error}</p>}
      {plan && <div className="validation-state">
        <p><b>Status:</b> {plan.status} · <b>Approval:</b> {plan.approved ? "approved" : "not approved"}</p>
        <p><b>Repository:</b> {plan.repository} · <b>Review SHA:</b> <code>{plan.commit_sha}</code></p>
        <p><b>Runner template:</b> <code>{plan.plan.runner_template}</code></p>
        <p><b>Expected behavior:</b> {plan.plan.expected_behavior}</p>
        <p><b>Test input:</b> {plan.plan.test_input}</p>
        <p><b>Expected observation:</b> {plan.plan.expected_observation}</p>
        <p><b>Canonical plan hash:</b> <code>{plan.plan_hash}</code></p>
      </div>}
      {run && <div className="validation-state" role="status"><b>Validation:</b> {run.status} / {run.conclusion}. {run.observation}</div>}
    </div>
  </details>;
}
