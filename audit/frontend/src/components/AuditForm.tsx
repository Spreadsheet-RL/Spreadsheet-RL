import { useEffect, useId, useState, type FormEvent } from "react";
import type {
  AssignedTask,
  GroundTruthAssessment,
  SubmitAuditRequest,
} from "../../../shared/types";
import { errorMessage, formatTimestamp } from "../format";

const MIN_DESCRIPTION_LENGTH = 5;
const MAX_DESCRIPTION_LENGTH = 4000;

const Q1 = "Does the ground-truth spreadsheet correctly satisfy the instruction?";
const Q2 =
  "Is exact all-cell matching at the answer position a reasonable correctness criterion for this task?";
const Q2_HELP =
  "Consider whether a semantically correct solution could use different formulas, layout, formatting, or equivalent values.";

interface Draft {
  groundTruthAssessment: GroundTruthAssessment | null;
  exactMatchReasonable: boolean | null;
  failureDescription: string;
}

function savedDraft(task: AssignedTask): Draft {
  if (!task.audit) {
    return { groundTruthAssessment: null, exactMatchReasonable: null, failureDescription: "" };
  }
  return {
    groundTruthAssessment: task.audit.groundTruthAssessment,
    exactMatchReasonable: task.audit.exactMatchReasonable,
    failureDescription: task.audit.failureDescription,
  };
}

function isDirty(draft: Draft, saved: Draft): boolean {
  return (
    draft.groundTruthAssessment !== saved.groundTruthAssessment ||
    draft.exactMatchReasonable !== saved.exactMatchReasonable ||
    draft.failureDescription.trim() !== saved.failureDescription.trim()
  );
}

interface GroundTruthQuestionProps {
  name: string;
  value: GroundTruthAssessment | null;
  onChange: (value: GroundTruthAssessment) => void;
  disabled: boolean;
}

const GROUND_TRUTH_OPTIONS: ReadonlyArray<{
  value: GroundTruthAssessment;
  label: string;
}> = [
  { value: "yes", label: "Yes" },
  { value: "almost", label: "Almost correct" },
  { value: "no", label: "No" },
];

function GroundTruthQuestion({ name, value, onChange, disabled }: GroundTruthQuestionProps) {
  return (
    <fieldset className="question">
      <legend className="question__legend">
        {Q1} <span className="required" aria-hidden="true">*</span>
      </legend>
      <div className="question__options">
        {GROUND_TRUTH_OPTIONS.map((option) => (
          <label key={option.value} className="radio">
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
              disabled={disabled}
              required
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

interface YesNoQuestionProps {
  name: string;
  legend: string;
  help?: string;
  value: boolean | null;
  onChange: (value: boolean) => void;
  disabled: boolean;
}

function YesNoQuestion({ name, legend, help, value, onChange, disabled }: YesNoQuestionProps) {
  const helpId = useId();
  return (
    <fieldset className="question" aria-describedby={help ? helpId : undefined}>
      <legend className="question__legend">
        {legend} <span className="required" aria-hidden="true">*</span>
      </legend>
      {help && (
        <p id={helpId} className="question__help">
          {help}
        </p>
      )}
      <div className="question__options">
        {[true, false].map((option) => (
          <label key={String(option)} className="radio">
            <input
              type="radio"
              name={name}
              value={option ? "yes" : "no"}
              checked={value === option}
              onChange={() => onChange(option)}
              disabled={disabled}
              required
            />
            <span>{option ? "Yes" : "No"}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

interface AuditFormProps {
  task: AssignedTask;
  hasNextPending: boolean;
  onUnsavedChange: (unsaved: boolean) => void;
  onSubmit: (payload: SubmitAuditRequest, advance: boolean) => Promise<void>;
}

export function AuditForm({ task, hasNextPending, onUnsavedChange, onSubmit }: AuditFormProps) {
  const descriptionId = useId();
  const descriptionHelpId = useId();
  const [draft, setDraft] = useState<Draft>(() => savedDraft(task));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showErrors, setShowErrors] = useState(false);

  const isDomain = task.split === "domain";
  const saved = savedDraft(task);
  const dirty = isDirty(draft, saved);

  useEffect(() => {
    onUnsavedChange(dirty);
  }, [dirty, onUnsavedChange]);

  const needsDescription =
    (draft.groundTruthAssessment !== null && draft.groundTruthAssessment !== "yes") ||
    (isDomain && draft.exactMatchReasonable === false);
  const description = draft.failureDescription.trim();

  const missingQ1 = draft.groundTruthAssessment === null;
  const missingQ2 = isDomain && draft.exactMatchReasonable === null;
  const descriptionTooShort = needsDescription && description.length < MIN_DESCRIPTION_LENGTH;
  const descriptionTooLong = description.length > MAX_DESCRIPTION_LENGTH;
  const canSubmit = !missingQ1 && !missingQ2 && !descriptionTooShort && !descriptionTooLong;

  function update(patch: Partial<Draft>) {
    setDraft((previous) => ({ ...previous, ...patch }));
    setError(null);
  }

  async function save(advance: boolean) {
    if (saving) return;
    setShowErrors(true);
    const groundTruthAssessment = draft.groundTruthAssessment;
    if (!canSubmit || groundTruthAssessment === null) return;

    setSaving(true);
    setError(null);
    try {
      await onSubmit(
        {
          groundTruthAssessment,
          exactMatchReasonable: isDomain ? draft.exactMatchReasonable : null,
          failureDescription: description,
        },
        advance,
      );
      setShowErrors(false);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void save(false);
  }

  return (
    <section className="card audit-form" aria-labelledby="audit-form-heading">
      <div className="audit-form__head">
        <h2 id="audit-form-heading">Your assessment</h2>
        {task.audit && (
          <p className="audit-form__saved">
            Saved {formatTimestamp(task.audit.updatedAt)}
            {dirty && <span className="badge badge--warning">Unsaved changes</span>}
          </p>
        )}
      </div>

      <form onSubmit={handleSubmit} noValidate>
        <GroundTruthQuestion
          name={`ground-truth-correct-${task.id}`}
          value={draft.groundTruthAssessment}
          onChange={(value) => update({ groundTruthAssessment: value })}
          disabled={saving}
        />
        {showErrors && missingQ1 && (
          <p className="field__error" role="alert">
            Please answer this question.
          </p>
        )}

        {isDomain && (
          <>
            <YesNoQuestion
              name={`exact-match-reasonable-${task.id}`}
              legend={Q2}
              help={Q2_HELP}
              value={draft.exactMatchReasonable}
              onChange={(value) => update({ exactMatchReasonable: value })}
              disabled={saving}
            />
            {showErrors && missingQ2 && (
              <p className="field__error" role="alert">
                Please answer this question.
              </p>
            )}
          </>
        )}

        <div className="field">
          <label htmlFor={descriptionId}>
            {needsDescription ? "Describe the problem" : "Optional note"}
            {needsDescription && (
              <span className="required" aria-hidden="true">
                {" "}
                *
              </span>
            )}
          </label>
          <p id={descriptionHelpId} className="field__hint">
            {needsDescription
              ? "Give the concrete failure you found, or the evaluation rule that would judge this task better."
              : "Anything worth flagging to the research team. Leave blank if there is nothing to add."}
          </p>
          <textarea
            id={descriptionId}
            name="failureDescription"
            rows={4}
            maxLength={MAX_DESCRIPTION_LENGTH}
            value={draft.failureDescription}
            aria-describedby={descriptionHelpId}
            aria-required={needsDescription}
            aria-invalid={showErrors && descriptionTooShort}
            disabled={saving}
            onChange={(event) => update({ failureDescription: event.target.value })}
          />
          {showErrors && descriptionTooShort && (
            <p className="field__error" role="alert">
              Please write at least {MIN_DESCRIPTION_LENGTH} characters describing the problem.
            </p>
          )}
        </div>

        {error !== null && (
          <p className="callout callout--error" role="alert">
            {error}
          </p>
        )}

        <div className="audit-form__actions">
          <button type="submit" className="button button--primary" disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="button"
            disabled={saving || !hasNextPending}
            onClick={() => void save(true)}
            title={hasNextPending ? undefined : "No pending tasks remain in the current filter."}
          >
            Save &amp; next pending
          </button>
          {dirty && (
            <span className="audit-form__hint" role="status">
              You have unsaved changes.
            </span>
          )}
        </div>
      </form>
    </section>
  );
}
