import auditConfig from "../audit.config.json";

const COOKIE_NAME = "audit_email";

export function normalizeEmail(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

const configuredEmails = [
  ...auditConfig.core_auditors,
  ...Object.keys(auditConfig.additional_auditors),
].map(normalizeEmail);

if (configuredEmails.some((email) => !email.includes("@"))) {
  throw new Error("Audit configuration contains an invalid email address.");
}
if (new Set(configuredEmails).size !== configuredEmails.length) {
  throw new Error("Audit configuration contains duplicate normalized email addresses.");
}

export const ALLOWED_EMAILS = new Set(configuredEmails);

export function isAllowedEmail(value: unknown): value is string {
  return ALLOWED_EMAILS.has(normalizeEmail(value));
}

export function readSessionEmail(request: Request): string | null {
  const cookie = request.headers.get("Cookie") ?? "";
  const pair = cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE_NAME}=`));
  if (!pair) return null;

  const raw = pair.slice(COOKIE_NAME.length + 1);
  try {
    const email = normalizeEmail(decodeURIComponent(raw));
    return ALLOWED_EMAILS.has(email) ? email : null;
  } catch {
    return null;
  }
}

export function sessionCookie(email: string): string {
  return `${COOKIE_NAME}=${encodeURIComponent(normalizeEmail(email))}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Strict`;
}

export function clearSessionCookie(): string {
  return `${COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict`;
}

export function hasSameOrigin(request: Request): boolean {
  const origin = request.headers.get("Origin");
  return origin === null || origin === new URL(request.url).origin;
}
