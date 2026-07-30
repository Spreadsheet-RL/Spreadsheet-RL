import { describe, expect, it } from "vitest";
import auditConfig from "../audit.config.json";
import {
  ALLOWED_EMAILS,
  hasSameOrigin,
  isAllowedEmail,
  normalizeEmail,
  readSessionEmail,
  sessionCookie,
} from "../worker/auth";

const configuredEmails = [
  ...auditConfig.core_auditors,
  ...Object.keys(auditConfig.additional_auditors),
].map(normalizeEmail);
const allowedEmail = configuredEmails[0];
const outsiderEmail = "not-configured@example.invalid";

describe("audit email gate", () => {
  it("normalizes and accepts configured addresses", () => {
    expect(ALLOWED_EMAILS).toEqual(new Set(configuredEmails));
    expect(isAllowedEmail(`  ${allowedEmail.toUpperCase()}  `)).toBe(true);
    expect(isAllowedEmail(outsiderEmail)).toBe(false);
  });

  it("round-trips the session cookie and rejects non-whitelisted values", () => {
    const cookie = sessionCookie(allowedEmail.toUpperCase()).split(";", 1)[0];
    expect(
      readSessionEmail(new Request("https://audit.example/api/me", { headers: { Cookie: cookie } })),
    ).toBe(allowedEmail);
    expect(
      readSessionEmail(
        new Request("https://audit.example/api/me", {
          headers: { Cookie: `audit_email=${encodeURIComponent(outsiderEmail)}` },
        }),
      ),
    ).toBeNull();
  });

  it("rejects cross-origin mutations", () => {
    expect(
      hasSameOrigin(
        new Request("https://audit.example/api/session", {
          headers: { Origin: "https://evil.example" },
        }),
      ),
    ).toBe(false);
  });
});
