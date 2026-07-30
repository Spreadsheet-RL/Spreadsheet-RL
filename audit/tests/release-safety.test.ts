import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const auditRoot = fileURLToPath(new URL("../", import.meta.url));
const releaseSensitiveFiles = [
  "README.md",
  "audit.config.json",
  "package.json",
  "frontend/src/api.ts",
  "frontend/src/components/TaskDetails.tsx",
  "frontend/src/styles.css",
  "migrations/0001_initial.sql",
  "scripts/prepare_seed.py",
  "seed/manifest.json",
  "shared/types.ts",
  "worker/auth.ts",
  "worker/index.ts",
  "wrangler.jsonc",
] as const;

describe("public release safety", () => {
  it("keeps private evaluation fields out of public sources", () => {
    const forbiddenTokens = [
      ["model", "result"].join("_"),
      ["model", "Result"].join(""),
      ["train", "rollout"].join("_"),
    ];

    for (const relativePath of releaseSensitiveFiles) {
      const source = readFileSync(resolve(auditRoot, relativePath), "utf8");
      for (const token of forbiddenTokens) expect(source).not.toContain(token);
    }
  });
});
