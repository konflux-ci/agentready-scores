# agentready-scores

A repository for collecting [AgentReady](https://github.com/ambient-code/agentready) AI readiness scores across one or more GitHub organizations and ingesting them into [DevLake](https://github.com/konflux-ci/devlake) for visualization.

## Structure

```
submissions/
  {org}/
    {repo}/
      assessment-YYYYMMDD-HHMMSS.json  ← actual assessment data
      assessment-latest.json           ← symlink to latest assessment
runner/
  assess.py        ← concurrent assessment runner (local + CI)
  runner_lib.py    ← core library (discovery, assessment, git commit)
  requirements.txt
  orgs/
    example.yaml   ← copy and rename for each org you want to assess
    {org}.yaml     ← your org config (org name + optional exclude list)
  tests/
    test_runner_lib.py
.github/workflows/
  assess-manual.yml    ← manual trigger with configurable inputs
  assess-scheduled.yml ← weekly cron for all orgs in runner/orgs/
```

## Setup

1. Copy `runner/orgs/example.yaml` → `runner/orgs/{your-org}.yaml` and fill in your org name
2. Add repository secrets:
   - `GHCR_TOKEN` — token to pull the `ghcr.io/ambient-code/agentready` image
   - `GH_TOKEN` — GitHub token with `repo` read + `contents: write` access
3. *(Optional)* Enable Slack failure notifications:
   - Secret: `SLACK_WEBHOOK_URL` — your Slack incoming webhook URL
   - Variable: `SLACK_NOTIFICATIONS` = `true`
4. Run the **Assess repos (manual)** workflow to generate your first assessments
5. Point your DevLake AgentReady connection at this repo (`submissions/` path)

## Running Locally

```bash
cd agentready-scores
pip install -r runner/requirements.txt

export GH_TOKEN=<your-github-token>
export GHCR_TOKEN=<your-ghcr-token>

# Assess repos listed in an org YAML file
python runner/assess.py --from-file runner/orgs/{your-org}.yaml

# Assess multiple orgs at once
python runner/assess.py --from-file runner/orgs/*.yaml

# Discover and assess ALL public repos in an org (no YAML needed)
python runner/assess.py --org your-org-name

# Re-run repos that failed a previous run
python runner/assess.py --from-file runner/failed-{your-org}.yaml
```

## Org YAML format

```yaml
org: your-org-name

# repos:           # optional — if omitted, all public repos are discovered
#   - repo-a
#   - repo-b

# exclude:         # optional — always skip these repos
#   - .github
#   - .fullsend
#   - archived-repo
```

## Failures

When any repo fails to assess, the runner writes `runner/failed-{org}.yaml` (same format as the org YAML) and commits it to the repo. On a fully clean run the file is removed.

**Re-run failures locally:**

```bash
python runner/assess.py --from-file runner/failed-{your-org}.yaml
```

**Re-run failures via GitHub Actions:**

1. Go to **Actions → Assess repos (manual) → Run workflow**
2. In the `from_file` field enter the path to the failure file, e.g.:
   ```
   runner/failed-konflux-ci.yaml
   ```
3. Click **Run workflow** — only the previously failed repos will be assessed

## Editing an org YAML

Org config files live in `runner/orgs/{org}.yaml`.

```yaml
org: your-org-name

# repos:           # pin to a specific list — omit to discover all public repos
#   - repo-a
#   - repo-b

exclude:           # always skip these (hidden dirs, archived repos, etc.)
  - .github
  - .fullsend
```

**Adding repos:** either add them to the `repos` list (pinned mode) or remove them from `exclude` (discovery mode).

**Excluding repos:** add the repo name to `exclude`. This persists across every run including scheduled ones.

**Switching from pinned to discovery mode:** delete the `repos` key entirely — the runner will call the GitHub API to find all public repos in `org`, minus anything in `exclude`.

After editing, commit and push the YAML file. The next scheduled run or manual dispatch will pick up the changes automatically.

## DevLake Integration

Configure an **AgentReady connection** in DevLake pointing to this repo:

| Field | Value |
|-------|-------|
| Submissions Repo | `your-org/agentready-scores` |
| Submissions Path | `submissions` |
| Branch | `main` |

DevLake discovers all `{org}/{repo}` scopes from the submissions tree and ingests the latest assessment for each.

## Notifications

Slack failure notifications are sent when `SLACK_NOTIFICATIONS = 'true'` is set as a repository variable and `SLACK_WEBHOOK_URL` is configured as a secret. Notifications fire on both manual and scheduled workflow failures.
