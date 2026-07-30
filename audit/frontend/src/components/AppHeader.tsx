interface AppHeaderProps {
  email: string;
  onOpenStats: () => void;
  onSignOut: () => void;
  signingOut: boolean;
}

export function AppHeader({ email, onOpenStats, onSignOut, signingOut }: AppHeaderProps) {
  return (
    <header className="app-header">
      <h1 className="app-header__title">Spreadsheet-RL Data Audit</h1>
      <div className="app-header__actions">
        <span className="app-header__email" title="Signed in as">
          {email}
        </span>
        <button type="button" className="button button--small" onClick={onOpenStats}>
          Statistics
        </button>
        <button
          type="button"
          className="button button--small"
          onClick={onSignOut}
          disabled={signingOut}
        >
          {signingOut ? "Signing out…" : "Log out"}
        </button>
      </div>
    </header>
  );
}
