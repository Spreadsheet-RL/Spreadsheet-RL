import { useCallback, useEffect, useState } from "react";
import { ApiError, fetchSession } from "./api";
import { errorMessage } from "./format";
import { AuditWorkspace } from "./components/AuditWorkspace";
import { LoginView } from "./components/LoginView";
import { Spinner } from "./components/Spinner";

type SessionState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "authenticated"; email: string }
  | { status: "unavailable"; message: string };

export function App() {
  const [session, setSession] = useState<SessionState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    fetchSession()
      .then((email) => {
        if (!active) return;
        setSession(email ? { status: "authenticated", email } : { status: "anonymous" });
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setSession({ status: "unavailable", message: errorMessage(caught) });
      });
    return () => {
      active = false;
    };
  }, []);

  const handleAuthenticated = useCallback((email: string) => {
    setSession({ status: "authenticated", email });
  }, []);

  const handleSignedOut = useCallback(() => {
    setSession({ status: "anonymous" });
  }, []);

  /** Any 401 anywhere in the app means the cookie is gone; drop back to login. */
  const handleApiError = useCallback((caught: unknown) => {
    if (caught instanceof ApiError && caught.isUnauthorized) setSession({ status: "anonymous" });
  }, []);

  if (session.status === "loading") {
    return (
      <main className="boot" aria-busy="true">
        <Spinner label="Checking your session…" />
      </main>
    );
  }

  if (session.status === "unavailable") {
    return (
      <main className="boot">
        <div className="callout callout--error" role="alert">
          <h1>Spreadsheet-RL Data Audit</h1>
          <p>{session.message}</p>
          <button type="button" className="button" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </main>
    );
  }

  if (session.status === "anonymous") {
    return <LoginView onAuthenticated={handleAuthenticated} />;
  }

  return (
    <AuditWorkspace
      email={session.email}
      onSignedOut={handleSignedOut}
      onApiError={handleApiError}
    />
  );
}
