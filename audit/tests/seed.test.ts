import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import auditConfig from "../audit.config.json";

interface SeedTask {
  id: string;
  split: "training" | "domain";
  output_key: string;
  target_key: string;
}

interface SeedAssignment {
  email: string;
  task_id: string;
  assignment_order: number;
}

interface SeedManifest {
  seed: string;
  pool_size_per_split: number;
  config: typeof auditConfig;
  auditors: string[];
  assignment_targets: Record<string, Record<"training" | "domain", number>>;
  tasks: SeedTask[];
  assignments: SeedAssignment[];
}

const auditRoot = fileURLToPath(new URL("../", import.meta.url));

describe("audit seed", () => {
  it("uses the configured identities and assignment targets", () => {
    const manifest = JSON.parse(
      readFileSync(resolve(auditRoot, "seed/manifest.json"), "utf8"),
    ) as SeedManifest;
    const configuredAuditors = [
      ...auditConfig.core_auditors,
      ...Object.keys(auditConfig.additional_auditors),
    ].map((email) => email.trim().toLowerCase());
    const configuredTargets: Record<string, Record<"training" | "domain", number>> = {};
    for (const email of auditConfig.core_auditors) {
      configuredTargets[email.trim().toLowerCase()] = {
        training: auditConfig.core_assignments_per_split,
        domain: auditConfig.core_assignments_per_split,
      };
    }
    for (const [email, splits] of Object.entries(auditConfig.additional_auditors)) {
      configuredTargets[email.trim().toLowerCase()] = {
        training: splits.training.count,
        domain: splits.domain.count,
      };
    }

    expect(manifest.seed).toBe(auditConfig.seed);
    expect(manifest.pool_size_per_split).toBe(auditConfig.pool_size_per_split);
    expect(manifest.config).toEqual(auditConfig);
    expect(manifest.auditors).toEqual(configuredAuditors);
    expect(manifest.assignment_targets).toEqual(configuredTargets);
    expect(manifest.tasks.filter((task) => task.split === "training")).toHaveLength(
      auditConfig.pool_size_per_split,
    );
    expect(manifest.tasks.filter((task) => task.split === "domain")).toHaveLength(
      auditConfig.pool_size_per_split,
    );

    const forbiddenTaskKey = ["model", "result"].join("_");
    for (const task of manifest.tasks) {
      expect(Object.hasOwn(task, forbiddenTaskKey)).toBe(false);
      expect(task.output_key).toBe(`workbooks/${task.id}/output.xlsx`);
      expect(task.target_key).toBe(`workbooks/${task.id}/target.xlsx`);
    }

    const taskIds = new Set(manifest.tasks.map((task) => task.id));
    const assignmentPairs = manifest.assignments.map((item) => `${item.email}\0${item.task_id}`);
    expect(new Set(assignmentPairs).size).toBe(assignmentPairs.length);
    expect(manifest.assignments.every((item) => taskIds.has(item.task_id))).toBe(true);

    for (const email of manifest.auditors) {
      const assigned = manifest.assignments.filter((item) => item.email === email);
      for (const split of ["training", "domain"] as const) {
        expect(assigned.filter((item) => item.task_id.startsWith(`${split}-`))).toHaveLength(
          manifest.assignment_targets[email][split],
        );
      }
      expect(new Set(assigned.map((item) => item.assignment_order)).size).toBe(assigned.length);
    }

    for (const split of ["training", "domain"] as const) {
      const expectedReviews = Object.values(configuredTargets).reduce(
        (total, targets) => total + targets[split],
        0,
      );
      const reviewCounts = manifest.tasks
        .filter((task) => task.split === split)
        .map(
          (task) =>
            manifest.assignments.filter((assignment) => assignment.task_id === task.id).length,
        );
      expect(reviewCounts.reduce((total, count) => total + count, 0)).toBe(expectedReviews);
      expect(Math.min(...reviewCounts)).toBeGreaterThanOrEqual(1);
    }
  });
});
