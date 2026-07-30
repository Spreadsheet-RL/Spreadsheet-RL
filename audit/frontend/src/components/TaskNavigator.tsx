import { useId } from "react";
import type { AssignedTask } from "../../../shared/types";

export type SplitFilter = "all" | "training" | "domain";
export type StatusFilter = "all" | "pending" | "completed";

const SPLIT_OPTIONS: ReadonlyArray<{ value: SplitFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "training", label: "Training" },
  { value: "domain", label: "Domain" },
];

const STATUS_OPTIONS: ReadonlyArray<{ value: StatusFilter; label: string }> = [
  { value: "pending", label: "Pending" },
  { value: "completed", label: "Completed" },
  { value: "all", label: "All" },
];

interface SegmentedProps<T extends string> {
  legend: string;
  name: string;
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (value: T) => void;
}

function Segmented<T extends string>({ legend, name, options, value, onChange }: SegmentedProps<T>) {
  return (
    <fieldset className="segmented">
      <legend className="segmented__legend">{legend}</legend>
      <div className="segmented__options">
        {options.map((option) => (
          <label key={option.value} className="segmented__option">
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

interface TaskNavigatorProps {
  tasks: AssignedTask[];
  selectedTaskId: string | null;
  splitFilter: SplitFilter;
  statusFilter: StatusFilter;
  onSplitFilterChange: (value: SplitFilter) => void;
  onStatusFilterChange: (value: StatusFilter) => void;
  onSelect: (taskId: string) => void;
}

export function TaskNavigator({
  tasks,
  selectedTaskId,
  splitFilter,
  statusFilter,
  onSplitFilterChange,
  onStatusFilterChange,
  onSelect,
}: TaskNavigatorProps) {
  const groupId = useId();
  const selectedIndex = tasks.findIndex((task) => task.id === selectedTaskId);
  const previous = selectedIndex > 0 ? tasks[selectedIndex - 1] : undefined;
  const next =
    selectedIndex >= 0 && selectedIndex < tasks.length - 1 ? tasks[selectedIndex + 1] : undefined;

  return (
    <nav className="navigator" aria-label="Task navigation">
      <div className="navigator__filters">
        <Segmented
          legend="Split"
          name={`${groupId}-split`}
          options={SPLIT_OPTIONS}
          value={splitFilter}
          onChange={onSplitFilterChange}
        />
        <Segmented
          legend="Status"
          name={`${groupId}-status`}
          options={STATUS_OPTIONS}
          value={statusFilter}
          onChange={onStatusFilterChange}
        />
      </div>

      <div className="navigator__steps">
        <button
          type="button"
          className="button button--small"
          disabled={!previous}
          onClick={() => previous && onSelect(previous.id)}
        >
          ← Previous
        </button>
        <p className="navigator__position" role="status">
          {selectedIndex >= 0
            ? `${selectedIndex + 1} of ${tasks.length}`
            : `${tasks.length} task${tasks.length === 1 ? "" : "s"}`}
        </p>
        <button
          type="button"
          className="button button--small"
          disabled={!next}
          onClick={() => next && onSelect(next.id)}
        >
          Next →
        </button>
      </div>

      {tasks.length === 0 ? (
        <p className="navigator__empty">No tasks match these filters.</p>
      ) : (
        <ul className="task-list">
          {tasks.map((task) => {
            const done = task.audit !== null;
            const selected = task.id === selectedTaskId;
            return (
              <li key={task.id}>
                <button
                  type="button"
                  className={`task-list__item${selected ? " task-list__item--selected" : ""}`}
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelect(task.id)}
                >
                  <span className="task-list__id">{task.id}</span>
                  <span className="task-list__meta">
                    <span className={`badge ${done ? "badge--done" : "badge--pending"}`}>
                      {done ? "Completed" : "Pending"}
                    </span>
                  </span>
                  <span className="task-list__category">{task.category}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </nav>
  );
}
