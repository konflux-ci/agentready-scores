#!/usr/bin/env python3
"""
AgentReady concurrent assessment runner.

Usage:
  --org <org>                     Discover and assess ALL public repos in the org
  --from-file FILE [FILE ...]     One or more YAML files (org + repos + exclude keys).
                                  Each file is processed independently — useful for
                                  multiple orgs or combining curated lists with retries.

Examples:
  # Single org, curated list
  python runner/assess.py --from-file runner/repos.yaml

  # Multiple orgs
  python runner/assess.py --from-file runner/orgs/konflux-ci.yaml runner/orgs/redhat-appstudio.yaml

  # Glob (all orgs)
  python runner/assess.py --from-file runner/orgs/*.yaml

  # Discover all public repos in an org
  python runner/assess.py --org konflux-ci

  # Re-run failures
  python runner/assess.py --from-file runner/failed-konflux-ci.yaml
"""
import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run agentready assessments concurrently",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--org",
        metavar="ORG",
        help="GitHub org — discovers all public repos automatically",
    )
    source_group.add_argument(
        "--from-file",
        nargs="+",
        metavar="PATH",
        help="One or more YAML files (org + repos + exclude). Each processed separately.",
    )

    parser.add_argument(
        "--workers", type=int, default=5,
        help="Max concurrent assessments (default: 5)"
    )
    parser.add_argument(
        "--retries", type=int, default=1,
        help="Retry attempts per failed repo (default: 1)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "submissions",
        help="Path to submissions directory (default: submissions/)"
    )
    return parser.parse_args()


def process_org(org, repos, exclusions, args, runner_lib):
    """Run assessments for one org, commit results, write failures."""
    load_exclusions = runner_lib["load_exclusions"]
    discover_org_repos = runner_lib["discover_org_repos"]
    run_batch = runner_lib["run_batch"]
    commit_results = runner_lib["commit_results"]
    write_failed_repos = runner_lib["write_failed_repos"]

    if not repos:
        print(f"No repos listed for {org}, discovering all public repos...")
        repos = discover_org_repos(org)
        if exclusions:
            before = len(repos)
            repos = [r for r in repos if r not in exclusions]
            print(f"Excluded {before - len(repos)} repo(s)")

    if not repos:
        print(f"No repos to assess for {org}. Skipping.")
        return 0, 0

    print(f"\nAssessing {len(repos)} repos in {org} with {args.workers} workers...")

    succeeded, failed = run_batch(
        org=org,
        repos=repos,
        output_dir=args.output_dir,
        workers=args.workers,
        retries=args.retries,
    )

    if succeeded:
        commit_results(REPO_ROOT, org, succeeded)

    failed_path = SCRIPT_DIR / f"failed-{org}.yaml"
    if failed:
        write_failed_repos(failed_path, org, failed)
        print(f"{len(failed)} repos failed. Written to {failed_path}")
    elif failed_path.exists():
        failed_path.unlink()

    return len(succeeded), len(failed)


def main():
    args = parse_args()

    sys.path.insert(0, str(SCRIPT_DIR))
    from runner_lib import (
        SchemaError,
        load_repos_from_file,
        load_exclusions,
        discover_org_repos,
        run_batch,
        commit_results,
        write_failed_repos,
    )

    runner_lib = {
        "load_exclusions": load_exclusions,
        "discover_org_repos": discover_org_repos,
        "run_batch": run_batch,
        "commit_results": commit_results,
        "write_failed_repos": write_failed_repos,
    }

    total_succeeded = total_failed = 0

    if args.from_file:
        for path_str in args.from_file:
            path = Path(path_str)
            print(f"\n--- Processing {path} ---")
            try:
                org, repos, exclusions = load_repos_from_file(path)
            except SchemaError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                sys.exit(2)
            s, f = process_org(org, repos, exclusions, args, runner_lib)
            total_succeeded += s
            total_failed += f
    else:
        org = args.org
        repos = []
        exclusions = set()
        # Apply exclusions from repos.yaml if present
        default_yaml = SCRIPT_DIR / "repos.yaml"
        if default_yaml.exists():
            exclusions = load_exclusions(default_yaml)
        s, f = process_org(org, repos, exclusions, args, runner_lib)
        total_succeeded += s
        total_failed += f

    print(f"\n=== Total: {total_succeeded} succeeded, {total_failed} failed ===")
    if total_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
