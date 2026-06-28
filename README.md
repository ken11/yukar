# yukar

**A local-only autonomous coding agent.**

Register an existing local Git repository as a Project, give it an Epic-sized request, and a team of Manager / Worker / Evaluator agents implements it in isolated git worktrees. Only the final merge into the default branch is approved by a human via the UI.

> **Security note.** yukar is designed for a single local user and binds exclusively to `127.0.0.1`. Workers execute shell commands with the privileges of the user who started the server. There is no OS-level sandbox. Only register repositories you trust, and keep each repo's command allowlist as small as possible. Do not expose the server to external networks.

---

## Design Philosophy

- **Epic-scale autonomy.** Rather than handling one issue at a time, yukar takes on a larger, coherent chunk of work (an Epic) and drives it to completion end-to-end.
- **Manager / Worker / Evaluator role separation.** The Manager decomposes the Epic into tasks; Workers implement and commit each task in isolation; the Evaluator reviews diffs and test results, returning failing tasks to a Worker until they pass.
- **Isolation and safe parallelism via git worktrees.** Every Epic operates inside a dedicated git worktree, so the source repository is never polluted. Workers can run in parallel without interfering with each other.
- **Human-in-the-loop.** Merges are never automatic. A human must review the final diff in the UI and explicitly approve merging into the default branch.
- **Local web app, single user.** No cloud service required. yukar runs entirely on your machine.

---

## Getting Started

### Prerequisites

- Git
- Python 3.14
- [uv](https://docs.astral.sh/uv/)
- Node.js 20.9+ and pnpm 11.1.3+
- **An AWS account with Amazon Bedrock access** — required (see [Configure credentials](#configure-credentials))

### Configure credentials

**Amazon Bedrock is required.** yukar builds a semantic code-search index using Amazon Titan Text Embeddings, which is only available through Bedrock — there is no Anthropic embeddings API. Bedrock is also the default provider for the agent LLM (Claude).

You may optionally route the **agent LLM** to the Anthropic API instead of Bedrock Claude (selectable in the Settings screen), but the code index always uses Bedrock Titan — so Bedrock credentials are required in every configuration.

Credentials are read from the process environment via the standard AWS SDK credential chain; yukar does **not** auto-load `.env`. Copy [`.env.example`](.env.example), fill it in, and export the variables in the shell that launches yukar:

```bash
set -a; . ./.env; set +a   # bash / zsh
# fish: export (grep -v '^#' .env | string trim)
```

Before your first run, open the Amazon Bedrock console and enable model access for the Claude model(s) you intend to use and for **Titan Text Embeddings V2**.

### Startup (local production mode)

```bash
pnpm install
pnpm build
cd apps/api
uv run yukar serve
```

`yukar serve` supervises both the FastAPI backend and the built Next.js standalone server, binding both to `127.0.0.1`. After startup, open:

- **Web UI:** <http://127.0.0.1:3000>
- **API / Swagger:** <http://127.0.0.1:8000/docs>

### Running your first Epic

1. **Settings** — choose your LLM provider/model and set the Git author name and email used for commits. All configuration lives here in the UI.
2. **Projects → New Project** — paste the absolute path to an existing local Git repository.
3. **Repos screen** — set the per-repo command allowlist. yukar is deny-by-default; add only the executables the repo needs (e.g. `pnpm`, `uv`, `pytest`).
4. Open the project, create an Epic with a title and acceptance criteria, and press **Start Run**.
5. Watch progress in Threads / Tasks. When the run completes, open **Git Diff**, review the changes, and press **Merge to default** to approve.

---

## License

[MIT](LICENSE)
