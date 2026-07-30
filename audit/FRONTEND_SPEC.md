# Spreadsheet-RL audit frontend reference

This document records the implemented frontend contract. Setup and deployment guidance lives in `README.md`.

## Stack

- React 19 + TypeScript + Vite.
- Render both XLSX workbooks read-only with `MogSheet` from `@mog-sdk/embed/react`.
- Import any required Mog CSS from its public package exports.
- Desktop is the primary audit environment; remain usable on tablet/mobile.
- Keep the visual style restrained, research-oriented, and accessible. Do not use decorative gradients.

## Routes and API

- `POST /api/session` with `{ "email": string }` logs in and sets an HttpOnly cookie.
- `DELETE /api/session` logs out.
- `GET /api/me` returns `{ email }` or 401.
- `GET /api/dashboard` returns `DashboardResponse` from `shared/types.ts`.
- `GET /api/tasks/:taskId` returns one `AssignedTask`.
- `PUT /api/audits/:taskId` accepts `SubmitAuditRequest` and returns the updated task.
- `GET /api/workbooks/:taskId/output` returns `output.xlsx` bytes.
- `GET /api/workbooks/:taskId/target` returns `target.xlsx` bytes.
- Add `?download=1` for a download response.
- `GET /api/stats` returns `StatsResponse`.
- `GET /api/stats/export?kind=auditors` downloads per-person progress CSV.
- `GET /api/stats/export?kind=tasks` downloads per-task aggregate CSV.

All fetch calls must use same-origin credentials. Treat a 401 as a logged-out session. Surface API errors clearly without exposing stack traces.

## Login

- Ask the visitor to type their email address.
- Explain briefly that access is limited to the audit team.
- Do not display or embed the whitelist in frontend code.
- Handle rejected emails with the API's message.

## Main audit experience

- Header: “Spreadsheet-RL Data Audit”, signed-in email, statistics button, and logout.
- Show Training and Domain progress separately as completed / assigned and remaining, including 0 / 0 for an unassigned split.
- Provide filters for split (Training / Domain), status (Pending / Completed / All), and task navigation. In each task-list item, show only the Pending/Completed badge; do not repeat the split badge beside it.
- Allow the task-navigation panel to collapse into a narrow restore control on wide screens.
- Default to the first pending task. Preserve a user's saved answer when revisiting a completed task.
- Display task ID, category, source path, instruction, and answer position.
- Show two clearly labeled, read-only workbook viewers:
  - “Initial workbook — output.xlsx”
  - “Ground truth — target.xlsx”
- Side by side on wide screens. Tabs or a vertical layout are acceptable on narrow screens.
- Each workbook needs an explicit download button.
- Show this warning near the viewers:
  “Mog displays the values cached in the original XLSX file and does not recalculate formulas. Cached values can be stale or differ from desktop Microsoft Excel. If the ground truth looks suspicious, download the workbook and open it locally before deciding.”
- Use stable `config` and `hostPolicy` identities for each Mog viewer so changing form state does not reload it.
- Read workbook bytes through the authenticated API URLs. Use `requestedMode: 'readonly'`, view-only capabilities, no save, and no collaboration.
- Keep cell selection and its visible highlight enabled so the formula bar updates for the focused cell.
- Display the cached cell values stored in the original XLSX and do not recalculate formulas during import.
- After a cell is selected, plain arrow keys move the active cell, keep it visible, and refresh the formula bar. Do not intercept modified arrow-key combinations.
- Keep direct horizontal and vertical mouse scrolling, zoom controls, sheet tabs, and column-width adjustment available. Column-width changes apply only to the current browser session, reset on reload, and must not modify the XLSX file.
- Viewer load failures must not block downloading or submitting the audit.

## Audit form

Question 1 for both splits:

“Does the ground-truth spreadsheet correctly satisfy the instruction?”

- Yes
- Almost correct
- No

Question 2 only for Domain tasks:

“Is exact all-cell matching at the answer position a reasonable correctness criterion for this task?”

- Yes
- No

Add helper text for Q2: “Consider whether a semantically correct solution could use different formulas, layout, formatting, or equivalent values.”

Do not add Q3.

Show a failure-description textarea when Q1 is Almost correct or No, or Domain Q2 is No. It is required in that case, must be at least five trimmed characters, and should ask for a concrete failure or better evaluation rule. Allow an optional note when both answers are Yes if this does not clutter the form.

Provide “Save” and “Save & next pending” actions. Disable submission while saving. Prevent silent loss of unsaved changes when navigating between tasks.

## Statistics

- A statistics view or modal must show per-person completion for Training, Domain, and total.
- Show compact aggregate information for task-level responses; a searchable/paginated table is fine.
- Provide prominent download buttons for both auditor-progress CSV and per-task response CSV.
- Report Q1 as separate Yes, Almost correct, and No proportions without converting them to a numeric score. Explain the binary Q2 average as the fraction of completed responses answering Yes. Empty tasks should display an em dash, not zero.

## Quality requirements

- Semantic HTML, keyboard-accessible controls, visible focus states, and useful loading/empty/error states.
- Avoid duplicating API response type definitions; import from `../../shared/types` where practical.
- Keep components reasonably separated, but do not create unnecessary abstraction layers.
- The result must pass `npm run typecheck` and `npm run build:frontend` from this directory.
