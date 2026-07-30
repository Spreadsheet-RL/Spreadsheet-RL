import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { AuditorStat, StatsResponse, TaskStat } from "../../../shared/types";
import { downloadStatsCsv, fetchStats, type StatsExportKind } from "../api";
import { EM_DASH, SPLIT_LABELS, errorMessage, formatCount, formatFraction, formatTimestamp } from "../format";
import { Spinner } from "./Spinner";

const PAGE_SIZE = 20;

interface Counts {
  assigned: number;
  completed: number;
}

interface AuditorRow {
  email: string;
  training: Counts;
  domain: Counts;
  total: Counts;
}

const ZERO: Counts = { assigned: 0, completed: 0 };

function add(a: Counts, b: Counts): Counts {
  return { assigned: a.assigned + b.assigned, completed: a.completed + b.completed };
}

function groupAuditors(auditors: AuditorStat[]): AuditorRow[] {
  const rows = new Map<string, AuditorRow>();
  for (const stat of auditors) {
    const row = rows.get(stat.email) ?? {
      email: stat.email,
      training: ZERO,
      domain: ZERO,
      total: ZERO,
    };
    const counts: Counts = { assigned: stat.assigned, completed: stat.completed };
    rows.set(stat.email, {
      email: row.email,
      training: stat.split === "training" ? add(row.training, counts) : row.training,
      domain: stat.split === "domain" ? add(row.domain, counts) : row.domain,
      total: add(row.total, counts),
    });
  }
  return [...rows.values()].sort((a, b) => a.email.localeCompare(b.email));
}

function matchesQuery(task: TaskStat, query: string): boolean {
  if (query.length === 0) return true;
  return (
    task.taskId.toLowerCase().includes(query) ||
    task.category.toLowerCase().includes(query) ||
    SPLIT_LABELS[task.split].toLowerCase().includes(query)
  );
}

interface StatsDialogProps {
  open: boolean;
  onClose: () => void;
  onApiError: (caught: unknown) => void;
}

export function StatsDialog({ open, onClose, onApiError }: StatsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const searchId = useId();
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const [exporting, setExporting] = useState<StatsExportKind | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setError(null);
    fetchStats()
      .then((response) => {
        if (active) setStats(response);
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setError(errorMessage(caught));
        onApiError(caught);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, onApiError]);

  const auditorRows = useMemo(() => (stats ? groupAuditors(stats.auditors) : []), [stats]);

  const filteredTasks = useMemo(() => {
    if (!stats) return [];
    const normalized = query.trim().toLowerCase();
    return stats.tasks.filter((task) => matchesQuery(task, normalized));
  }, [stats, query]);

  const pageCount = Math.max(1, Math.ceil(filteredTasks.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const visibleTasks = filteredTasks.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  async function handleExport(kind: StatsExportKind) {
    setExporting(kind);
    setExportError(null);
    try {
      await downloadStatsCsv(kind);
    } catch (caught) {
      setExportError(errorMessage(caught));
      onApiError(caught);
    } finally {
      setExporting(null);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className="stats-dialog"
      aria-labelledby="stats-heading"
      onClose={onClose}
    >
      <div className="stats-dialog__inner">
        <header className="stats-dialog__header">
          <h2 id="stats-heading">Audit statistics</h2>
          <button type="button" className="button button--small" onClick={onClose}>
            Close
          </button>
        </header>

        <div className="stats-dialog__exports">
          <button
            type="button"
            className="button button--primary"
            onClick={() => void handleExport("auditors")}
            disabled={exporting !== null}
          >
            {exporting === "auditors" ? "Preparing…" : "Download auditor progress (CSV)"}
          </button>
          <button
            type="button"
            className="button button--primary"
            onClick={() => void handleExport("tasks")}
            disabled={exporting !== null}
          >
            {exporting === "tasks" ? "Preparing…" : "Download per-task responses (CSV)"}
          </button>
        </div>

        {exportError !== null && (
          <p className="callout callout--error" role="alert">
            {exportError}
          </p>
        )}

        {loading && <Spinner label="Loading statistics…" />}

        {error !== null && (
          <p className="callout callout--error" role="alert">
            {error}
          </p>
        )}

        {stats && !loading && (
          <>
            <section aria-labelledby="stats-auditors-heading">
              <h3 id="stats-auditors-heading">Progress by auditor</h3>
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Auditor</th>
                      <th scope="col">Training</th>
                      <th scope="col">Domain</th>
                      <th scope="col">Total</th>
                      <th scope="col">Remaining</th>
                    </tr>
                  </thead>
                  <tbody>
                    {auditorRows.map((row) => (
                      <tr key={row.email}>
                        <th scope="row">{row.email}</th>
                        <td>
                          {row.training.completed} / {row.training.assigned}
                        </td>
                        <td>
                          {row.domain.completed} / {row.domain.assigned}
                        </td>
                        <td>
                          {row.total.completed} / {row.total.assigned}
                        </td>
                        <td>{row.total.assigned - row.total.completed}</td>
                      </tr>
                    ))}
                    {auditorRows.length === 0 && (
                      <tr>
                        <td colSpan={5}>No auditors have been assigned tasks yet.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>

            <section aria-labelledby="stats-tasks-heading">
              <h3 id="stats-tasks-heading">Responses by task</h3>
              <p className="stats-dialog__note">
                Ground-truth responses are reported as separate Yes, Almost correct, and No
                proportions; they are not converted into a numeric score. Exact-match figures are
                the fraction of completed responses answering Yes and apply to Domain tasks only.
                Tasks with no completed responses show {EM_DASH} rather than zero.
              </p>

              <div className="field field--inline">
                <label htmlFor={searchId}>Search tasks</label>
                <input
                  id={searchId}
                  type="search"
                  placeholder="Task ID, category, or split"
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    setPage(0);
                  }}
                />
              </div>

              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Task</th>
                      <th scope="col">Split</th>
                      <th scope="col">Category</th>
                      <th scope="col">Completed</th>
                      <th scope="col">Ground truth correct</th>
                      <th scope="col">Exact match reasonable</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleTasks.map((task) => (
                      <tr key={task.taskId}>
                        <th scope="row">{task.taskId}</th>
                        <td>{SPLIT_LABELS[task.split]}</td>
                        <td>{task.category}</td>
                        <td>
                          {task.completed} / {task.assigned}
                        </td>
                        <td>
                          <span className="table__detail">
                            Yes: {formatFraction(task.groundTruthYesRate)} ({task.groundTruthYes})
                          </span>
                          <span className="table__detail">
                            Almost correct: {formatFraction(task.groundTruthAlmostRate)} (
                            {task.groundTruthAlmost})
                          </span>
                          <span className="table__detail">
                            No: {formatFraction(task.groundTruthNoRate)} ({task.groundTruthNo})
                          </span>
                        </td>
                        <td>
                          {formatFraction(task.exactMatchAverage)}
                          <span className="table__detail">
                            {task.split === "domain"
                              ? `${formatCount(task.exactMatchYes)} yes / ${formatCount(task.exactMatchNo)} no`
                              : "not asked"}
                          </span>
                        </td>
                      </tr>
                    ))}
                    {visibleTasks.length === 0 && (
                      <tr>
                        <td colSpan={6}>No tasks match this search.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div className="pager">
                <button
                  type="button"
                  className="button button--small"
                  onClick={() => setPage((value) => Math.max(0, value - 1))}
                  disabled={safePage === 0}
                >
                  ← Previous
                </button>
                <p role="status">
                  Page {safePage + 1} of {pageCount} · {filteredTasks.length} tasks
                </p>
                <button
                  type="button"
                  className="button button--small"
                  onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}
                  disabled={safePage >= pageCount - 1}
                >
                  Next →
                </button>
              </div>
            </section>

            <p className="stats-dialog__generated">
              Generated {formatTimestamp(stats.generatedAt)}
            </p>
          </>
        )}
      </div>
    </dialog>
  );
}
