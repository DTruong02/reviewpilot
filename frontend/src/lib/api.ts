export type Page<T> = { items: T[]; limit: number; offset: number; total: number };
export type Repository = { id: string; owner: string; name: string; full_name: string; private: boolean; updated_at: string };
export type PullRequest = { id: string; number: number; title: string; state: string; html_url: string; head_sha: string; repository_full_name: string; repository_id: string; updated_at: string; latest_review_status?: string | null };
export type ReviewSummary = { id: string; status: string; created_at: string; completed_at?: string | null; commit_sha: string; model_name?: string | null; error_code?: string | null };
export type EvidenceReference = { symbol_id: string; file_path: string; start_line: number; end_line: number };
export type ContextProvenance = { file_path: string; symbol_id: string; start_line: number; end_line: number; commit_sha: string; retrieval_reason: "changed_symbol" | "imported_file" | "importer_file" | "semantic_similarity"; score: number };
export type Finding = { id: string; severity: "high" | "medium" | "low"; title: string; category: string; explanation: string; suggested_fix?: string | null; evidence_refs?: EvidenceReference[] | null; file_path: string; line_number: number | null; feedback?: "helpful" | "false_positive" | null };
export type Review = ReviewSummary & { pull_request_id: string; findings: Finding[]; input_tokens?: number | null; output_tokens?: number | null; duration_ms?: number | null; context_mode: "diff_only" | "diff_only_fallback" | "context_aware"; context_fallback_reason?: string | null; context_metadata?: ContextProvenance[] | null };
export class ApiError extends Error { constructor(message: string, public status?: number) { super(message); } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`/api${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers }, cache: "no-store" }); }
  catch { throw new ApiError("The ReviewPilot API is unreachable."); }
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body: { detail?: unknown } = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (body.detail && typeof body.detail === "object" && "message" in body.detail && typeof body.detail.message === "string") message = body.detail.message;
    } catch { /* use safe status */ }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}
export const api = {
  repositories: (offset = 0, limit = 12) => request<Page<Repository>>(`/repositories?offset=${offset}&limit=${limit}`),
  pullRequests: (repoId: string, offset = 0, limit = 12) => request<Page<PullRequest>>(`/repositories/${repoId}/pull-requests?offset=${offset}&limit=${limit}`),
  pullRequest: (id: string) => request<PullRequest>(`/pull-requests/${id}`),
  reviews: (prId: string, offset = 0, limit = 12) => request<Page<ReviewSummary>>(`/pull-requests/${prId}/reviews?offset=${offset}&limit=${limit}`),
  review: (id: string) => request<Review>(`/reviews/${id}`),
  feedback: (findingId: string, feedback: "helpful" | "false_positive") => request(`/findings/${findingId}/feedback`, { method: "PATCH", body: JSON.stringify({ feedback }) }),
};
