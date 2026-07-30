import type {
  AssignedTask,
  DashboardResponse,
  StatsResponse,
  SubmitAuditRequest,
} from "../../shared/types";

export type WorkbookKind = "output" | "target";
export type StatsExportKind = "auditors" | "tasks";

/** An API failure that is safe to show to an auditor: status plus a human message. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

const GENERIC_MESSAGES: Record<number, string> = {
  400: "The request was rejected as invalid.",
  401: "Your session has ended. Please sign in again.",
  403: "This email address is not permitted to use the audit tool.",
  404: "The requested item could not be found.",
  500: "The server hit an unexpected error. Please try again.",
};

function fallbackMessage(status: number): string {
  return GENERIC_MESSAGES[status] ?? `Request failed with status ${status}.`;
}

/**
 * Reads the worker's `{ error }` payload. Anything else (HTML error pages, empty
 * bodies, opaque proxy responses) collapses to a generic message so that server
 * internals never reach the UI.
 */
async function readErrorMessage(response: Response): Promise<string> {
  const contentType = response.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "error" in body) {
        const message = (body as { error: unknown }).error;
        if (typeof message === "string" && message.trim().length > 0) return message.trim();
      }
    } catch {
      // fall through to the generic message
    }
  }
  return fallbackMessage(response.status);
}

async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...init });
  } catch {
    throw new ApiError(0, "Could not reach the audit server. Check your connection and retry.");
  }
  if (!response.ok) throw new ApiError(response.status, await readErrorMessage(response));
  return response;
}

async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  return (await response.json()) as T;
}

function jsonBody(payload: unknown): RequestInit {
  return {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

export function login(email: string): Promise<{ email: string }> {
  return apiJson<{ email: string }>("/api/session", {
    method: "POST",
    ...jsonBody({ email }),
  });
}

export function logout(): Promise<{ ok: boolean }> {
  return apiJson<{ ok: boolean }>("/api/session", { method: "DELETE" });
}

/** Resolves to the signed-in email, or `null` when the session cookie is absent or stale. */
export async function fetchSession(): Promise<string | null> {
  try {
    const body = await apiJson<{ email: string }>("/api/me");
    return body.email;
  } catch (caught) {
    if (caught instanceof ApiError && caught.isUnauthorized) return null;
    throw caught;
  }
}

export function fetchDashboard(): Promise<DashboardResponse> {
  return apiJson<DashboardResponse>("/api/dashboard");
}

export function submitAudit(
  taskId: string,
  payload: SubmitAuditRequest,
  expectedEmail: string,
): Promise<AssignedTask> {
  return apiJson<AssignedTask>(`/api/audits/${encodeURIComponent(taskId)}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "X-Auditor-Email": expectedEmail,
    },
    body: JSON.stringify(payload),
  });
}

export function fetchStats(): Promise<StatsResponse> {
  return apiJson<StatsResponse>("/api/stats");
}

export function workbookUrl(taskId: string, kind: WorkbookKind, download = false): string {
  const base = `/api/workbooks/${encodeURIComponent(taskId)}/${kind}`;
  return download ? `${base}?download=1` : base;
}

/** Authenticated workbook bytes for the Mog host policy. */
export async function fetchWorkbookBytes(
  taskId: string,
  kind: WorkbookKind,
): Promise<ArrayBuffer> {
  const response = await apiFetch(workbookUrl(taskId, kind));
  return await response.arrayBuffer();
}

/**
 * Downloads through `fetch` rather than a bare anchor so an expired session
 * surfaces as an in-app error instead of saving a JSON error body to disk.
 */
async function downloadFrom(path: string, filename: string): Promise<void> {
  const response = await apiFetch(path);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = "noopener";
    // `appendChild`/`removeChild` come from `Node`, which both the DOM lib and
    // the Workers globals in this project agree on. The `ParentNode.append` /
    // `ChildNode.remove` mixins do not survive that merge cleanly.
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  } finally {
    // Give the browser a turn to start the download before releasing the blob.
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000);
  }
}

export function downloadWorkbook(taskId: string, kind: WorkbookKind): Promise<void> {
  return downloadFrom(workbookUrl(taskId, kind, true), `${taskId}-${kind}.xlsx`);
}

export function downloadStatsCsv(kind: StatsExportKind): Promise<void> {
  return downloadFrom(`/api/stats/export?kind=${kind}`, `audit-${kind}.csv`);
}
