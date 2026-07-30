interface SpinnerProps {
  label: string;
}

export function Spinner({ label }: SpinnerProps) {
  return (
    <p className="spinner" role="status">
      <span className="spinner__dot" aria-hidden="true" />
      {label}
    </p>
  );
}
