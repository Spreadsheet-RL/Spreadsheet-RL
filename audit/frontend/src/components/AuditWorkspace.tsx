import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type {
  AssignedTask,
  DashboardResponse,
  SplitProgress,
  SubmitAuditRequest,
} from "../../../shared/types";
import { fetchDashboard, logout, submitAudit } from "../api";
import { SPLIT_ORDER, errorMessage } from "../format";
import { useUnsavedChangesPrompt } from "../hooks";
import { AppHeader } from "./AppHeader";
import { AuditForm } from "./AuditForm";
import { ProgressSummary } from "./ProgressSummary";
import { Spinner } from "./Spinner";
import { StatsDialog } from "./StatsDialog";
import { TaskDetails } from "./TaskDetails";
import { TaskNavigator, type SplitFilter, type StatusFilter } from "./TaskNavigator";
import { WorkbookPanels } from "./WorkbookPanels";

const DISCARD_PROMPT =
  "You have unsaved changes to this audit. Leave without saving?";

/** Progress is recomputed locally after each save so the header stays live. */
function computeProgress(tasks: AssignedTask[]): SplitProgress[] {
  return SPLIT_ORDER.map((split) => {
    const subset = tasks.filter((task) => task.split === split);
    const completed = subset.filter((task) => task.audit !== null).length;
    return { split, assigned: subset.length, completed, remaining: subset.length - completed };
  });
}

function matchesSplit(task: AssignedTask, splitFilter: SplitFilter): boolean {
  return splitFilter === "all" || task.split === splitFilter;
}

function matchesStatus(task: AssignedTask, statusFilter: StatusFilter): boolean {
  if (statusFilter === "pending") return task.audit === null;
  if (statusFilter === "completed") return task.audit !== null;
  return true;
}

function matchesFilters(
  task: AssignedTask,
  splitFilter: SplitFilter,
  statusFilter: StatusFilter,
): boolean {
  return matchesSplit(task, splitFilter) && matchesStatus(task, statusFilter);
}

/** Landing task for a freshly filtered list: first pending, else first overall. */
function pickTask(candidates: AssignedTask[]): AssignedTask | null {
  const pending = candidates.find((task) => task.audit === null);
  if (pending) return pending;
  return candidates.length > 0 ? candidates[0] : null;
}

/**
 * First pending task after the current one in assignment order, wrapping to the
 * start. Stays inside the selected split so a run through one pool is not
 * interrupted by the other.
 */
function findNextPending(
  tasks: AssignedTask[],
  currentTask: AssignedTask | null,
  splitFilter: SplitFilter,
): AssignedTask | null {
  const candidates = tasks
    .filter((task) => task.audit === null && task.id !== currentTask?.id)
    .filter((task) => splitFilter === "all" || task.split === splitFilter)
    .sort((a, b) => a.assignmentOrder - b.assignmentOrder);
  if (candidates.length === 0) return null;
  const order = currentTask?.assignmentOrder ?? -1;
  return candidates.find((task) => task.assignmentOrder > order) ?? candidates[0];
}

interface AuditWorkspaceProps {
  email: string;
  onSignedOut: () => void;
  onApiError: (caught: unknown) => void;
}

export function AuditWorkspace({ email, onSignedOut, onApiError }: AuditWorkspaceProps) {
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [splitFilter, setSplitFilter] = useState<SplitFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [unsaved, setUnsaved] = useState(false);
  const [statsOpen, setStatsOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  // Purely presentational, and intentionally not persisted: every fresh page
  // starts with the task panel open.
  const [taskPanelOpen, setTaskPanelOpen] = useState(true);
  const taskPanelId = useId();

  useUnsavedChangesPrompt(unsaved);

  const mainRef = useRef<HTMLElement>(null);

  // Lets the loader below choose a fallback that obeys the active filters
  // without re-fetching the dashboard every time a filter changes.
  const filtersRef = useRef({ split: splitFilter, status: statusFilter });
  useEffect(() => {
    filtersRef.current = { split: splitFilter, status: statusFilter };
  }, [splitFilter, statusFilter]);

  useEffect(() => {
    let active = true;
    setLoadError(null);
    fetchDashboard()
      .then((response) => {
        if (!active) return;
        if (response.email !== email) {
          onSignedOut();
          return;
        }
        setDashboard(response);
        setSelectedTaskId((current) => {
          if (current && response.tasks.some((task) => task.id === current)) return current;
          const { split, status } = filtersRef.current;
          const candidates = response.tasks.filter((task) => matchesFilters(task, split, status));
          // On first load the filters are still "all", so this is the spec's
          // "default to the first pending task".
          return pickTask(candidates)?.id ?? null;
        });
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setLoadError(errorMessage(caught));
        onApiError(caught);
      });
    return () => {
      active = false;
    };
  }, [email, reloadNonce, onApiError, onSignedOut]);

  const tasks = dashboard?.tasks ?? [];
  const selectedTask = tasks.find((task) => task.id === selectedTaskId) ?? null;
  const tasksRef = useRef(tasks);
  const selectedTaskIdRef = useRef(selectedTaskId);
  const splitFilterRef = useRef(splitFilter);
  tasksRef.current = tasks;
  selectedTaskIdRef.current = selectedTaskId;
  splitFilterRef.current = splitFilter;

  // Saving a task under the "Pending" filter would otherwise drop it from the
  // list the instant it was answered. Keep the open task visible until the
  // auditor moves on — but only ever as a *status* exemption. A task from a
  // different split must never survive an explicit split filter.
  const visibleTasks = useMemo(() => {
    const filtered = tasks.filter((task) => matchesFilters(task, splitFilter, statusFilter));
    if (selectedTask === null) return filtered;
    const keepSelected =
      matchesSplit(selectedTask, splitFilter) &&
      !filtered.some((task) => task.id === selectedTask.id);
    return keepSelected
      ? [...filtered, selectedTask].sort((a, b) => a.assignmentOrder - b.assignmentOrder)
      : filtered;
  }, [tasks, splitFilter, statusFilter, selectedTask]);

  const nextPending = useMemo(
    () => findNextPending(tasks, selectedTask, splitFilter),
    [tasks, selectedTask, splitFilter],
  );

  const confirmDiscard = useCallback(
    () => !unsaved || window.confirm(DISCARD_PROMPT),
    [unsaved],
  );

  /**
   * Brings the task pane into view after a selection change. On narrow screens
   * the navigator sits above the pane and its list can run to 50vh, so scrolling
   * the window to the top would leave the auditor looking at the header instead
   * of the task they just opened.
   *
   * Deliberately captures nothing: the element comes from a ref and the motion
   * preference is read inside the frame callback, so this can never act on a
   * stale node or a stale setting. The frame delay lets React commit the new
   * selection first, and a `null` ref (unmounted in the meantime) is a no-op.
   */
  const revealTaskPane = useCallback(() => {
    requestAnimationFrame(() => {
      const pane = mainRef.current;
      if (!pane) return;
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      pane.focus({ preventScroll: true });
      pane.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
  }, []);

  const selectTask = useCallback(
    (taskId: string) => {
      if (taskId === selectedTaskId) return;
      if (!confirmDiscard()) return;
      setUnsaved(false);
      setSelectedTaskId(taskId);
      revealTaskPane();
    },
    [selectedTaskId, confirmDiscard, revealTaskPane],
  );

  /**
   * Applies an explicit filter change. The open task is kept only when it still
   * matches both new filters; otherwise the selection moves into the newly
   * filtered list. Because that discards unsaved edits, it is confirmed first —
   * and a cancelled confirmation leaves the filters exactly as they were.
   */
  const applyFilters = useCallback(
    (nextSplit: SplitFilter, nextStatus: StatusFilter) => {
      if (nextSplit === splitFilter && nextStatus === statusFilter) return;

      const keepSelection =
        selectedTask !== null && matchesFilters(selectedTask, nextSplit, nextStatus);
      if (!keepSelection && selectedTask !== null && !confirmDiscard()) return;

      setSplitFilter(nextSplit);
      setStatusFilter(nextStatus);
      // A preserved selection is not a navigation, so leave the scroll position
      // where the auditor put it.
      if (keepSelection) return;

      const replacement = pickTask(
        tasks.filter((task) => matchesFilters(task, nextSplit, nextStatus)),
      );
      setUnsaved(false);
      setSelectedTaskId(replacement?.id ?? null);
      if (replacement) revealTaskPane();
    },
    [tasks, selectedTask, splitFilter, statusFilter, confirmDiscard, revealTaskPane],
  );

  const changeSplitFilter = useCallback(
    (next: SplitFilter) => applyFilters(next, statusFilter),
    [applyFilters, statusFilter],
  );

  const changeStatusFilter = useCallback(
    (next: StatusFilter) => applyFilters(splitFilter, next),
    [applyFilters, splitFilter],
  );

  const handleSubmit = useCallback(
    async (payload: SubmitAuditRequest, advance: boolean) => {
      if (!selectedTaskId) return;
      const submittedTaskId = selectedTaskId;
      const updated = await submitAudit(submittedTaskId, payload, email).catch((caught: unknown) => {
        onApiError(caught);
        throw caught;
      });

      tasksRef.current = tasksRef.current.map((task) =>
        task.id === updated.id ? updated : task,
      );
      setDashboard((current) => {
        if (!current) return current;
        const nextTasks = current.tasks.map((task) => (task.id === updated.id ? updated : task));
        tasksRef.current = nextTasks;
        return { ...current, tasks: nextTasks, progress: computeProgress(nextTasks) };
      });

      if (selectedTaskIdRef.current !== submittedTaskId) return;
      setUnsaved(false);

      if (advance) {
        const target = findNextPending(tasksRef.current, updated, splitFilterRef.current);
        if (target) {
          setSelectedTaskId(target.id);
          revealTaskPane();
        }
      }
    },
    [email, selectedTaskId, onApiError, revealTaskPane],
  );

  const handleSignOut = useCallback(async () => {
    if (!confirmDiscard()) return;
    setSigningOut(true);
    try {
      await logout();
    } catch {
      // The cookie may already be gone; drop the local session either way.
    } finally {
      setSigningOut(false);
      onSignedOut();
    }
  }, [confirmDiscard, onSignedOut]);

  return (
    <div className="app">
      <a className="skip-link" href="#audit-main">
        Skip to the current task
      </a>

      <AppHeader
        email={email}
        onOpenStats={() => setStatsOpen(true)}
        onSignOut={() => void handleSignOut()}
        signingOut={signingOut}
      />

      {loadError !== null && (
        <div className="callout callout--error" role="alert">
          <p>{loadError}</p>
          <button
            type="button"
            className="button button--small"
            onClick={() => setReloadNonce((value) => value + 1)}
          >
            Retry
          </button>
        </div>
      )}

      {dashboard === null && loadError === null && (
        <div className="boot" aria-busy="true">
          <Spinner label="Loading your assigned tasks…" />
        </div>
      )}

      {dashboard !== null && (
        <>
          <ProgressSummary progress={dashboard.progress} />

          <div className={`workspace${taskPanelOpen ? "" : " workspace--collapsed"}`}>
            <aside className="workspace__sidebar" aria-label="Task panel">
              <button
                type="button"
                className="button button--small sidebar-toggle"
                aria-expanded={taskPanelOpen}
                aria-controls={taskPanelId}
                title={taskPanelOpen ? undefined : "Show task panel"}
                onClick={() => setTaskPanelOpen((open) => !open)}
              >
                <span className="sidebar-toggle__chevron" aria-hidden="true">
                  {taskPanelOpen ? "‹" : "›"}
                </span>
                {/*
                  Kept in the DOM in every state. On the collapsed desktop rail
                  it is clipped rather than removed, so the button still exposes
                  the full "Show task panel" accessible name.
                */}
                <span className="sidebar-toggle__label">
                  {taskPanelOpen ? "Hide task panel" : "Show task panel"}
                </span>
              </button>

              {/*
                `hidden` rather than unmounting: the navigator keeps its place in
                the tree (no state churn, no re-render of the task list), while
                `display: none` takes its filters and task buttons out of both
                the focus order and the accessibility tree.
              */}
              <div id={taskPanelId} hidden={!taskPanelOpen}>
                <TaskNavigator
                  tasks={visibleTasks}
                  selectedTaskId={selectedTaskId}
                  splitFilter={splitFilter}
                  statusFilter={statusFilter}
                  onSplitFilterChange={changeSplitFilter}
                  onStatusFilterChange={changeStatusFilter}
                  onSelect={selectTask}
                />
              </div>
            </aside>

            <main id="audit-main" ref={mainRef} className="workspace__main" tabIndex={-1}>
              {selectedTask === null ? (
                <p className="callout">
                  {tasks.length === 0
                    ? "No tasks are assigned to this account yet."
                    : visibleTasks.length === 0
                      ? "No tasks match these filters. Change the split or status filter to continue."
                      : "Select a task from the list to begin."}
                </p>
              ) : (
                <>
                  <TaskDetails task={selectedTask} />
                  <WorkbookPanels taskId={selectedTask.id} onApiError={onApiError} />
                  <AuditForm
                    key={selectedTask.id}
                    task={selectedTask}
                    hasNextPending={nextPending !== null}
                    onUnsavedChange={setUnsaved}
                    onSubmit={handleSubmit}
                  />
                </>
              )}
            </main>
          </div>
        </>
      )}

      <StatsDialog
        open={statsOpen}
        onClose={() => setStatsOpen(false)}
        onApiError={onApiError}
      />
    </div>
  );
}
