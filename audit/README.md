# Spreadsheet-RL Audit

Spreadsheet-RL Audit is the human-review platform used to inspect Spreadsheet-RL training and domain-evaluation tasks. This directory contains a reusable, public-safe configuration; it is not connected to the original deployment or audit database.

## What it does

- Samples deterministic pools of 100 training tasks and 100 domain-evaluation tasks.
- Assigns the example audit team 80 tasks from each split, plus two split-specific 50-task assignments. Every pooled task receives four to six independent reviews.
- Shows the instruction, category, source path, answer position, initial `output.xlsx`, and ground-truth `target.xlsx`.
- Renders the values cached in each XLSX with Mog without recalculating formulas. Auditors can select cells, inspect formulas, resize columns for the browser session, zoom, switch sheets, and scroll.
- Provides downloads for both workbooks so suspicious cached values can be checked in desktop Excel.
- Records `Yes`, `Almost correct`, or `No` ground-truth assessments for both splits and binary exact-match suitability for domain tasks.
- Requires a concrete description for `Almost correct` or `No` ground-truth assessments and `No` exact-match assessments.
- Provides per-auditor progress, per-task response proportions, exact-match averages, and CSV exports.

## Stack

- React 19, TypeScript, and Vite
- Cloudflare Workers and Static Assets
- Cloudflare D1
- `@mog-sdk/embed` for spreadsheet rendering
- Vitest

## Repository layout

```text
audit.config.json       Example identities and deterministic assignment settings
frontend/               React application
worker/                 Worker API and D1 access
shared/                 Shared API types
migrations/             Fresh-install D1 schema
scripts/                Seed preparation and production build
seed/                   Reproducible task and assignment manifest
patches/                Mog read-only interaction patch
tests/                  Focused automated tests
wrangler.jsonc          Example Cloudflare configuration
```

## Configure the audit team

Edit `audit.config.json` before preparing the seed. The checked-in addresses use the reserved `example.com` domain and must be replaced before real use. The Worker reads the same file for its login whitelist, so seed assignments and allowed identities stay synchronized.

The five `core_auditors` use balanced omissions across each task pool. Entries under `additional_auditors` select a deterministic number of tasks from each split using the configured offsets. If you change pool sizes or assignment counts, keep every task assigned to at least one auditor.

The email gate only checks whether the typed address is listed; it does not prove mailbox ownership. Use Cloudflare Access or another identity provider before exposing sensitive data or accepting adversarial traffic.

## Install and prepare data

Prerequisites are Node.js, npm, Python 3, and the Spreadsheet-RL workbook archive from the `Spreadsheet-RL/Spreadsheet-RL` Hugging Face dataset repository.

By default, the generator reads `../data/spreadsheets.zip` relative to this directory:

```text
Spreadsheet-RL/
├── audit/
└── data/
    └── spreadsheets.zip
```

You can instead set `SPREADSHEET_RL_ARCHIVE` to an absolute archive path.

```bash
npm ci
npm run seed:prepare
```

Seed preparation extracts the 400 required workbooks and writes `seed/manifest.json` plus `seed/seed.sql`. The pool and assignment order are deterministic for a fixed configuration. Extracted workbooks and `seed/seed.sql` are intentionally ignored; the reviewed manifest is tracked.

## Run locally

Initialize a fresh local D1 database and start Wrangler:

```bash
npm run seed:local
npm run dev
```

Wrangler normally serves the site at `http://localhost:8787`.
`npm run dev` builds the frontend before starting the local Worker.

Warning: `seed/seed.sql` deletes existing audits, assignments, and tasks before inserting the deterministic seed. Do not run it against a database whose responses must be preserved.

## Validate

```bash
npm run check
```

This runs TypeScript checking, Vitest, and the production frontend build. `npm ci` also applies the tracked Mog patch with `patch-package`.

The custom build script compresses Mog's emitted WASM below Cloudflare's 25 MiB per-asset limit. Use `npm run build` rather than a bare Vite build for deployment.

## Configure Cloudflare

The checked-in `wrangler.jsonc` uses an all-zero example D1 identifier and no custom-domain route.

1. Create a D1 database:

   ```bash
   npx wrangler d1 create spreadsheet-rl-audit
   ```

2. Replace the all-zero `database_id` in `wrangler.jsonc` with the returned ID.

3. Optional: add a custom-domain route, replacing the example hostname:

   ```jsonc
   "routes": [
     { "pattern": "audit.example.com", "custom_domain": true }
   ],
   ```

4. Apply the schema and generated seed to a new database:

   ```bash
   npx wrangler d1 execute spreadsheet-rl-audit --remote --file migrations/0001_initial.sql
   npx wrangler d1 execute spreadsheet-rl-audit --remote --file seed/seed.sql
   ```

5. Deploy:

   ```bash
   npm run deploy
   ```

The checked-in migration targets a fresh database and is not an upgrade path for earlier or private deployments. Back up the existing database and design a dedicated migration before adapting an existing deployment.

## Audit protocol

All tasks ask:

> Does the ground-truth spreadsheet correctly satisfy the instruction?

Domain tasks additionally ask:

> Is exact all-cell matching at the answer position a reasonable correctness criterion for this task?

Auditors should consider whether semantically correct alternatives could use different formulas, values, formatting, or layout. A `No` exact-match response requires a failure description or a better evaluation rule.

Ground-truth answers are `Yes`, `Almost correct`, and `No`. `Almost correct` and `No` both require a concrete problem description. Statistics keep these as three separate proportions rather than converting them to numeric scores.

## Security and data handling

The application uses an `HttpOnly`, `Secure`, `SameSite=Strict` cookie and same-origin checks for state-changing requests. Workbook and audit endpoints require an address from the server-side whitelist.

Do not commit `.dev.vars`, credentials, raw datasets, extracted workbooks, generated seed SQL, or exported audit responses.

## Mog limitation

Mog displays the values cached in the original XLSX file and does not recalculate formulas. Those values can be stale or differ from desktop Microsoft Excel after recalculation. When a workbook looks suspicious, download `output.xlsx` and `target.xlsx` and inspect them locally before recording a decision. Column-width changes remain local to the current browser session and never modify the downloaded files.
