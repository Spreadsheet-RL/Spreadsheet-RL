import type { AssignedTask } from "../../../shared/types";
import { SPLIT_LABELS } from "../format";

interface TaskDetailsProps {
  task: AssignedTask;
}

export function TaskDetails({ task }: TaskDetailsProps) {
  const done = task.audit !== null;

  return (
    <section className="card task-details" aria-labelledby="task-details-heading">
      <div className="task-details__head">
        <h2 id="task-details-heading" className="task-details__id">
          {task.id}
        </h2>
        <p className="task-details__badges">
          <span className="badge badge--muted">{SPLIT_LABELS[task.split]}</span>
          <span className={`badge ${done ? "badge--done" : "badge--pending"}`}>
            {done ? "Completed" : "Pending"}
          </span>
        </p>
      </div>

      <h3 className="task-details__section">Instruction</h3>
      <p className="task-details__instruction">{task.instruction}</p>

      <dl className="task-details__grid">
        <div>
          <dt>Category</dt>
          <dd>{task.category}</dd>
        </div>
        <div>
          <dt>Answer position</dt>
          <dd>
            <code>{task.answerPosition}</code>
          </dd>
        </div>
        <div className="task-details__wide">
          <dt>Source path</dt>
          <dd>
            <code>{task.sourcePath}</code>
          </dd>
        </div>
      </dl>
    </section>
  );
}
