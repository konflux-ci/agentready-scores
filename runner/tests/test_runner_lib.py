"""Tests for runner_lib.py"""
import json
import os
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure runner/ is on the path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from runner_lib import (
    SchemaError,
    _prior_commit_hash,
    commit_results,
    discover_org_repos,
    load_exclusions,
    load_repos_from_file,
    run_batch,
    write_failed_repos,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repos_yaml(tmp_path):
    f = tmp_path / "repos.yaml"
    f.write_text(textwrap.dedent("""\
        org: my-org
        repos:
          - repo-a
          - repo-b
          - repo-c
    """))
    return f


@pytest.fixture
def failed_repos_yaml(tmp_path):
    f = tmp_path / "failed-repos.yaml"
    f.write_text(textwrap.dedent("""\
        # Failed repos from 2026-06-05
        org: my-org
        repos:
          - repo-x
          - repo-y
    """))
    return f


# ---------------------------------------------------------------------------
# load_repos_from_file (YAML format — same for repos.yaml and failed-repos.yaml)
# ---------------------------------------------------------------------------

class TestLoadReposFromFile:
    def test_returns_org_repos_and_exclusions(self, repos_yaml):
        org, repos, exclusions = load_repos_from_file(repos_yaml)
        assert org == "my-org"
        assert repos == ["repo-a", "repo-b", "repo-c"]
        assert exclusions == set()

    def test_parses_failed_repos_yaml(self, failed_repos_yaml):
        org, repos, exclusions = load_repos_from_file(failed_repos_yaml)
        assert org == "my-org"
        assert repos == ["repo-x", "repo-y"]

    def test_exclude_filters_repos(self, tmp_path):
        f = tmp_path / "repos.yaml"
        f.write_text(textwrap.dedent("""\
            org: my-org
            repos:
              - repo-a
              - repo-b
              - archived-repo
            exclude:
              - archived-repo
        """))
        org, repos, exclusions = load_repos_from_file(f)
        assert "archived-repo" not in repos
        assert repos == ["repo-a", "repo-b"]
        assert "archived-repo" in exclusions

    def test_no_repos_key_returns_empty_list_with_exclusions(self, tmp_path):
        f = tmp_path / "repos.yaml"
        f.write_text(textwrap.dedent("""\
            org: my-org
            exclude:
              - bad-repo
        """))
        org, repos, exclusions = load_repos_from_file(f)
        assert org == "my-org"
        assert repos == []
        assert exclusions == {"bad-repo"}

    def test_exclude_key_absent_returns_empty_set(self, repos_yaml):
        org, repos, exclusions = load_repos_from_file(repos_yaml)
        assert exclusions == set()

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_repos_from_file(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def _write(self, tmp_path, content):
        f = tmp_path / "repos.yaml"
        f.write_text(textwrap.dedent(content))
        return f

    def test_missing_org_raises(self, tmp_path):
        f = self._write(tmp_path, "repos:\n  - repo-a\n")
        with pytest.raises(SchemaError, match="'org' is required"):
            load_repos_from_file(f)

    def test_empty_org_raises(self, tmp_path):
        f = self._write(tmp_path, "org:\nrepos:\n  - repo-a\n")
        with pytest.raises(SchemaError, match="'org' is required"):
            load_repos_from_file(f)

    def test_org_non_string_raises(self, tmp_path):
        f = self._write(tmp_path, "org: 123\n")
        with pytest.raises(SchemaError, match="'org' must be a string"):
            load_repos_from_file(f)

    def test_repos_non_list_raises(self, tmp_path):
        f = self._write(tmp_path, "org: my-org\nrepos: not-a-list\n")
        with pytest.raises(SchemaError, match="'repos' must be a list"):
            load_repos_from_file(f)

    def test_repos_non_string_entries_raises(self, tmp_path):
        f = self._write(tmp_path, "org: my-org\nrepos:\n  - 42\n  - repo-b\n")
        with pytest.raises(SchemaError, match="'repos' entries must be strings"):
            load_repos_from_file(f)

    def test_exclude_non_list_raises(self, tmp_path):
        f = self._write(tmp_path, "org: my-org\nexclude: bad\n")
        with pytest.raises(SchemaError, match="'exclude' must be a list"):
            load_repos_from_file(f)

    def test_unknown_key_raises(self, tmp_path):
        f = self._write(tmp_path, "org: my-org\nrepos: []\ntypo_key: oops\n")
        with pytest.raises(SchemaError, match="unknown key"):
            load_repos_from_file(f)

    def test_top_level_non_mapping_raises(self, tmp_path):
        f = self._write(tmp_path, "- just-a-list\n")
        with pytest.raises(SchemaError, match="expected a YAML mapping"):
            load_repos_from_file(f)

    def test_valid_file_passes(self, tmp_path):
        f = self._write(tmp_path, "org: my-org\nrepos:\n  - repo-a\nexclude:\n  - bad\n")
        org, repos, exclusions = load_repos_from_file(f)
        assert org == "my-org"


class TestLoadExclusions:
    def test_returns_exclude_set(self, tmp_path):
        f = tmp_path / "repos.yaml"
        f.write_text("org: my-org\nrepos: []\nexclude:\n  - bad-repo\n")
        assert load_exclusions(f) == {"bad-repo"}

    def test_returns_empty_set_when_absent(self, repos_yaml):
        assert load_exclusions(repos_yaml) == set()


# ---------------------------------------------------------------------------
# discover_org_repos
# ---------------------------------------------------------------------------

class TestDiscoverOrgRepos:
    def test_paginates_until_empty(self):
        page1 = [{"name": "repo-a"}, {"name": "repo-b"}]
        page2 = []

        mock_resp1 = MagicMock()
        mock_resp1.json.return_value = page1
        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = page2

        with patch("runner_lib.requests.get", side_effect=[mock_resp1, mock_resp2]) as mock_get:
            repos = discover_org_repos("my-org")

        assert repos == ["repo-a", "repo-b"]
        assert mock_get.call_count == 2

    def test_uses_gh_token_from_env(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []

        with patch.dict(os.environ, {"GH_TOKEN": "test-token"}):
            with patch("runner_lib.requests.get", return_value=mock_resp) as mock_get:
                discover_org_repos("my-org")

        _, kwargs = mock_get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"

    def test_raises_on_api_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")

        with patch("runner_lib.requests.get", return_value=mock_resp):
            with pytest.raises(Exception, match="403"):
                discover_org_repos("my-org")


# ---------------------------------------------------------------------------
# write_failed_repos
# ---------------------------------------------------------------------------

class TestWriteFailedRepos:
    def test_writes_yaml_with_org_and_repos(self, tmp_path):
        import yaml as _yaml
        out = tmp_path / "failed-repos.yaml"
        write_failed_repos(out, "my-org", ["repo-x", "repo-y"])
        content = out.read_text()
        # Comment header present
        assert content.startswith("#")
        # Parseable as YAML with correct structure
        data = _yaml.safe_load(content)
        assert data["org"] == "my-org"
        assert data["repos"] == ["repo-x", "repo-y"]

    def test_output_can_be_reloaded_by_runner(self, tmp_path):
        out = tmp_path / "failed-my-org.yaml"
        write_failed_repos(out, "my-org", ["repo-x"])
        org, repos, exclusions = load_repos_from_file(out)
        assert org == "my-org"
        assert repos == ["repo-x"]
        assert exclusions == set()


# ---------------------------------------------------------------------------
# Commit hash pre-check / unchanged detection
# ---------------------------------------------------------------------------

class TestCommitHashPreCheck:
    def test_prior_commit_hash_reads_from_json(self, tmp_path):
        import json as _json
        f = tmp_path / "assessment-latest.json"
        real = tmp_path / "assessment-20260101-000000.json"
        real.write_text(_json.dumps({"repository": {"commit_hash": "abc123"}}))
        f.symlink_to(real.name)
        assert _prior_commit_hash(f) == "abc123"

    def test_prior_commit_hash_missing_file_returns_none(self, tmp_path):
        assert _prior_commit_hash(tmp_path / "assessment-latest.json") is None

    def test_assess_repo_skips_when_commit_unchanged(self, tmp_path):
        """assess_repo returns 'skipped:unchanged' when HEAD matches stored commit_hash."""
        import json as _json
        from unittest.mock import patch, MagicMock

        commit = "deadbeef" * 5

        # Pre-populate submissions dir with an existing assessment
        submissions = tmp_path / "submissions"
        repo_dir = submissions / "my-org" / "my-repo"
        repo_dir.mkdir(parents=True)
        existing = repo_dir / "assessment-20260101-000000.json"
        existing.write_text(_json.dumps({"repository": {"commit_hash": commit}, "timestamp": "old"}))
        latest = repo_dir / "assessment-latest.json"
        latest.symlink_to(existing.name)

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0)

        def fake_check_output(cmd, **kwargs):
            if "rev-parse" in cmd:
                return (commit + "\n").encode()
            return b"1000\n"

        with patch("runner_lib.subprocess.run", side_effect=fake_run), \
             patch("runner_lib.subprocess.check_output", side_effect=fake_check_output):
            from runner_lib import assess_repo
            result = assess_repo("my-org", "my-repo", submissions)

        assert result == "skipped:unchanged"

    def test_run_batch_skipped_not_in_succeeded(self, tmp_path):
        """Repos returning skipped:unchanged are not added to succeeded list."""
        with patch("runner_lib.assess_repo", return_value="skipped:unchanged"):
            succeeded, failed = run_batch(
                org="my-org",
                repos=["repo-a"],
                output_dir=tmp_path,
                workers=1,
                retries=0,
            )
        assert succeeded == []
        assert failed == []


# ---------------------------------------------------------------------------
# run_batch
# ---------------------------------------------------------------------------

class TestRunBatch:
    def test_returns_succeeded_and_failed(self, tmp_path):
        def fake_assess(org, repo, output_dir):
            if repo == "bad-repo":
                raise RuntimeError("clone failed")
            return str(output_dir / org / repo / "assessment.json")

        with patch("runner_lib.assess_repo", side_effect=fake_assess):
            succeeded, failed = run_batch(
                org="my-org",
                repos=["repo-a", "bad-repo"],
                output_dir=tmp_path,
                workers=2,
                retries=0,
            )

        assert "repo-a" in succeeded
        assert "bad-repo" in failed

    def test_retries_failed_repos(self, tmp_path):
        call_count = {"bad": 0}

        def fake_assess(org, repo, output_dir):
            if repo == "flaky":
                call_count["bad"] += 1
                if call_count["bad"] < 2:
                    raise RuntimeError("transient error")
            return "ok"

        with patch("runner_lib.assess_repo", side_effect=fake_assess):
            succeeded, failed = run_batch(
                org="my-org",
                repos=["flaky"],
                output_dir=tmp_path,
                workers=1,
                retries=1,
            )

        assert "flaky" in succeeded
        assert failed == []
        assert call_count["bad"] == 2


# ---------------------------------------------------------------------------
# commit_results
# ---------------------------------------------------------------------------

class TestCommitResults:
    def test_runs_git_add_commit_push(self, tmp_path):
        with patch("runner_lib.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            commit_results(tmp_path, "my-org", ["repo-a", "repo-b"])

        calls = [str(c) for c in mock_run.call_args_list]
        assert any("git" in c and "add" in c for c in calls)
        assert any("git" in c and "commit" in c for c in calls)
        assert any("git" in c and "push" in c for c in calls)

    def test_commit_message_includes_org_and_repos(self, tmp_path):
        with patch("runner_lib.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            commit_results(tmp_path, "my-org", ["repo-a"])

        commit_call = next(
            c for c in mock_run.call_args_list
            if "'git', 'commit'" in str(c)
        )
        msg = str(commit_call)
        assert "my-org" in msg
