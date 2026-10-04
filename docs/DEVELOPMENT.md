# Development and deployment boundaries

## Repository layout

The primary runtime is `backend/portable.py`, using the shared core and SQLite without AWS or Site imports.
Keep `backend/bridge.py` beside `backend/portable.py` and `backend/aws/`: the AWS adapter imports the core module and its SAM template uses `CodeUri: ../`.
Keep `site/worker`, `site/db`, `site/drizzle`, and `site/scripts` together. Site migrations are append-only after deployment.

## Reproducible local checks

Use Python 3.12+ and Node 24 LTS, then run:

```sh
bash scripts/check.sh
```

No dependency installation or network access is required for these checks. The Site tests use built-in Node SQLite, which may emit an experimental-feature warning.
All service calls in the included tests use local doubles; no production secrets are needed.
The check script copies the Site source to a temporary directory, substitutes the example hosting manifest, and validates build output there.
It never deploys and does not change a real local hosting manifest.

To regenerate a Site schema migration, run from `site/`:

```sh
npm ci --ignore-scripts
npm run db:generate
```

The lockfile pins Drizzle dependencies. Installing them requires access to the npm registry.
Review the generated SQL and metadata, append a migration instead of modifying an applied one, then rerun the offline checks.
No dependency installation is needed merely to run tests or build the existing worker.

## Ordinary-server setup

See [SELF_HOSTING.md](SELF_HOSTING.md). The templates under `deploy/` are examples for review, not an installer.
Do not run the legacy Generic-bot poller against the portable server's database.

## AWS setup

Use `backend/aws/README.ja.md` for the existing implementation's deployment sequence.
From the repository root, the relevant pre-deployment command is:

```sh
sam validate --lint --template-file backend/aws/template.yaml
```

If using the detailed guide's relative commands, first change to `backend/`.
The guide clones this repository and runs relative commands from `backend/`.
The template is reusable and contains no account ID, deployment endpoint, or live secret.
SAM packages the whole `backend/` directory through `CodeUri`; Git ignore rules do not filter deployment archives. Deploy from a clean source copy with credentials stored externally, never from a directory containing local secret files.
Deployments may create billable resources and IAM policies and must be explicitly authorized.

## Private Site setup

`site/.openai/hosting.example.json` preserves the logical `DB` binding and MCP capability, with a placeholder project ID.
The real `site/.openai/hosting.json` is deliberately absent and ignored by Git.
For an existing Site, retrieve its verified existing configuration through the supported Sites workflow. Preserve its identity and owner-private access.
Do not use the example's placeholder as a deployment target and do not register a replacement Site merely to archive the source.

For a local-only build smoke test, use `scripts/check.sh`. For an authorized real deployment, operate from the existing Site's managed checkout and transfer the reviewed source changes there while preserving its actual hosting configuration.
Run that platform's check/build/save/deploy workflow. GitHub is source storage, not an automatic deployment trigger.
The Site depends on a trusted platform-provided authenticated user ID and a D1 binding. Do not expose the raw worker on an untrusted host that lets callers forge that header.

The owner must enter the separate proxy secret through the secure setup flow. Keep the Zulip API key in AWS.
Never put credentials into source, issue text, commit messages, URLs, or logs.

## Future work

- Make feature edits in the repository and keep tests alongside them.
- Run `bash scripts/check.sh` on the final source before committing.
- Review the actual staged file list and diff before pushing. `.gitignore` is only a guardrail.
- Do not copy `.git`, caches, credentials, databases, logs, or existing deployment configuration into source imports.
- Keep public Zulip replies limited to authorized public-channel context and public research. Do not source them from private assistant conversations or personal connected data.
- Treat publishing code, deploying infrastructure, enabling event intake, and sending a Zulip reply as separate actions requiring their own appropriate authorization.

No automatic CI workflow or deployment credentials are configured by this import.
