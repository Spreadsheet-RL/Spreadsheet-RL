import type { SplitProgress } from "../../../shared/types";
import { SPLIT_LABELS, SPLIT_ORDER } from "../format";

interface ProgressSummaryProps {
  progress: SplitProgress[];
}

export function ProgressSummary({ progress }: ProgressSummaryProps) {
  const bySplit = new Map(progress.map((entry) => [entry.split, entry] as const));

  return (
    <section className="progress" aria-label="Your progress">
      {SPLIT_ORDER.map((split) => {
        const entry = bySplit.get(split) ?? {
          split,
          assigned: 0,
          completed: 0,
          remaining: 0,
        };
        const label = SPLIT_LABELS[split];
        return (
          <div key={split} className="progress__item">
            <div className="progress__row">
              <h2 className="progress__label">{label}</h2>
              <p className="progress__count">
                <strong>{entry.completed}</strong> / {entry.assigned} completed
              </p>
            </div>
            <progress
              className="progress__bar"
              max={entry.assigned || 1}
              value={entry.completed}
              aria-label={`${label}: ${entry.completed} of ${entry.assigned} completed`}
            />
            <p className="progress__remaining">{entry.remaining} remaining</p>
          </div>
        );
      })}
    </section>
  );
}
