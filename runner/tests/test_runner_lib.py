"""Tests for runner_lib.py"""
import json
import os
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

# Ensure runner/ is on the path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from runner_lib import (
    SchemaError,
    _find_repo_config,
    _load_yaml_config,
    _prior_commit_hash,
    assess_repo,
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
        org, repos, exclusions, default_config = load_repos_from_file(repos_yaml)
        assert org == "my-org"
        assert repos == ["repo-a", "repo-b", "repo-c"]
        assert exclusions == set()

    def test_parses_failed_repos_yaml(self, failed_repos_yaml):
        org, repos, exclusions, default_config = load_repos_from_file(failed_repos_yaml)
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
        org, repos, exclusions, default_config = load_repos_from_file(f)
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
        org, repos, exclusions, default_config = load_repos_from_file(f)
        assert org == "my-org"
        assert repos == []
        assert exclusions == {"bad-repo"}

    def test_exclude_key_absent_returns_empty_set(self, repos_yaml):
        org, repos, exclusions, default_config = load_repos_from_file(repos_yaml)
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
        org, repos, exclusions, default_config = load_repos_from_file(f)
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
        org, repos, exclusions, default_config = load_repos_from_file(out)
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
        def fake_assess(org, repo, output_dir, default_config=None, adr_clone_dir_default=None):
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

        def fake_assess(org, repo, output_dir, default_config=None, adr_clone_dir_default=None):
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


# ---------------------------------------------------------------------------
# default_config resolution (schema + loading)
# ---------------------------------------------------------------------------

class TestDefaultConfig:
    def test_loads_fallback_config_referenced_by_default_config(self, tmp_path):
        adr_config = tmp_path / "default-config.yaml"
        adr_config.write_text(textwrap.dedent("""\
            adr_source:
              repo: konflux-ci/architecture
              path: ADR
        """))
        f = tmp_path / "repos.yaml"
        f.write_text(textwrap.dedent(f"""\
            org: my-org
            repos:
              - repo-a
            default_config: {adr_config}
        """))
        # default_config is resolved relative to RUNNER_DIR in production, but
        # accepts an absolute path here too since Path(RUNNER_DIR / abs_path)
        # collapses to abs_path.
        org, repos, exclusions, default_config = load_repos_from_file(f)
        assert default_config == {
            "adr_source": {"repo": "konflux-ci/architecture", "path": "ADR"}
        }

    def test_absent_default_config_returns_none(self, tmp_path):
        f = tmp_path / "repos.yaml"
        f.write_text("org: my-org\nrepos:\n  - repo-a\n")
        org, repos, exclusions, default_config = load_repos_from_file(f)
        assert default_config is None

    def test_missing_default_config_file_raises_schema_error(self, tmp_path):
        f = tmp_path / "repos.yaml"
        f.write_text(textwrap.dedent("""\
            org: my-org
            default_config: does/not/exist.yaml
        """))
        # Must fail on the file-not-found check, not be rejected as an unknown key.
        with pytest.raises(SchemaError, match="file not found"):
            load_repos_from_file(f)

    def test_default_config_non_string_raises_schema_error(self, tmp_path):
        f = tmp_path / "repos.yaml"
        f.write_text("org: my-org\ndefault_config: 123\n")
        with pytest.raises(SchemaError, match="'default_config' must be a string"):
            load_repos_from_file(f)


# ---------------------------------------------------------------------------
# _find_repo_config / _load_yaml_config
# ---------------------------------------------------------------------------

class TestFindRepoConfig:
    def test_subdirectory_config_found(self, tmp_path):
        cfg_dir = tmp_path / ".agentready" / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / ".agentready-config.yaml").write_text("adr_source:\n  repo: x\n")
        assert _find_repo_config(tmp_path) == cfg_dir / ".agentready-config.yaml"

    def test_root_config_found_when_no_subdirectory(self, tmp_path):
        (tmp_path / ".agentready-config.yaml").write_text("adr_source:\n  repo: x\n")
        assert _find_repo_config(tmp_path) == tmp_path / ".agentready-config.yaml"

    def test_subdirectory_wins_over_root(self, tmp_path):
        cfg_dir = tmp_path / ".agentready" / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / ".agentready-config.yaml").write_text("a: 1\n")
        (tmp_path / ".agentready-config.yaml").write_text("b: 2\n")
        assert _find_repo_config(tmp_path) == cfg_dir / ".agentready-config.yaml"

    def test_no_config_returns_none(self, tmp_path):
        assert _find_repo_config(tmp_path) is None


class TestLoadYamlConfig:
    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("adr_source:\n  repo: konflux-ci/architecture\n  path: ADR\n")
        assert _load_yaml_config(f) == {"adr_source": {"repo": "konflux-ci/architecture", "path": "ADR"}}

    def test_invalid_yaml_returns_none(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("not: valid: yaml: [\n")
        assert _load_yaml_config(f) is None

    def test_non_mapping_yaml_returns_none(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("- just\n- a\n- list\n")
        assert _load_yaml_config(f) is None

    def test_empty_file_returns_empty_dict(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("")
        assert _load_yaml_config(f) == {}


# ---------------------------------------------------------------------------
# run_batch — adr_source batch-level clone
# ---------------------------------------------------------------------------

class TestRunBatchAdrSource:
    def test_adr_source_present_clones_once_and_passes_to_workers(self, tmp_path):
        default_config = {"adr_source": {"repo": "konflux-ci/architecture", "path": "ADR"}}
        calls = []

        def fake_assess(org, repo, output_dir, default_config=None, adr_clone_dir_default=None):
            calls.append((repo, default_config, adr_clone_dir_default))
            return "ok"

        clone_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "git" and "clone" in cmd:
                clone_calls.append(cmd)
                Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0)

        with patch("runner_lib.assess_repo", side_effect=fake_assess), \
             patch("runner_lib.subprocess.run", side_effect=fake_run):
            succeeded, failed = run_batch(
                org="konflux-ci", repos=["repo-a", "repo-b"], output_dir=tmp_path,
                workers=2, retries=0, default_config=default_config,
            )

        assert sorted(succeeded) == ["repo-a", "repo-b"]
        assert failed == []
        # Exactly one clone of the ADR repo for the whole batch, not per-repo.
        adr_clones = [c for c in clone_calls if "architecture" in c[-2]]
        assert len(adr_clones) == 1
        # Every worker got the same non-None adr_clone_dir_default.
        dirs_seen = {c[2] for c in calls}
        assert len(dirs_seen) == 1
        assert list(dirs_seen)[0] is not None

    def test_no_adr_source_skips_clone(self, tmp_path):
        with patch("runner_lib.assess_repo", return_value="ok") as mock_assess, \
             patch("runner_lib.subprocess.run") as mock_run:
            run_batch(org="my-org", repos=["repo-a"], output_dir=tmp_path, workers=1, retries=0)

        mock_run.assert_not_called()
        assert mock_assess.call_args[0][4] is None  # adr_clone_dir_default positional arg

    def test_adr_clone_failure_continues_without_adr_source(self, tmp_path):
        default_config = {"adr_source": {"repo": "bad/repo", "path": "ADR"}}

        def fake_run(cmd, **kwargs):
            if cmd[0] == "git" and "clone" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return MagicMock(returncode=0)

        with patch("runner_lib.assess_repo", return_value="ok") as mock_assess, \
             patch("runner_lib.subprocess.run", side_effect=fake_run):
            succeeded, failed = run_batch(
                org="my-org", repos=["repo-a"], output_dir=tmp_path,
                workers=1, retries=0, default_config=default_config,
            )

        assert succeeded == ["repo-a"]
        assert mock_assess.call_args[0][4] is None  # clone failed — no adr_clone_dir_default


# ---------------------------------------------------------------------------
# assess_repo — config discovery + adr_source resolution
# ---------------------------------------------------------------------------

class TestAssessRepoConfigResolution:
    COMMIT = "deadbeef" * 5

    def _fake_check_output(self, cmd, **kwargs):
        if "rev-parse" in cmd:
            return (self.COMMIT + "\n").encode()
        return b"1000\n"

    def _fake_run(self, podman_cmds, capture_config_into=None):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "git" and "clone" in cmd:
                Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0)
            if cmd[0] == "podman":
                podman_cmds.append(cmd)
                if capture_config_into is not None:
                    for part in cmd:
                        if part.endswith(":/agentready-config.yaml:ro,z"):
                            host_path = Path(part.split(":")[0])
                            capture_config_into["contents"] = host_path.read_text()
                return MagicMock(returncode=0, stdout=b"", stderr=b"")
            return MagicMock(returncode=0)
        return fake_run

    def test_own_config_with_no_adr_source_is_mounted_as_is(self, tmp_path):
        podman_cmds = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "git" and "clone" in cmd:
                clone_dir = Path(cmd[-1])
                clone_dir.mkdir(parents=True, exist_ok=True)
                (clone_dir / ".agentready-config.yaml").write_text("exclude:\n  - foo\n")
                return MagicMock(returncode=0)
            if cmd[0] == "podman":
                podman_cmds.append(cmd)
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("runner_lib.subprocess.run", side_effect=fake_run), \
             patch("runner_lib.subprocess.check_output", side_effect=self._fake_check_output), \
             patch("runner_lib.glob.glob", return_value=["/fake/assessment-20260101-000000.json"]), \
             patch("runner_lib.os.path.islink", return_value=False), \
             patch("runner_lib.shutil.copy2"), \
             patch("runner_lib.Path.symlink_to"), \
             patch("runner_lib.Path.resolve", lambda self: self):
            result = assess_repo("konflux-ci", "some-repo", tmp_path)

        assert podman_cmds, "podman run should have been invoked"
        cmd = podman_cmds[0]
        assert "--config" in cmd
        assert cmd[cmd.index("--config") + 1] == "/agentready-config.yaml"
        assert any(":/agentready-config.yaml:ro,z" in part for part in cmd)

    def test_fallback_adr_source_used_when_no_own_config(self, tmp_path):
        default_config = {"adr_source": {"repo": "konflux-ci/architecture", "path": "ADR"}}
        adr_clone_dir = tmp_path / "adr-clone"
        adr_clone_dir.mkdir()
        podman_cmds = []
        captured = {}

        with patch("runner_lib.subprocess.run", side_effect=self._fake_run(podman_cmds, captured)), \
             patch("runner_lib.subprocess.check_output", side_effect=self._fake_check_output), \
             patch("runner_lib.glob.glob", return_value=["/fake/assessment-20260101-000000.json"]), \
             patch("runner_lib.os.path.islink", return_value=False), \
             patch("runner_lib.shutil.copy2"), \
             patch("runner_lib.Path.symlink_to"), \
             patch("runner_lib.Path.resolve", lambda self: self):
            result = assess_repo("konflux-ci", "some-repo", tmp_path, default_config, adr_clone_dir)

        assert podman_cmds, "podman run should have been invoked"
        cmd = podman_cmds[0]
        assert "--config" in cmd
        assert any(part == f"{adr_clone_dir}:/adr-repo:ro,z" for part in cmd)
        # Patched config content (captured pre-cleanup) should point adr_source.repo
        # at the container path, not the original GitHub org/repo shorthand.
        patched = yaml.safe_load(captured["contents"])
        assert patched["adr_source"]["repo"] == "/adr-repo"

    def test_no_own_config_and_no_default_config_passes_no_flag(self, tmp_path):
        podman_cmds = []

        with patch("runner_lib.subprocess.run", side_effect=self._fake_run(podman_cmds)), \
             patch("runner_lib.subprocess.check_output", side_effect=self._fake_check_output), \
             patch("runner_lib.glob.glob", return_value=["/fake/assessment-20260101-000000.json"]), \
             patch("runner_lib.os.path.islink", return_value=False), \
             patch("runner_lib.shutil.copy2"), \
             patch("runner_lib.Path.symlink_to"), \
             patch("runner_lib.Path.resolve", lambda self: self):
            result = assess_repo("konflux-ci", "some-repo", tmp_path)

        assert podman_cmds, "podman run should have been invoked"
        assert "--config" not in podman_cmds[0]
