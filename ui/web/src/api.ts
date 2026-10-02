import axios from "axios";
import type {
  AuditRequest,
  AuditResponse,
  BankEntitySummary,
  ChangeDiffRequest,
  ChangeDiffResponse,
  LineageResponse,
  ObligationRelationsRequest,
  ObligationRelationsResponse,
  ReviewActionResponse,
  ReviewQueueItem,
  ReviewSubmission,
} from "./types";

// src/api/main.py runs on :$VITE_API_PORT (make api, default 8055 -- see
// ui/web/.env); this SPA runs on :5173 (make ui) -- CORSMiddleware in
// main.py explicitly allows this origin.
//
// Read from an env var, not hardcoded, because this project has already
// twice hit a dev port becoming permanently unusable on a specific
// machine -- port 8000 first, then port 8010 too (both silently swallowed
// every request without ever reaching the real backend process; the
// intercepting process's PID wasn't resolvable by Get-Process or
// Get-CimInstance, consistent with corporate endpoint-security/proxy
// software on a managed laptop). If it happens a third time, only
// ui/web/.env.local and Makefile's API_PORT need to change -- not this file.
const API_PORT = import.meta.env.VITE_API_PORT ?? "8055";
const API_BASE_URL = `http://localhost:${API_PORT}`;

const client = axios.create({
  baseURL: API_BASE_URL,
  headers: { "Content-Type": "application/json" },
});

// Every src/api/main.py error response is {"detail": "<stringified root
// cause>"} (e.g. `HTTPException(detail=f"Pipeline run failed: {exc}")`) --
// axios's own Error.message for a failed request is just generic HTTP
// status text ("Request failed with status code 500"), which throws away
// that detail entirely. This interceptor re-throws a plain Error carrying
// the backend's actual detail string as .message, so every catch block in
// App.tsx that does `err instanceof Error ? err.message : ...` (unchanged)
// automatically surfaces the real root cause instead of the status text.
client.interceptors.response.use(
  (response) => response,
  (error) => {
    const detail = error?.response?.data?.detail;
    if (typeof detail === "string" && detail.length > 0) {
      return Promise.reject(new Error(detail));
    }
    return Promise.reject(error);
  }
);

export async function runAudit(payload: AuditRequest): Promise<AuditResponse> {
  const response = await client.post<AuditResponse>("/api/v1/audit", payload);
  return response.data;
}

export async function getLineage(runId: string): Promise<LineageResponse> {
  const response = await client.get<LineageResponse>(`/api/v1/lineage/${runId}`);
  return response.data;
}

export async function diffChanges(payload: ChangeDiffRequest): Promise<ChangeDiffResponse> {
  const response = await client.post<ChangeDiffResponse>("/api/v1/changes/diff", payload);
  return response.data;
}

export async function listBankEntities(): Promise<BankEntitySummary[]> {
  const response = await client.get<BankEntitySummary[]>("/api/v1/bank_entities");
  return response.data;
}

export async function findObligationRelations(
  payload: ObligationRelationsRequest
): Promise<ObligationRelationsResponse> {
  const response = await client.post<ObligationRelationsResponse>("/api/v1/obligations/relations", payload);
  return response.data;
}

export async function listReviewQueue(): Promise<ReviewQueueItem[]> {
  const response = await client.get<ReviewQueueItem[]>("/api/v1/review");
  return response.data;
}

export async function submitReview(payload: ReviewSubmission): Promise<ReviewActionResponse> {
  const response = await client.post<ReviewActionResponse>("/api/v1/review", payload);
  return response.data;
}
