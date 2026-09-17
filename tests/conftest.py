"""Shared fixtures and git helpers for the git_commit_editor test suite.

`test_gitops.py` and `test_app.py` both need throwaway git repositories and a
handful of small git-plumbing queries; those live here so both modules (and
`test_ui.py`) can use them without duplicating them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from git_commit_editor import app as appmod
from git_commit_editor import gitops


def run_git(repo, *args, env=None):
    """Run `git -C repo <args>` and return stripped stdout, raising on failure."""
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def commit(repo, filename, content, message, date="2026-01-01T12:00:00"):
    """Write `filename`, stage it, and commit with a fixed author/committer identity."""
    (Path(repo) / filename).write_text(content)
    run_git(repo, "add", filename)
    env = {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.com",
        "GIT_COMMITTER_DATE": date,
    }
    run_git(repo, "commit", "-m", message, env=env)
    return run_git(repo, "rev-parse", "HEAD")


def new_repo(tmp):
    """Initialise a fresh git repository (branch `main`) rooted at `tmp`."""
    repo_path = Path(tmp)
    run_git(repo_path, "init", "-q", "-b", "main")
    run_git(repo_path, "config", "user.name", "Test")
    run_git(repo_path, "config", "user.email", "test@example.com")
    return repo_path


def subjects(repo, branch="main"):
    return run_git(repo, "log", "--format=%s", branch).splitlines()


def trees(repo, branch="main"):
    return run_git(repo, "log", "--format=%T", branch).splitlines()


def backup_refs(repo):
    # Must track gitops.BACKUP_NAMESPACE: a hardcoded prefix that drifted from
    # it would make every "no orphan backup" assertion pass vacuously.
    return run_git(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/heads/{gitops.BACKUP_NAMESPACE}/",
    ).splitlines()


@pytest.fixture
def repo(tmp_path):
    """An initialised, empty git repository (branch `main`) under tmp_path."""
    return new_repo(tmp_path)


@pytest.fixture
def client():
    """A plain Flask test client for the app."""
    return appmod.app.test_client()


@pytest.fixture
def post_json(client):
    """POST helper that carries a valid CSRF token and JSON content type by
    default, since every /api/* route refuses requests missing either."""

    def _post_json(path, json=None, headers=None, **kwargs):
        hdrs = {"X-CSRF-Token": appmod.CSRF_TOKEN}
        if headers:
            hdrs.update(headers)
        return client.post(path, json=json, headers=hdrs, **kwargs)

    return _post_json
