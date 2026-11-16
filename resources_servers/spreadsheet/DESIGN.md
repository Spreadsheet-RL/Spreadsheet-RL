# Spreadsheet tools and LibreOffice rewards

Status: source implemented; target-node and dataset qualification remain required before training.

## Deployment

The existing training container runs the Gym model/agent servers and a lightweight
`SpreadsheetResourcesServer` HTTP adapter. One independently started Linux service
container owns all spreadsheet files, Python execution, and LibreOffice processes.
The adapter calls its configured IP/port. Neither side creates nested containers.
There is no dependency on the Spreadsheet-RL trainer, PyTorch, or a GPU in the
spreadsheet service.

## Contracts and invariants

1. A dataset task ID selects a server-owned manifest entry. Client/model input must
   never select an oracle path or replace the answer regions.
2. A new rollout gets a new session and an independent copy of `output.xlsx` named
   `data.xlsx`. Many rollouts of one task must never share mutable files.
3. `/seed_session`, twelve tool endpoints, `/verify`, and `/close_session` implement
   Gym's resource lifecycle. Agent orchestration uses Responses function calls,
   preserving Gym token/log-probability fields; Hermes parsing is not duplicated.
4. Every operation has an ID provided by the agent outside model arguments.
   A retry with identical ID/input returns the stored result. A changed input with
   the same ID is rejected. Identical arguments with different IDs are real calls.
5. Each operation locks its session. Mutations run on a staging workbook and commit
   by atomic replacement. Python also operates on a staging copy. Failed calls
   cannot partially change the canonical workbook. No hard links to original data.
6. SQLite records sessions/jobs/results. A process crash with an in-flight mutation
   leaves an indeterminate session, never an automatically replayed write.
7. LibreOffice workers have separate profiles and process groups; each handles one
   job at a time. Hard timeout kills only its worker, which is then replaced.
8. Native tools and final verification use the same recalculation engine. The
   oracle's original cached values remain the reference. Reward is binary all-cell
   correctness over server-owned answer regions; cell accuracy is diagnostic only.
9. Bad model arguments/invalid edits are tool errors. Infrastructure/engine failure
   is an environment error and must not silently become reward zero.
10. Python executes only through an OS isolation backend. This target denies user
    namespaces but grants chroot/setuid/setgid capabilities. Default: a minimal
    Python chroot, different unprivileged UIDs per active job, no-new-privileges,
    and seccomp blocking network/introspection/identity/process-group escapes.
    Only the staging workbook is copied in; no oracle, service token, or host root
    is exposed. Bubblewrap is optional on supported hosts. Missing isolation
    support fails readiness; there is no unsafe fallback.
11. Macro execution and external link updates are disabled in LibreOffice. Tool
    schemas cannot access lifecycle/reward endpoints. Only the authenticated adapter
    can access control APIs. Service keys never enter model input or child env.
12. Verification freezes a workbook. Close/TTL cleanup cannot delete active jobs.
    Retain task/revision, seed/output hashes, tool receipts, engine version, and
    failed-rollout artifacts according to configured retention.

## Implementation sequence

- [x] Schemas, dataset converter/catalog, and workbook tools.
- [x] Transactional sessions, operation receipts, HTTP job API, auth, cleanup.
- [x] LibreOffice pool/UNO bridge and isolated Python runner.
- [x] Gym proxy/agent, configs, and synthetic service canary.
- [x] Tests: same-task isolation, retry after lost response, rollback, restart,
      multi-sheet formulas, oracle denial, and infrastructure-error classification.
- [x] Build/qualify a CPU runtime on Modal for the target distro/architecture.
- [x] Publish a credential-free bundle, checksums, dependency/license notices,
      code/data provenance, and qualification receipt to a public HF repository.
- [ ] User downloads through the company proxy; run readiness and a small canary
      on the target container before scaling RL.

## Runtime packaging

Build dependencies on Modal, not on the Mac. Runtime and source are separate:
runtime changes only with interpreter/ABI/LibreOffice/dependency changes; source
is pinned by Git commit. Download datasets directly on the service node, outside
the runtime bundle. Public bundles must contain no credentials, company files,
private datasets, or oracle payloads. Include checksums and exact build inputs.

The target's CPU, glibc, writable disk, available Python, and namespace support
must be checked before selecting the bundle. A standalone archive does not grant
kernel capabilities missing from the enclosing container.

## Validation boundaries

LibreOffice rewards are a new execution backend, not a claimed reproduction of
Excel rewards. Qualify supported tasks by recalculating a copy of their oracle
and comparing against the untouched oracle values. Keep unsupported or unstable
tasks out of training using a versioned compatibility manifest. A successful
import, mocked test, or reachable HTTP port is not a passed Linux canary.

Existing calculator changes and unrelated `pyproject.toml`/`uv.lock` edits remain
in the original checkout. Work proceeds on `tung/feat/spreadsheet-libreoffice` in
an isolated worktree. Local commits do not imply a GitHub push.
