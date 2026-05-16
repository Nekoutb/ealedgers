# Contributing to EA Ledgers

Workflow for changes to this repository.

## Branch protection

`main` is protected:

- **Direct pushes are blocked.** All changes go through a Pull Request.
- **No required approvals.** A solo contributor can self-merge.
- **Force pushes and branch deletion are disabled.**
- Admins can bypass these rules in emergencies (`enforce_admins: false`).

## Branch naming

Pick a prefix that matches the intent of the change:

| Prefix         | When to use                                              | Example                                     |
| -------------- | -------------------------------------------------------- | ------------------------------------------- |
| `feat/...`     | New feature or user-facing capability                    | `feat/customer-invoicing`                   |
| `fix/...`      | Bug fix                                                  | `fix/trial-balance-zero-rows`               |
| `chore/...`    | Tooling, CI, infra, refactor with no behaviour change    | `chore/ci-and-pr-workflow`                  |
| `docs/...`     | Documentation only                                       | `docs/syscohada-class-9-notes`              |
| `refactor/...` | Internal refactor (no behaviour change)                  | `refactor/extract-aging-buckets`            |
| `claude/...`   | Branch created in a Claude Code session (any kind)       | `claude/dashboard-charts`                   |

## Commit messages

First line is a short imperative summary (≤ 70 chars). Optional body explains
why. Reference the affected area if it isn't obvious.

```
Workspace: 3x3 icon grid + admin sub-nav + emerald accent

Workspace:
- Centred 3x3 grid of 9 module tiles
- Accounting (only live module) sits in the centre cell with a small Live pill
- ...
```

## Pull Requests

Open a PR with `gh pr create`. Title should match the headline of the work,
not just `WIP`. The body should answer:

- **What changed** — short summary of files / modules touched
- **Why** — the motivation
- **How to verify** — what the reviewer (or you, in 2 weeks) should click
- **Risk** — anything that could break, especially on production

Minimum template:

```markdown
## Summary
- Bullet points of what changed.

## Why
Short paragraph.

## Verify
- Visit https://ealedgers.com/...
- Click X, expect Y

## Risk
None / migrations / breaking change / ...
```

## Deploy on merge

The deploy workflow at `.github/workflows/deploy.yml` runs on every push to
`main`. When you merge a PR, that's a push, so the merge auto-deploys to
production (`ealedgers.com`) in ~10 seconds:

```
git pull origin main
pip install -r requirements.txt
manage.py migrate --noinput
manage.py collectstatic --noinput
systemctl restart ealedgers.service
```

If a deploy fails, the failure is visible in the Actions tab; the previous
commit stays on the server (the script only updates files via `git reset`
after a successful pull, but service restart could still fail). Tail
`journalctl -u ealedgers -f` on the host to debug.

## Hotfixes

If `main` is broken and you need to bypass review:

1. Push the fix to a branch (`fix/<thing>`).
2. Open a PR.
3. Use admin merge (branch protection has `enforce_admins: false`, so an admin
   can merge without satisfying any pending checks).

## Secrets and credentials

- Never commit `.env`, `deploy_key`, `deploy_key.pub`, SSH passwords, or
  database files. `.gitignore` covers these but pay attention if you add new
  files with secrets.
- GitHub Action secrets live under repo Settings → Secrets and variables →
  Actions. Current set: `SSH_HOST`, `SSH_USER`, `SSH_PORT`, `SSH_PRIVATE_KEY`.

## Local dev quickstart

```bash
# from the working directory
PYTHONIOENCODING=utf-8 ./venv/Scripts/python.exe manage.py runserver
```

Then open http://127.0.0.1:8000/ and sign in.
