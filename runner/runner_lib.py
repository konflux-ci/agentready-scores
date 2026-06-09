import glob
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests
import yaml


class SchemaError(ValueError):
    """Raised when a repos YAML file fails schema validation."""


def _validate_repos_yaml(data: dict, path: Path) -> None:
    """
    Validate the structure of a repos YAML file.

    Required keys:
      org   — non-empty string

    Optional keys:
      repos   — list of strings (repo names)
      exclude — list of strings (repo names to skip)

    Raises SchemaError with a descriptive message on any violation.
    """
    errors = []

    if not isinstance(data, dict):
        raise SchemaError(f"{path}: expected a YAML mapping at the top level, got {type(data).__name__}")

    # org
    org = data.get("org")
    if not org:
        errors.append("'org' is required and must be a non-empty string")
    elif not isinstance(org, str):
        errors.append(f"'org' must be a string, got {type(org).__name__}")

    # repos (optional)
    repos = data.get("repos")
    if repos is not None:
        if not isinstance(repos, list):
            errors.append(f"'repos' must be a list, got {type(repos).__name__}")
        else:
            bad = [r for r in repos if not isinstance(r, str)]
            if bad:
                errors.append(f"'repos' entries must be strings, got: {bad}")

    # exclude (optional)
    exclude = data.get("exclude")
    if exclude is not None:
        if not isinstance(exclude, list):
            errors.append(f"'exclude' must be a list, got {type(exclude).__name__}")
        else:
            bad = [r for r in exclude if not isinstance(r, str)]
            if bad:
                errors.append(f"'exclude' entries must be strings, got: {bad}")

    # unknown keys
    known = {"org", "repos", "exclude"}
    unknown = set(data.keys()) - known
    if unknown:
        errors.append(f"unknown key(s): {sorted(unknown)}")

    if errors:
        msg = f"{path}: schema validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        raise SchemaError(msg)


def load_repos_from_yaml(path: Path) -> Tuple[str, List[str], set]:
    """
    Load org, repo list, and exclusions from a YAML file.

    Expected structure:
        org: my-org
        repos:          # optional — if absent, caller should use org discovery
          - repo-a
          - repo-b
        exclude:        # optional — repos to skip in any mode
          - archived-repo

    Returns (org, repos, exclusions).
    If 'repos' is absent, returns an empty list — caller decides whether to
    discover repos from the org and apply the returned exclusions.

    Raises SchemaError if the file structure is invalid.
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    _validate_repos_yaml(data, path)

    org = data["org"]
    repos = data.get("repos") or []
    exclude = set(data.get("exclude") or [])

    if exclude and repos:
        repos = [r for r in repos if r not in exclude]

    return org, repos, exclude


def load_exclusions(path: Path) -> set:
    """Return the exclude set from a YAML file, empty set if key absent."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return set(data.get("exclude", []))


# Alias used by assess.py --from-file
load_repos_from_file = load_repos_from_yaml

# Backwards-compatible alias
load_demo_repos = load_repos_from_yaml


def discover_org_repos(org: str) -> List[str]:
    """Discover all public repos in a GitHub org via the API."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers,
            params={"type": "public", "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(r["name"] for r in batch)
        page += 1

    print(f"Discovered {len(repos)} public repos in {org}")
    return repos


# Backwards-compatible alias
discover_prod_repos = discover_org_repos


def _prior_commit_hash(latest_json: Path) -> Optional[str]:
    """
    Return repository.commit_hash from an existing assessment-latest.json,
    or None if the file doesn't exist or can't be parsed.
    """
    try:
        with open(latest_json.resolve()) as f:
            return json.load(f).get("repository", {}).get("commit_hash")
    except Exception:
        return None


def assess_repo(org: str, repo: str, output_dir: Path) -> str:
    """
    Clone repo, run agentready container, extract JSON, write to submissions dir.
    Returns the path of the assessment JSON written, or raises on failure.
    """
    repo_submissions_dir = output_dir / org / repo
    repo_submissions_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"agentready-{repo}-") as tmp:
        clone_dir = Path(tmp) / "repo"
        output_tmp = Path(tmp) / "output"
        output_tmp.mkdir()

        # Shallow clone
        subprocess.run(
            [
                "git", "clone", "--depth=1",
                f"https://github.com/{org}/{repo}.git",
                str(clone_dir),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )

        # Commit hash pre-check — skip if HEAD matches the last assessed commit
        head_hash = subprocess.check_output(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            timeout=10,
        ).decode().strip()
        existing_latest = repo_submissions_dir / "assessment-latest.json"
        prior_hash = _prior_commit_hash(existing_latest)
        if prior_hash and head_hash == prior_hash:
            return "skipped:unchanged"

        uid = subprocess.check_output(["id", "-u"]).decode().strip()
        gid = subprocess.check_output(["id", "-g"]).decode().strip()

        # Run agentready container
        subprocess.run(
            [
                "podman", "run", "--rm",
                "--user", f"{uid}:{gid}",
                "--userns=keep-id",
                "-e", "GIT_CONFIG_COUNT=1",
                "-e", "GIT_CONFIG_KEY_0=safe.directory",
                "-e", "GIT_CONFIG_VALUE_0=/repo",
                "-v", f"{clone_dir}:/repo:ro,z",
                "-v", f"{output_tmp}:/reports:z",
                "ghcr.io/ambient-code/agentready:latest",
                "assess", "/repo", "--output-dir", "/reports",
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )

        # Find timestamped assessment JSONs only (exclude symlinks like assessment-latest.json)
        all_json = glob.glob(str(output_tmp / "assessment-*.json"))
        json_files = [f for f in all_json if not os.path.islink(f)]
        if not json_files:
            # Fall back to resolving symlinks if no plain files found
            json_files = [str(Path(f).resolve()) for f in all_json if os.path.islink(f)]
        if not json_files:
            raise FileNotFoundError(
                f"No assessment JSON found in agentready output for {repo}"
            )

        # Take the most recent timestamped file
        json_files.sort()
        src_json = Path(json_files[-1])
        # Always use the real filename (resolve symlinks)
        src_json = src_json.resolve()

        dest_json = repo_submissions_dir / src_json.name
        shutil.copy2(src_json, dest_json)

        # Create/update the assessment-latest.json symlink pointing to the timestamped file
        symlink = repo_submissions_dir / "assessment-latest.json"
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()
        symlink.symlink_to(src_json.name)

        return str(dest_json)


def run_batch(
    org: str,
    repos: List[str],
    output_dir: Path,
    workers: int,
    retries: int,
) -> Tuple[List[str], List[str]]:
    """
    Run assessments concurrently. Returns (succeeded_repos, failed_repos).
    Retries failed repos up to `retries` times.
    """
    succeeded = []
    failed = list(repos)

    for attempt in range(retries + 1):
        if not failed:
            break
        if attempt > 0:
            print(f"\nRetry attempt {attempt} for {len(failed)} repos...")

        to_try = list(failed)
        failed = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(assess_repo, org, repo, output_dir): repo
                for repo in to_try
            }
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    result = future.result()
                    if result == "skipped:unchanged":
                        print(f"  ⏭  {org}/{repo} — unchanged, skipped")
                    else:
                        print(f"  ✓ {org}/{repo} → {result}")
                        succeeded.append(repo)
                except Exception as e:
                    print(f"  ✗ {org}/{repo}: {e}")
                    failed.append(repo)

    return succeeded, failed


def commit_results(repo_root: Path, org: str, repos: List[str]) -> None:
    """Stage and commit all new assessment files in one commit."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repo_list = ", ".join(repos[:5])
    if len(repos) > 5:
        repo_list += f" (+{len(repos) - 5} more)"

    subprocess.run(
        ["git", "add", "submissions/"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m",
         f"chore: assess {org} repos {date_str} — {repo_list}"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "push"],
        cwd=repo_root,
        check=True,
    )
    print(f"\nCommitted and pushed {len(repos)} assessment(s).")


def write_failed_repos(path: Path, org: str, repos: List[str]) -> None:
    """Write failed repos to a YAML file with the same structure as repos.yaml."""
    data = {"org": org, "repos": repos}
    with open(path, "w") as f:
        f.write(f"# Failed repos from {datetime.now(timezone.utc).isoformat()}\n")
        yaml.dump(data, f, default_flow_style=False)
