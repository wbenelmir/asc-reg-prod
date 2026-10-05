# Manual GitHub handoff (owner)

The owner alone creates the repository, commits and pushes. Nothing here is
automated. Replace every `<placeholder>`; never put a token, password or key in
a command, a URL or this file. Authenticate with the normal GitHub tooling:
GitHub CLI (`gh auth login`) or Git Credential Manager in the browser.

**The folder to upload** is the project root: the folder that contains
`manage.py`, `pyproject.toml` and `uv.lock`. Initialize Git there, not in a
parent folder.

## 1. Create a private repository

On GitHub: **New repository** → owner `<organization or account>` → name
`<repository name>` → **Private** → do **not** add a README, `.gitignore` or
license (the project already has them) → **Create repository**.

## 2. Initialize the local repository (first time only)

```bash
cd <project root>
git init -b main
git config user.name "<your name>"
git config user.email "<your email>"
```

## 3. Review what will be committed

```bash
git add .
git status
git diff --cached --stat
git diff --cached --name-only
```

Read the file list. It must not contain any of these; if one appears, run
`git reset` and fix `.gitignore` before continuing:

* `.env` or any `.env.*` file except `.env.example` (and `deploy/staging.env.example`);
* `.venv/`, `var/`, `staticfiles/`, `__pycache__/`, `reference/`;
* any `.zip`, `.pem`, `.key`, `.p12`, `.log`, `.sqlite3` file;
* `.claude/`, `CLAUDE.md`, `AGENTS.md` or other tool working files;
* uploaded documents, exports or any real personal data.

A quick filter (it must print nothing):

```bash
git diff --cached --name-only | grep -E '(^|/)\.env($|\.)|^\.venv/|^var/|^reference/|\.(zip|pem|key|p12|log|sqlite3)$|CLAUDE|AGENTS'
```

(`.env.example` and `deploy/staging.env.example` are allowed; if the filter
prints only those two, it is clean.)

## 4. First commit and push

```bash
git commit -m "ASC 2026 registration platform: staging Beta"
git remote add origin <repository URL>
git push -u origin main
git tag -a beta-1 -m "Staging Beta 1"
git push origin beta-1
```

Give the deployment team the tag name; they build from it (deployment guide §6).

## 5. Later updates

```bash
git status
git add <changed paths>          # or: git add -p   to review hunk by hunk
git diff --cached
git commit -m "<what changed>"
git push
git tag -a beta-<n> -m "<summary>" && git push origin beta-<n>
```

## 6. Give the deployment team access

Repository **Settings → Collaborators and teams → Add teams/people**:

* the deployment team: **Read** (enough to clone and build from a tag);
* developers who push: **Write**;
* nobody else **Admin**; require two-factor authentication for the
  organization.

## 7. Files that must not be tracked

`.gitignore` only prevents new files from being added. A file that is already
tracked stays tracked. To stop tracking one file while keeping it on disk:

```bash
git rm --cached <path>
git commit -m "Stop tracking <path>"
```

Use exact paths. Do not run a broad `git rm -r --cached .` without reviewing
the result.

If a secret was ever committed, removing the file from the current tree does
**not** remove it from the history or from clones and forks. Treat the secret
as exposed: rotate it at once (new database password, new keys, new SMTP
credentials), then decide separately whether to rewrite the history.
