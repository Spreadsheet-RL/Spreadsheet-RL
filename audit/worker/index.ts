import type {
  AssignedTask,
  AuditSplit,
  DashboardResponse,
  GroundTruthAssessment,
  StatsResponse,
  SubmitAuditRequest,
} from "../shared/types";
import {
  ALLOWED_EMAILS,
  clearSessionCookie,
  hasSameOrigin,
  isAllowedEmail,
  normalizeEmail,
  readSessionEmail,
  sessionCookie,
} from "./auth";
import { toCsv } from "./csv";

const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};

function json(data: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(data), {
    ...init,
    headers: { ...JSON_HEADERS, ...init.headers },
  });
}

function error(status: number, message: string): Response {
  return json({ error: message }, { status });
}

async function parseJson<T>(request: Request): Promise<T | null> {
  const contentType = request.headers.get("Content-Type") ?? "";
  if (!contentType.includes("application/json")) return null;
  try {
    return (await request.json()) as T;
  } catch {
    return null;
  }
}

function requireEmail(request: Request): string | Response {
  return readSessionEmail(request) ?? error(401, "Please enter an approved email address.");
}

function splitFrom(value: string | null): AuditSplit | null {
  return value === "training" || value === "domain" ? value : null;
}

interface TaskRow {
  id: string;
  split: AuditSplit;
  pool_index: number;
  category: string;
  source_path: string;
  instruction: string;
  answer_position: string;
  assignment_order: number;
  ground_truth_assessment: GroundTruthAssessment | null;
  exact_match_reasonable: number | null;
  failure_description: string | null;
  updated_at: string | null;
}

function taskFromRow(row: TaskRow): AssignedTask {
  return {
    id: row.id,
    split: row.split,
    poolIndex: row.pool_index,
    category: row.category,
    sourcePath: row.source_path,
    instruction: row.instruction,
    answerPosition: row.answer_position,
    assignmentOrder: row.assignment_order,
    audit:
      row.ground_truth_assessment === null
        ? null
        : {
            groundTruthAssessment: row.ground_truth_assessment,
            exactMatchReasonable:
              row.exact_match_reasonable === null ? null : row.exact_match_reasonable === 1,
            failureDescription: row.failure_description ?? "",
            updatedAt: row.updated_at ?? "",
          },
  };
}

async function dashboard(env: Env, email: string): Promise<DashboardResponse> {
  const result = await env.DB.prepare(
    `SELECT t.id, t.split, t.pool_index, t.category, t.source_path,
            t.instruction, t.answer_position, a.assignment_order,
            u.ground_truth_assessment,
            u.exact_match_reasonable,
            u.failure_description, u.updated_at
       FROM assignments a
       JOIN tasks t ON t.id = a.task_id
       LEFT JOIN audits u ON u.email = a.email AND u.task_id = a.task_id
      WHERE a.email = ?
      ORDER BY a.assignment_order`,
  )
    .bind(email)
    .all<TaskRow>();

  const tasks = result.results.map(taskFromRow);
  const progress = (["training", "domain"] as const).map((split) => {
    const splitTasks = tasks.filter((task) => task.split === split);
    const completed = splitTasks.filter((task) => task.audit !== null).length;
    return { split, assigned: splitTasks.length, completed, remaining: splitTasks.length - completed };
  });
  return { email, progress, tasks };
}

async function isAssigned(env: Env, email: string, taskId: string): Promise<boolean> {
  const row = await env.DB.prepare(
    "SELECT 1 AS assigned FROM assignments WHERE email = ? AND task_id = ? LIMIT 1",
  )
    .bind(email, taskId)
    .first<{ assigned: number }>();
  return row?.assigned === 1;
}

async function handleLogin(request: Request): Promise<Response> {
  if (!hasSameOrigin(request)) return error(403, "Cross-origin request rejected.");
  const body = await parseJson<{ email?: unknown }>(request);
  const email = normalizeEmail(body?.email);
  if (!isAllowedEmail(email)) return error(403, "This email address is not on the audit whitelist.");
  return json({ email }, { headers: { "Set-Cookie": sessionCookie(email) } });
}

async function handleDashboard(env: Env, email: string, url: URL): Promise<Response> {
  const payload = await dashboard(env, email);
  const split = splitFrom(url.searchParams.get("split"));
  const status = url.searchParams.get("status");
  if (split) payload.tasks = payload.tasks.filter((task) => task.split === split);
  if (status === "pending") payload.tasks = payload.tasks.filter((task) => task.audit === null);
  if (status === "completed") payload.tasks = payload.tasks.filter((task) => task.audit !== null);
  return json(payload);
}

async function handleTask(env: Env, email: string, taskId: string): Promise<Response> {
  const result = await env.DB.prepare(
    `SELECT t.id, t.split, t.pool_index, t.category, t.source_path,
            t.instruction, t.answer_position, a.assignment_order,
            u.ground_truth_assessment,
            u.exact_match_reasonable,
            u.failure_description, u.updated_at
       FROM assignments a
       JOIN tasks t ON t.id = a.task_id
       LEFT JOIN audits u ON u.email = a.email AND u.task_id = a.task_id
      WHERE a.email = ? AND a.task_id = ?`,
  )
    .bind(email, taskId)
    .first<TaskRow>();
  return result ? json(taskFromRow(result)) : error(404, "Task not found in this auditor's assignment.");
}

async function handleWorkbook(
  request: Request,
  env: Env,
  email: string,
  taskId: string,
  kind: string,
): Promise<Response> {
  if (kind !== "output" && kind !== "target") return error(404, "Workbook not found.");
  if (!(await isAssigned(env, email, taskId))) return error(404, "Task not found in this auditor's assignment.");

  const row = await env.DB.prepare(`SELECT ${kind}_key AS object_key FROM tasks WHERE id = ?`)
    .bind(taskId)
    .first<{ object_key: string }>();
  if (!row) return error(404, "Workbook not found.");

  const objectUrl = new URL(`/${row.object_key}`, request.url);
  const object = await env.ASSETS.fetch(new Request(objectUrl, { method: "GET" }));
  if (!object.ok || !object.body) return error(404, "Workbook object is missing.");

  const disposition = new URL(request.url).searchParams.get("download") === "1" ? "attachment" : "inline";
  const headers = new Headers(object.headers);
  headers.set(
    "Content-Type",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  );
  headers.set("Content-Disposition", `${disposition}; filename="${kind}.xlsx"`);
  headers.set("Cache-Control", "private, no-store");
  headers.set("X-Content-Type-Options", "nosniff");
  return new Response(object.body, { status: object.status, headers });
}

async function handleMogWasm(request: Request, env: Env): Promise<Response> {
  const assetUrl = new URL("/assets/mog-wasm.wasm.gz", request.url);
  const object = await env.ASSETS.fetch(new Request(assetUrl, { method: "GET" }));
  if (!object.ok || !object.body) return error(503, "The spreadsheet viewer engine is unavailable.");

  const headers = new Headers(object.headers);
  headers.set("Content-Type", "application/wasm");
  headers.set("Cache-Control", "private, max-age=86400");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.delete("Content-Encoding");
  headers.delete("Content-Length");
  headers.delete("ETag");
  const body = request.method === "HEAD" ? null : object.body.pipeThrough(new DecompressionStream("gzip"));
  return new Response(body, {
    status: object.status,
    headers,
  });
}

export function validAudit(body: SubmitAuditRequest, split: AuditSplit): string | null {
  if (
    body.groundTruthAssessment !== "yes" &&
    body.groundTruthAssessment !== "almost" &&
    body.groundTruthAssessment !== "no"
  ) {
    return "Ground-truth correctness is required.";
  }
  if (split === "domain" && typeof body.exactMatchReasonable !== "boolean") {
    return "Exact-match suitability is required for Domain tasks.";
  }
  if (split === "training" && body.exactMatchReasonable !== null) {
    return "Exact-match suitability must be null for training tasks.";
  }
  if (typeof body.failureDescription !== "string") {
    return "Failure description must be text.";
  }
  const description = body.failureDescription.trim();
  if (
    (body.groundTruthAssessment !== "yes" || body.exactMatchReasonable === false) &&
    description.length < 5
  ) {
    return "Describe the problem when answering Almost correct or No.";
  }
  if (description.length > 4000) return "The failure description is too long.";
  return null;
}

async function handleSubmitAudit(
  request: Request,
  env: Env,
  email: string,
  taskId: string,
): Promise<Response> {
  if (!hasSameOrigin(request)) return error(403, "Cross-origin request rejected.");
  if (normalizeEmail(request.headers.get("X-Auditor-Email")) !== email) {
    return error(409, "Your audit session changed. Sign in again before saving.");
  }
  const assignment = await env.DB.prepare(
    `SELECT t.split FROM assignments a JOIN tasks t ON t.id = a.task_id
      WHERE a.email = ? AND a.task_id = ?`,
  )
    .bind(email, taskId)
    .first<{ split: AuditSplit }>();
  if (!assignment) return error(404, "Task not found in this auditor's assignment.");

  const body = await parseJson<SubmitAuditRequest>(request);
  if (!body) return error(400, "A JSON request body is required.");
  const validationError = validAudit(body, assignment.split);
  if (validationError) return error(400, validationError);

  const description = body.failureDescription.trim();
  await env.DB.prepare(
    `INSERT INTO audits (
       email, task_id, ground_truth_assessment, exact_match_reasonable,
       failure_description, updated_at
     ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
     ON CONFLICT (email, task_id) DO UPDATE SET
       ground_truth_assessment = excluded.ground_truth_assessment,
       exact_match_reasonable = excluded.exact_match_reasonable,
       failure_description = excluded.failure_description,
       updated_at = CURRENT_TIMESTAMP`,
  )
    .bind(
      email,
      taskId,
      body.groundTruthAssessment,
      assignment.split === "domain" ? (body.exactMatchReasonable ? 1 : 0) : null,
      description,
    )
    .run();
  return handleTask(env, email, taskId);
}

async function loadStats(env: Env): Promise<StatsResponse> {
  const [auditorRows, taskRows] = await env.DB.batch([
    env.DB.prepare(
      `SELECT a.email, t.split, COUNT(*) AS assigned,
              SUM(CASE WHEN u.task_id IS NOT NULL THEN 1 ELSE 0 END) AS completed
         FROM assignments a
         JOIN tasks t ON t.id = a.task_id
         LEFT JOIN audits u ON u.email = a.email AND u.task_id = a.task_id
        GROUP BY a.email, t.split
        ORDER BY a.email, t.split`,
    ),
    env.DB.prepare(
      `SELECT t.id AS task_id, t.split, t.pool_index, t.category,
              COUNT(a.email) AS assigned,
              COUNT(u.email) AS completed,
              SUM(CASE WHEN u.ground_truth_assessment = 'yes' THEN 1 ELSE 0 END) AS gt_yes,
              SUM(CASE WHEN u.ground_truth_assessment = 'almost' THEN 1 ELSE 0 END) AS gt_almost,
              SUM(CASE WHEN u.ground_truth_assessment = 'no' THEN 1 ELSE 0 END) AS gt_no,
              SUM(CASE WHEN u.exact_match_reasonable = 1 THEN 1 ELSE 0 END) AS exact_yes,
              SUM(CASE WHEN u.exact_match_reasonable = 0 THEN 1 ELSE 0 END) AS exact_no
         FROM tasks t
         JOIN assignments a ON a.task_id = t.id
         LEFT JOIN audits u ON u.email = a.email AND u.task_id = a.task_id
        GROUP BY t.id
        ORDER BY t.split, t.pool_index`,
    ),
  ]);

  const auditorStats = (auditorRows.results as Array<Record<string, unknown>>).map((row) => {
    const assigned = Number(row.assigned);
    const completed = Number(row.completed);
    return {
      email: String(row.email),
      split: row.split as AuditSplit,
      assigned,
      completed,
      remaining: assigned - completed,
    };
  });
  const auditorStatsByKey = new Map(
    auditorStats.map((stat) => [`${stat.email}:${stat.split}`, stat] as const),
  );
  const auditors = [...ALLOWED_EMAILS].flatMap((email) =>
    (["training", "domain"] as const).map(
      (split) =>
        auditorStatsByKey.get(`${email}:${split}`) ?? {
          email,
          split,
          assigned: 0,
          completed: 0,
          remaining: 0,
        },
    ),
  );
  const tasks = (taskRows.results as Array<Record<string, unknown>>).map((row) => {
    const completed = Number(row.completed);
    const gtYes = Number(row.gt_yes);
    const gtAlmost = Number(row.gt_almost);
    const gtNo = Number(row.gt_no);
    const exactYes = Number(row.exact_yes);
    const exactNo = Number(row.exact_no);
    const exactTotal = exactYes + exactNo;
    return {
      taskId: String(row.task_id),
      split: row.split as AuditSplit,
      poolIndex: Number(row.pool_index),
      category: String(row.category),
      assigned: Number(row.assigned),
      completed,
      groundTruthYes: gtYes,
      groundTruthAlmost: gtAlmost,
      groundTruthNo: gtNo,
      groundTruthYesRate: completed === 0 ? null : gtYes / completed,
      groundTruthAlmostRate: completed === 0 ? null : gtAlmost / completed,
      groundTruthNoRate: completed === 0 ? null : gtNo / completed,
      exactMatchYes: row.split === "domain" ? exactYes : null,
      exactMatchNo: row.split === "domain" ? exactNo : null,
      exactMatchAverage: row.split === "domain" && exactTotal > 0 ? exactYes / exactTotal : null,
    };
  });
  return { auditors, tasks, generatedAt: new Date().toISOString() };
}

async function handleStatsExport(env: Env, url: URL): Promise<Response> {
  const stats = await loadStats(env);
  const kind = url.searchParams.get("kind") === "tasks" ? "tasks" : "auditors";
  const rows = kind === "tasks" ? stats.tasks : stats.auditors;
  const columns =
    kind === "tasks"
      ? [
          "taskId",
          "split",
          "poolIndex",
          "category",
          "assigned",
          "completed",
          "groundTruthYes",
          "groundTruthAlmost",
          "groundTruthNo",
          "groundTruthYesRate",
          "groundTruthAlmostRate",
          "groundTruthNoRate",
          "exactMatchYes",
          "exactMatchNo",
          "exactMatchAverage",
        ]
      : ["email", "split", "assigned", "completed", "remaining"];
  return new Response(toCsv(rows, columns), {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="audit-${kind}.csv"`,
      "Cache-Control": "no-store",
    },
  });
}

async function handleRequest(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;

  if (path === "/api/health" && request.method === "GET") return json({ ok: true });
  if (path === "/api/session" && request.method === "POST") return handleLogin(request);
  if (path === "/api/session" && request.method === "DELETE") {
    if (!hasSameOrigin(request)) return error(403, "Cross-origin request rejected.");
    return json({ ok: true }, { headers: { "Set-Cookie": clearSessionCookie() } });
  }

  const directWorkbookMatch = path.match(/^\/workbooks\/([^/]+)\/(output|target)\.xlsx$/);
  if (directWorkbookMatch && request.method === "GET") {
    const session = requireEmail(request);
    if (session instanceof Response) return session;
    return handleWorkbook(
      request,
      env,
      session,
      decodeURIComponent(directWorkbookMatch[1]),
      directWorkbookMatch[2],
    );
  }

  if (!path.startsWith("/api/")) return env.ASSETS.fetch(request);
  const session = requireEmail(request);
  if (session instanceof Response) return session;

  if (path === "/api/me" && request.method === "GET") return json({ email: session });
  if (path === "/api/mog-wasm" && (request.method === "GET" || request.method === "HEAD")) {
    return handleMogWasm(request, env);
  }
  if (path === "/api/dashboard" && request.method === "GET") return handleDashboard(env, session, url);
  if (path === "/api/stats" && request.method === "GET") return json(await loadStats(env));
  if (path === "/api/stats/export" && request.method === "GET") return handleStatsExport(env, url);

  const taskMatch = path.match(/^\/api\/tasks\/([^/]+)$/);
  if (taskMatch && request.method === "GET") return handleTask(env, session, decodeURIComponent(taskMatch[1]));

  const workbookMatch = path.match(/^\/api\/workbooks\/([^/]+)\/(output|target)$/);
  if (workbookMatch && request.method === "GET") {
    return handleWorkbook(
      request,
      env,
      session,
      decodeURIComponent(workbookMatch[1]),
      workbookMatch[2],
    );
  }

  const auditMatch = path.match(/^\/api\/audits\/([^/]+)$/);
  if (auditMatch && request.method === "PUT") {
    return handleSubmitAudit(request, env, session, decodeURIComponent(auditMatch[1]));
  }
  return error(404, "API route not found.");
}

export default {
  async fetch(request, env): Promise<Response> {
    try {
      return await handleRequest(request, env);
    } catch (caught) {
      console.error(
        JSON.stringify({
          event: "request_error",
          method: request.method,
          path: new URL(request.url).pathname,
          error: caught instanceof Error ? caught.message : String(caught),
        }),
      );
      return error(500, "Unexpected server error.");
    }
  },
} satisfies ExportedHandler<Env>;
