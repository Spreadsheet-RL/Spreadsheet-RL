import { useId, useState, type FormEvent } from "react";
import { login } from "../api";
import { errorMessage } from "../format";

interface LoginViewProps {
  onAuthenticated: (email: string) => void;
}

export function LoginView({ onAuthenticated }: LoginViewProps) {
  const emailId = useId();
  const hintId = useId();
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = email.trim();
    if (!trimmed || submitting) return;

    setSubmitting(true);
    setError(null);
    try {
      const session = await login(trimmed);
      onAuthenticated(session.email);
    } catch (caught) {
      // The worker owns the whitelist; show its message verbatim.
      setError(errorMessage(caught));
      setSubmitting(false);
    }
  }

  return (
    <main className="boot">
      <section className="card login" aria-labelledby="login-heading">
        <h1 id="login-heading">Spreadsheet-RL Data Audit</h1>
        <p className="login__lede">
          Access is limited to the audit team. Enter the email address you were invited with to
          continue.
        </p>

        <form onSubmit={handleSubmit} noValidate>
          <div className="field">
            <label htmlFor={emailId}>Email address</label>
            <input
              id={emailId}
              name="email"
              type="email"
              inputMode="email"
              autoComplete="email"
              autoFocus
              required
              spellCheck={false}
              aria-describedby={hintId}
              aria-invalid={error !== null}
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={submitting}
            />
            <p id={hintId} className="field__hint">
              We do not send a code — signing in only records who submitted each audit.
            </p>
          </div>

          {error !== null && (
            <p className="callout callout--error" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            className="button button--primary"
            disabled={submitting || email.trim().length === 0}
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}
