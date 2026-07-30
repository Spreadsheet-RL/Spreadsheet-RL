import { describe, expect, it } from "vitest";
import type { GroundTruthAssessment, SubmitAuditRequest } from "../shared/types";
import { validAudit } from "../worker/index";

function response(
  groundTruthAssessment: GroundTruthAssessment,
  failureDescription = "",
  exactMatchReasonable: boolean | null = null,
): SubmitAuditRequest {
  return { groundTruthAssessment, exactMatchReasonable, failureDescription };
}

describe("audit validation", () => {
  it("accepts Yes without a description", () => {
    expect(validAudit(response("yes"), "training")).toBeNull();
  });

  it("rejects a non-text description before submission", () => {
    const malformed = response("yes");
    Reflect.set(malformed, "failureDescription", null);
    expect(validAudit(malformed, "training")).toContain("must be text");
  });

  it("requires a description for Almost correct and No", () => {
    expect(validAudit(response("almost"), "training")).toContain("Almost correct or No");
    expect(validAudit(response("no"), "training")).toContain("Almost correct or No");
    expect(validAudit(response("almost", "Minor formula issue"), "training")).toBeNull();
  });

  it("keeps the Domain exact-match question binary and required", () => {
    expect(validAudit(response("yes", "", null), "domain")).toContain(
      "Exact-match suitability",
    );
    expect(validAudit(response("yes", "", true), "domain")).toBeNull();
    expect(validAudit(response("yes", "", false), "domain")).toContain(
      "Almost correct or No",
    );
  });
});
