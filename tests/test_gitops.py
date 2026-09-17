"""Tests for gitops.rewrite_messages and friends.

Run under pytest (`pytest test_gitops.py`), or directly as a script
(`python test_gitops.py`), which delegates to pytest via the __main__ guard
below. Shared git-repo helpers and fixtures live in conftest.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import backup_refs, commit, new_repo, run_git, subjects, trees
from git_commit_editor import gitops
from git_commit_editor.gitops import GitError


def test_linear_edit_middle(repo):
    a = commit(repo, "f.txt", "1", "first commit")
    b = commit(repo, "f.txt", "1\n2", "secnd comit")  # typo to fix
    c = commit(repo, "f.txt", "1\n2\n3", "third commit")
    trees_before = trees(repo)

    res = gitops.rewrite_messages(
        str(repo), "main", {b: "second commit (fixed)"}, limit=10
    )

    assert res.changed
    # A unchanged; B and C rewritten (C because its parent B changed).
    assert {r["old"] for r in res.rewritten} == {b, c}
    assert subjects(repo) == [
        "third commit",
        "second commit (fixed)",
        "first commit",
    ]
    # Oldest commit (A) is untouched.
    assert run_git(repo, "rev-parse", "main~2") == a
    # Trees are identical -> working tree stays consistent.
    assert trees(repo) == trees_before
    # Backup ref points at the old tip.
    assert run_git(repo, "rev-parse", res.backup_ref) == c
    # Reflog records the move.
    assert "reword" in run_git(repo, "reflog", "-1", "main").lower()


def test_edit_tip_only(repo):
    a = commit(repo, "f.txt", "1", "first commit")
    b = commit(repo, "f.txt", "1\n2", "second commit")
    c = commit(repo, "f.txt", "1\n2\n3", "third comit")  # typo

    res = gitops.rewrite_messages(str(repo), "main", {c: "third commit"}, limit=10)

    # Only the tip changed; ancestors keep their exact SHAs.
    assert {r["old"] for r in res.rewritten} == {c}
    assert run_git(repo, "rev-parse", "main~1") == b
    assert run_git(repo, "rev-parse", "main~2") == a
    assert subjects(repo)[0] == "third commit"


def test_multiline_message_preserved(repo):
    c = commit(repo, "f.txt", "1", "old subject")
    body = "New subject line\n\nA body paragraph.\n\n- bullet one\n- bullet two"

    gitops.rewrite_messages(str(repo), "main", {c: body}, limit=10)

    stored = run_git(repo, "log", "-1", "--format=%B", "main").rstrip("\n")
    assert stored == body


def test_leading_blank_lines_are_stripped_like_git_commit(repo):
    c = commit(repo, "f.txt", "1", "old subject")

    gitops.rewrite_messages(
        str(repo), "main", {c: "\n\nreal subject\n\nbody"}, limit=10
    )

    stored = gitops.get_commits(str(repo), "main", limit=10)[0]
    assert stored.message == "real subject\n\nbody"
    assert stored.subject == "real subject"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Subject\n\nbody\n", "Subject\n\nbody"),
        ("Subject\n\nbody\n\n\n", "Subject\n\nbody"),
        ("Subject\n\nbody\n \n", "Subject\n\nbody"),
        ("Subject\n\nbody\n\t\n  \n", "Subject\n\nbody"),
        ("Subject\r\n\r\nbody\r\n", "Subject\n\nbody"),
        ("\n \nSubject\n\nbody", "Subject\n\nbody"),
        ("Subject\n\nbody  ", "Subject\n\nbody  "),  # trailing spaces on text stay
    ],
    ids=[
        "one_newline",
        "blank_lines",
        "space_only_line",
        "tab_and_spaces_lines",
        "crlf",
        "leading_blanks",
        "inline_trailing_spaces_kept",
    ],
)
def test_normalize_message_strips_blank_and_whitespace_only_edge_lines(raw, expected):
    assert gitops.normalize_message(raw) == expected


def test_noop_when_message_unchanged(repo):
    c = commit(repo, "f.txt", "1", "unchanged subject")
    before = run_git(repo, "rev-parse", "main")

    res = gitops.rewrite_messages(str(repo), "main", {c: "unchanged subject"}, limit=10)

    assert res.changed is False
    assert run_git(repo, "rev-parse", "main") == before


def test_author_and_committer_identity_and_dates_preserved(repo):
    """Only the message changes: a rewrite is never stamped with the current
    committer or the current time, so the committer date is pinned too."""
    c = commit(repo, "f.txt", "1", "msg", date="2020-05-05T08:09:10 +0000")
    fmt = "--format=%an|%ae|%aI|%cn|%ce|%cI"
    before = run_git(repo, "log", "-1", fmt, "main")
    assert before.endswith("|2020-05-05T08:09:10Z"), before  # fixture sanity

    gitops.rewrite_messages(str(repo), "main", {c: "new msg"}, limit=10)

    after = run_git(repo, "log", "-1", fmt, "main")
    assert before == after, f"{before!r} != {after!r}"


def test_merge_history(repo):
    commit(repo, "base.txt", "base", "base commit")
    run_git(repo, "checkout", "-q", "-b", "side")
    s = commit(repo, "side.txt", "side", "side comit")  # typo on side
    run_git(repo, "checkout", "-q", "main")
    commit(repo, "main.txt", "main", "main work")
    env = {
        "GIT_AUTHOR_DATE": "2026-01-02T12:00:00",
        "GIT_COMMITTER_DATE": "2026-01-02T12:00:00",
    }
    run_git(repo, "merge", "--no-ff", "-m", "merge side", "side", env=env)
    merge_sha = run_git(repo, "rev-parse", "main")
    parents_before = run_git(repo, "rev-list", "--parents", "-n", "1", merge_sha)
    trees_before = set(trees(repo))

    # Edit the side-branch commit message; the merge must be replayed and keep
    # both parents (one remapped, one unchanged).
    res = gitops.rewrite_messages(str(repo), "main", {s: "side commit"}, limit=10)

    assert res.changed
    # Merge commit still has two parents.
    new_merge = run_git(repo, "rev-parse", "main")
    new_parents = run_git(repo, "rev-list", "--parents", "-n", "1", new_merge).split()
    assert len(new_parents) == 3, new_parents  # self + 2 parents
    assert "side commit" in run_git(repo, "log", "--format=%s", "main")
    # No trees changed.
    assert set(trees(repo)) == trees_before
    assert parents_before != run_git(
        repo, "rev-list", "--parents", "-n", "1", new_merge
    )


def test_out_of_range_rejected(repo):
    old = commit(repo, "f.txt", "1", "old")
    for i in range(5):
        commit(repo, "f.txt", f"1\n{i}", f"commit {i}")

    # Only the last 2 are in range; editing `old` must be rejected.
    with pytest.raises(GitError, match="not in the editable range"):
        gitops.rewrite_messages(str(repo), "main", {old: "new"}, limit=2)


def test_stale_tip_rejected(repo):
    c = commit(repo, "f.txt", "1", "only")

    with pytest.raises(GitError, match="moved since it was loaded"):
        gitops.rewrite_messages(
            str(repo), "main", {c: "new"}, limit=10, expected_old_tip="0" * 40
        )


def test_abbreviated_expected_old_tip_is_refused_up_front(repo):
    c = commit(repo, "f.txt", "1", "only")

    with pytest.raises(GitError, match="Invalid expected_old_tip"):
        gitops.rewrite_messages(
            str(repo), "main", {c: "new"}, limit=10, expected_old_tip=c[:9]
        )


def test_abbreviated_edit_key_is_refused_as_invalid(repo):
    c = commit(repo, "f.txt", "1", "only")

    with pytest.raises(GitError, match="Invalid commit id"):
        gitops.rewrite_messages(str(repo), "main", {c[:9]: "new"}, limit=10)


@pytest.mark.parametrize(
    "message",
    [pytest.param("", id="empty"), pytest.param("   \n\n", id="whitespace_only")],
)
def test_empty_message_is_refused(repo, message):
    # commit-tree performs no cleanup, so an empty message would be stored
    # verbatim; the refusal has to happen here.
    c = commit(repo, "f.txt", "1", "only")

    with pytest.raises(GitError, match="would have an empty message"):
        gitops.rewrite_messages(str(repo), "main", {c: message}, limit=10)

    assert run_git(repo, "rev-parse", "main") == c
    assert backup_refs(repo) == []


def test_message_over_max_length_is_refused(repo):
    c = commit(repo, "f.txt", "1", "only")
    message = "x" * (gitops.MAX_MSG_LEN + 1)

    with pytest.raises(GitError, match="too long"):
        gitops.rewrite_messages(str(repo), "main", {c: message}, limit=10)

    assert run_git(repo, "rev-parse", "main") == c


def test_get_commits_and_branches(repo):
    commit(repo, "f.txt", "1", "first")
    commit(repo, "f.txt", "1\n2", "second\n\nwith body")
    run_git(repo, "branch", "feature")

    info = gitops.list_branches(str(repo))
    commits = gitops.get_commits(str(repo), "main", limit=10)

    assert set(info["branches"]) == {"main", "feature"}
    assert info["current"] == "main"
    assert [c.subject for c in commits] == ["second", "first"]
    assert commits[0].message == "second\n\nwith body"
    assert commits[0].is_merge is False


def test_branch_list_is_unaffected_by_a_tag_of_the_same_name(repo):
    commit(repo, "f.txt", "1", "first")
    commit(repo, "f.txt", "1\n2", "second")
    run_git(repo, "tag", "main", "HEAD~1")

    info = gitops.list_branches(str(repo))

    assert info["branches"] == ["main"]


def test_current_branch_is_unaffected_by_a_tag_of_the_same_name(repo):
    """`symbolic-ref --short HEAD` disambiguates to `heads/main` once a tag
    called main exists, so the checked-out branch would stop matching."""
    commit(repo, "f.txt", "1", "first")
    commit(repo, "f.txt", "1\n2", "second")
    run_git(repo, "tag", "main", "HEAD~1")

    assert gitops.list_branches(str(repo))["current"] == "main"
    state = gitops.get_repo_state(str(repo), "main")
    assert state["current_branch"] == "main"
    assert state["is_checked_out"] is True


def test_commits_come_from_the_branch_when_a_tag_shares_its_name(repo):
    commit(repo, "f.txt", "1", "first")
    tip = commit(repo, "f.txt", "1\n2", "second")
    run_git(repo, "tag", "main", "HEAD~1")

    commits = gitops.get_commits(str(repo), "main", limit=10)

    assert [c.subject for c in commits] == ["second", "first"]
    assert commits[0].sha == tip


def test_detect_default_branch_and_branch_scope(repo):
    # main with two commits...
    commit(repo, "f.txt", "1", "main one")
    commit(repo, "f.txt", "1\n2", "main two")
    # ...then a feature branch with its own commits on top.
    run_git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "g.txt", "a", "feat one")
    commit(repo, "g.txt", "a\nb", "feat two")

    assert gitops.detect_default_branch(str(repo)) == "main"

    # With base=main, only the feature branch's own commits are listed.
    scoped = gitops.get_commits(str(repo), "feature", base="main", limit=50)
    assert [c.subject for c in scoped] == ["feat two", "feat one"]

    # Editing a feature commit must not touch main's commits.
    main_before = run_git(repo, "rev-parse", "main")
    feat_one = scoped[1].sha
    gitops.rewrite_messages(
        str(repo), "feature", {feat_one: "feat one (reworded)"}, base="main", limit=50
    )

    assert run_git(repo, "rev-parse", "main") == main_before
    assert "feat one (reworded)" in run_git(repo, "log", "--format=%s", "feature")
    # main's messages are untouched.
    assert subjects(repo, "main") == ["main two", "main one"]


def test_default_branch_falls_back_to_remote_tracking_ref(repo):
    run_git(repo, "checkout", "-q", "-b", "work")  # unborn main: never created
    tip = commit(repo, "f.txt", "1", "one")
    run_git(repo, "update-ref", "refs/remotes/origin/main", tip)

    assert gitops.list_branches(str(repo))["branches"] == ["work"]  # no local main
    assert gitops.detect_default_branch(str(repo)) == "origin/main"


def test_atomic_helper_aborts_cleanly_on_bad_cas(repo):
    """Calls the private `_apply_rewrite_atomic` directly.

    The CAS check inside the transaction can never be reached through the
    public `rewrite_messages`, because its own pre-check (comparing
    `expected_old_tip` before the transaction starts) fires first and raises
    before the atomic helper is even called.
    """
    c1 = commit(repo, "f.txt", "1", "one")
    c2 = commit(repo, "f.txt", "1\n2", "two")
    new = run_git(
        repo,
        "commit-tree",
        run_git(repo, "rev-parse", "main^{tree}"),
        "-p",
        c1,
        "-m",
        "reworded",
    )

    # Pass a WRONG old_tip (c1) while main is at c2 -> CAS must abort.
    with pytest.raises(GitError):
        gitops._apply_rewrite_atomic(
            str(repo),
            "main",
            old_tip=c1,
            new_tip=new,
            backup_ref=f"refs/heads/{gitops.BACKUP_NAMESPACE}/should-not-exist",
        )

    assert run_git(repo, "rev-parse", "main") == c2  # unchanged
    assert backup_refs(repo) == []  # no orphan backup


def test_stale_save_creates_no_backup(repo):
    commit(repo, "f.txt", "1", "a")
    b = commit(repo, "f.txt", "1\n2", "b")
    t0 = run_git(repo, "rev-parse", "main")
    # Concurrent change moves the branch after t0 was "loaded".
    commit(repo, "f.txt", "1\n2\n3", "concurrent")

    with pytest.raises(GitError, match="moved since it was loaded"):
        gitops.rewrite_messages(
            str(repo), "main", {b: "b new"}, limit=10, expected_old_tip=t0
        )

    assert backup_refs(repo) == []  # rejected save leaves no backup litter


def test_root_commit_reword(repo):
    c = commit(repo, "f.txt", "1", "original root")

    res = gitops.rewrite_messages(str(repo), "main", {c: "new root"}, limit=10)

    assert res.changed
    assert subjects(repo) == ["new root"]
    # Still a parentless root commit.
    parents = run_git(repo, "rev-list", "--parents", "-n", "1", "main").split()
    assert len(parents) == 1, parents


def test_compound_remap_edit_ancestor_and_descendant(repo):
    a = commit(repo, "f.txt", "1", "a")
    b = commit(repo, "f.txt", "1\n2", "b")
    c = commit(repo, "f.txt", "1\n2\n3", "c")

    res = gitops.rewrite_messages(str(repo), "main", {a: "a new", c: "c new"}, limit=10)

    # a (msg), b (parent changed), c (msg + parent changed) all rewritten.
    assert {r["old"] for r in res.rewritten} == {a, b, c}
    assert subjects(repo) == ["c new", "b", "a new"]


def test_field_separator_and_unicode_roundtrip(repo):
    c = commit(repo, "f.txt", "1", "x")
    weird = "fix: café \U0001f600 with \x1f unit-sep\n\nbody ✅ end"

    gitops.rewrite_messages(str(repo), "main", {c: weird}, limit=10)

    got = gitops.get_commits(str(repo), "main", limit=10)[0].message
    assert got == "fix: café \U0001f600 with \x1f unit-sep\n\nbody ✅ end", repr(got)


def test_commit_with_separator_byte_in_author_name_parses_correctly(repo):
    (repo / "f.txt").write_text("1")
    run_git(repo, "add", "f.txt")
    run_git(
        repo,
        "commit",
        "-q",
        "-m",
        "weird author",
        env={"GIT_AUTHOR_NAME": "Ann\x1fUnit", "GIT_AUTHOR_EMAIL": "ann@example.com"},
    )

    c = gitops.get_commits(str(repo), "main", limit=5)[0]

    assert c.author_name == "Ann\x1fUnit"
    assert c.author_email == "ann@example.com"
    assert c.message == "weird author"
    assert c.signed is False


def test_rewrite_with_dirty_index(repo):
    commit(repo, "f.txt", "1", "first")
    c = commit(repo, "f.txt", "1\n2", "secnd")
    # Stage an uncommitted change on the checked-out branch.
    (repo / "staged.txt").write_text("WIP")
    run_git(repo, "add", "staged.txt")

    gitops.rewrite_messages(str(repo), "main", {c: "second"}, limit=10)

    # The staged change survives and the index is still valid.
    cached = run_git(repo, "diff", "--cached", "--name-only")
    assert "staged.txt" in cached
    assert subjects(repo)[0] == "second"


@pytest.mark.parametrize(
    "marker, is_dir",
    [
        ("rebase-merge", True),
        ("rebase-apply", True),
        ("MERGE_HEAD", False),
        ("CHERRY_PICK_HEAD", False),
        ("REVERT_HEAD", False),
        ("BISECT_LOG", False),
    ],
)
def test_in_progress_operations_block_rewrite(repo, marker, is_dir):
    c = commit(repo, "f.txt", "1", "only")
    path = Path(run_git(repo, "rev-parse", "--absolute-git-dir")) / marker
    if is_dir:
        path.mkdir()
    else:
        path.write_text(c + "\n")

    with pytest.raises(GitError, match="operation in progress"):
        gitops.rewrite_messages(str(repo), "main", {c: "new"}, limit=10)

    assert run_git(repo, "rev-parse", "main") == c
    assert backup_refs(repo) == []


def test_conflicted_rebase_in_a_linked_worktree_blocks_rewrite(repo, tmp_path):
    """The rebase's own state lives in the linked worktree, not the main one;
    rewriting through it would be undone by `git rebase --abort` over there."""
    commit(repo, "f.txt", "base", "base")
    run_git(repo, "branch", "feature")
    commit(repo, "f.txt", "main side", "main change")
    wt = tmp_path / "wt"
    run_git(repo, "worktree", "add", "-q", str(wt), "feature")
    feat = commit(wt, "f.txt", "feature side", "feature change")
    rebase = subprocess.run(
        ["git", "-C", str(wt), "rebase", "main"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rebase.returncode != 0, "expected the rebase to stop on a conflict"

    with pytest.raises(GitError, match="operation in progress"):
        gitops.rewrite_messages(str(repo), "feature", {feat: "reworded"}, limit=10)

    assert run_git(repo, "rev-parse", "refs/heads/feature") == feat
    assert backup_refs(repo) == []


def test_conflicted_rebase_in_a_worktree_whose_path_has_a_newline_still_blocks(
    repo, tmp_path
):
    """Without `-z`, `worktree list --porcelain` C-quotes such a path
    (`"…/wt\\nline2"`), which no longer names a directory on disk — so the
    conflicted rebase in it would be skipped as "pruned" and the rewrite let
    through."""
    commit(repo, "f.txt", "base", "base")
    run_git(repo, "branch", "feature")
    commit(repo, "f.txt", "main side", "main change")
    wt = tmp_path / "wt\nline2"
    run_git(repo, "worktree", "add", "-q", str(wt), "feature")
    feat = commit(wt, "f.txt", "feature side", "feature change")
    rebase = subprocess.run(
        ["git", "-C", str(wt), "rebase", "main"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rebase.returncode != 0, "expected the rebase to stop on a conflict"

    assert any(
        os.path.realpath(p) == os.path.realpath(str(wt))
        for p, _ in gitops._worktrees(str(repo))
    )
    with pytest.raises(GitError, match="operation in progress"):
        gitops.rewrite_messages(str(repo), "feature", {feat: "reworded"}, limit=10)

    assert run_git(repo, "rev-parse", "refs/heads/feature") == feat
    assert backup_refs(repo) == []


def test_worktree_detection(repo, tmp_path):
    commit(repo, "f.txt", "1", "base")
    run_git(repo, "branch", "feature")
    wt = tmp_path / "wt"

    run_git(repo, "worktree", "add", str(wt), "feature")

    paths = gitops.branch_checked_out_elsewhere(str(repo), "feature")
    assert any(os.path.realpath(p) == os.path.realpath(str(wt)) for p in paths), paths
    # The branch checked out in the main worktree is not "elsewhere".
    assert gitops.branch_checked_out_elsewhere(str(repo), "main") == []


def test_dry_run_preview_moves_nothing(repo):
    a = commit(repo, "f.txt", "1", "a")
    commit(repo, "f.txt", "1\n2", "b")
    commit(repo, "f.txt", "1\n2\n3", "c")
    tip = run_git(repo, "rev-parse", "main")

    res = gitops.rewrite_messages(
        str(repo), "main", {a: "a new"}, limit=10, dry_run=True
    )

    assert res.changed and res.dry_run
    assert len(res.rewritten) == 3  # a + 2 descendants replayed in preview
    assert res.backup_ref is None
    assert run_git(repo, "rev-parse", "main") == tip  # nothing moved
    assert backup_refs(repo) == []


# --------------------------------------------------------------------------- #
# Input validators                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["-x", "a b", "a\tb", "\x01x"])
def test_safe_ref_rejects_malformed_names(bad):
    with pytest.raises(GitError):
        gitops.safe_ref(bad, label="x")


def test_safe_ref_allows_empty_unless_required():
    assert gitops.safe_ref("", label="x") is None
    assert gitops.safe_ref(None, label="x") is None

    with pytest.raises(GitError):
        gitops.safe_ref("", label="x", required=True)


@pytest.mark.parametrize("bad", ["xyz", "12", "g" * 40, "main", ""])
def test_validate_sha_rejects_non_hex(bad):
    with pytest.raises(GitError):
        gitops.validate_sha(bad, "sha")


@pytest.mark.parametrize("sha", ["a" * 40, "0" * 64])
def test_validate_sha_accepts_full_length_ids(sha):
    assert gitops.validate_sha(sha, "sha") == sha


@pytest.mark.parametrize("short", ["deadbeef", "a" * 39, "a" * 41, "a" * 63])
def test_validate_sha_rejects_abbreviated_ids(short):
    with pytest.raises(GitError, match="Invalid sha"):
        gitops.validate_sha(short, "sha")


@pytest.mark.parametrize("bad", ["HEAD", "main~1", "origin/main", "nope", "-x"])
def test_validate_local_branch_rejects_non_branches(repo, bad):
    commit(repo, "f.txt", "1", "c")
    run_git(repo, "branch", "feature")

    with pytest.raises(GitError):
        gitops.validate_local_branch(str(repo), bad)


def test_validate_local_branch_accepts_existing_branch(repo):
    commit(repo, "f.txt", "1", "c")
    run_git(repo, "branch", "feature")

    assert gitops.validate_local_branch(str(repo), "feature") == "feature"


@pytest.mark.parametrize(
    "target",
    ["refs/heads/main", "refs/remotes/origin/protected"],
    ids=["local", "remote"],
)
def test_symbolic_branch_is_refused(repo, target):
    """`refs/heads/alias` -> <target> is a symbolic ref. `show-ref --verify`
    and `rev-parse` both resolve straight through it, so without a dedicated
    check a rewrite of `alias` would move its *target* instead. (The
    end-to-end refusal through /api/save is in test_app.py.)"""
    commit(repo, "f.txt", "1", "one")
    tip = commit(repo, "f.txt", "1\n2", "two")
    run_git(repo, "update-ref", "refs/remotes/origin/protected", tip)
    run_git(repo, "symbolic-ref", "refs/heads/alias", target)

    with pytest.raises(GitError, match="symbolic"):
        gitops.validate_local_branch(str(repo), "alias")

    assert run_git(repo, "symbolic-ref", "refs/heads/alias") == target


def test_atomic_helper_never_dereferences_a_symbolic_branch(repo):
    """Defence in depth below `validate_local_branch`: the transaction itself
    must carry `option no-deref`, so even if a symbolic ref reached it the
    ref that moves is the named one, never the one it points at."""
    a = commit(repo, "f.txt", "1", "one")
    b = commit(repo, "f.txt", "1\n2", "two")
    run_git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/main")

    gitops._apply_rewrite_atomic(
        str(repo),
        "alias",
        old_tip=b,
        new_tip=a,
        backup_ref=f"refs/heads/{gitops.BACKUP_NAMESPACE}/alias-1-{b[:9]}",
    )

    assert run_git(repo, "rev-parse", "refs/heads/main") == b  # target untouched
    assert run_git(repo, "rev-parse", "refs/heads/alias") == a  # alias itself moved
    # `alias` is now a plain ref, no longer symbolic.
    proc = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "refs/heads/alias"],
        capture_output=True,
        check=False,
    )
    assert proc.returncode != 0


@pytest.mark.parametrize(
    "bad", ["main..feature", "main^", "main~2", "HEAD@{0}", "nonexistent"]
)
def test_validate_base_rejects_revision_syntax(repo, bad):
    commit(repo, "f.txt", "1", "c")

    with pytest.raises(GitError):
        gitops.validate_base(str(repo), bad)


def test_validate_base_accepts_none_blank_or_branch(repo):
    commit(repo, "f.txt", "1", "c")

    assert gitops.validate_base(str(repo), None) is None
    assert gitops.validate_base(str(repo), "  ") is None
    assert gitops.validate_base(str(repo), "main") == "main"


def test_signed_and_pushed_flags_default_false(repo):
    commit(repo, "f.txt", "1", "c")

    c = gitops.get_commits(str(repo), "main", limit=10)[0]

    # No GPG signature and no remote -> both flags false, no crash.
    assert c.signed is False
    assert c.pushed is False


@pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen is needed to sign a commit"
)
def test_ssh_signed_commit_is_marked_signed_without_verification_setup(repo, tmp_path):
    """A signature is detected from the commit header, not from verification:
    with no allowed-signers file `%G?` reports N for a genuinely signed commit."""
    key = tmp_path / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    (repo / "f.txt").write_text("1")
    run_git(repo, "add", "f.txt")
    run_git(
        repo,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={key}",
        "commit",
        "-q",
        "-S",
        "-m",
        "signed commit",
    )

    c = gitops.get_commits(str(repo), "main", limit=5)[0]

    assert c.signed is True
    assert c.message == "signed commit"


@pytest.mark.parametrize("header", ["gpgsig", "gpgsig-sha256"])
def test_commit_with_signature_header_is_marked_signed(repo, header):
    """Deterministic companion to the SSH test: any signature header counts,
    whatever git makes of the signature itself."""
    commit(repo, "f.txt", "1", "unsigned parent")
    tree = run_git(repo, "rev-parse", "HEAD^{tree}")
    raw = (
        f"tree {tree}\n"
        "author Ann <ann@example.com> 1700000000 +0000\n"
        "committer Ann <ann@example.com> 1700000000 +0000\n"
        f"{header} -----BEGIN PGP SIGNATURE-----\n not-a-real-signature\n"
        " -----END PGP SIGNATURE-----\n"
        "\n"
        "hand-signed commit\n"
    )
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=raw,
        text=True,
        capture_output=True,
        check=True,
    )
    run_git(repo, "update-ref", "refs/heads/main", proc.stdout.strip())

    c = gitops.get_commits(str(repo), "main", limit=5)[0]

    assert c.signed is True
    assert c.message == "hand-signed commit"


# --------------------------------------------------------------------------- #
# commit_stat / commit_diff                                                    #
# --------------------------------------------------------------------------- #


def test_commit_stat_reports_real_statuses(repo):
    """A/D/R must be reported as such, not all collapsed to 'M' (which is what
    an unchecked, empty `git show --no-patch --name-status` would yield)."""
    (repo / "old.txt").write_text("a\nb\nc\nd\ne\n")
    (repo / "gone.txt").write_text("x\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "base")
    run_git(repo, "mv", "old.txt", "new.txt")
    (repo / "new.txt").write_text("a\nb\nc\nd\nE\n")
    run_git(repo, "rm", "-q", "gone.txt")
    (repo / "added.txt").write_text("new\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "rename, edit, delete, add")

    stat = gitops.commit_stat(str(repo), run_git(repo, "rev-parse", "HEAD"))

    by_path = {f["path"]: f for f in stat["files"]}
    assert by_path["added.txt"]["status"] == "A", by_path
    assert by_path["gone.txt"]["status"] == "D", by_path
    assert by_path["new.txt"]["status"].startswith("R"), by_path
    assert by_path["new.txt"]["old_path"] == "old.txt", by_path
    assert stat["vs_first_parent"] is False


def test_commit_stat_root_commit_is_not_empty(repo):
    """`diff-tree <root>` prints nothing; --root is required."""
    commit(repo, "f.txt", "1\n2\n", "root commit")

    root = run_git(repo, "rev-list", "--max-parents=0", "HEAD")
    stat = gitops.commit_stat(str(repo), root)

    assert stat["file_count"] == 1, stat
    assert stat["files"][0]["status"] == "A", stat


def test_commit_stat_merge_uses_first_parent(repo):
    """`diff-tree <merge>` prints nothing; compare against ^1 instead."""
    commit(repo, "base.txt", "b", "base")
    run_git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "side.txt", "s", "side work")
    run_git(repo, "checkout", "-q", "main")
    commit(repo, "main.txt", "m", "main work")
    run_git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")

    stat = gitops.commit_stat(str(repo), run_git(repo, "rev-parse", "HEAD"))

    assert stat["vs_first_parent"] is True, stat
    assert [f["path"] for f in stat["files"]] == ["side.txt"], stat


def test_commit_stat_tab_in_filename_not_truncated(repo):
    """A tab inside a filename must not truncate the path (split('\\t', 2))."""
    commit(repo, "seed.txt", "s", "seed")
    weird = "has\ttab.txt"
    (repo / weird).write_text("x\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "tabbed filename")

    stat = gitops.commit_stat(str(repo), run_git(repo, "rev-parse", "HEAD"))

    assert [f["path"] for f in stat["files"]] == [weird], stat


@pytest.mark.parametrize(
    "raw",
    [b"R100\0old.txt\0", b"R100\0old.txt", b"C075\0", b"M\0", b"M\0\0x.txt\0"],
    ids=["rename_no_new", "rename_no_new_no_nul", "copy_bare", "bare", "empty_path"],
)
def test_name_status_parser_refuses_a_truncated_record(raw):
    """A rename/copy carries two paths; a short record must not be recorded
    against '' or against its *old* path, which would then mismatch numstat."""
    with pytest.raises(GitError, match="(?i)malformed"):
        gitops._parse_name_status_z(raw)


@pytest.mark.parametrize(
    "raw",
    [b"1\t2\t\0old.txt\0", b"1\t2\t\0", b"1\t2\0", b"1\t2\t\0\0new.txt\0"],
    ids=["rename_no_new", "rename_no_paths", "two_fields", "rename_empty_old"],
)
def test_numstat_parser_refuses_a_truncated_record(raw):
    with pytest.raises(GitError, match="(?i)malformed"):
        gitops._parse_numstat_z(raw)


def test_parsers_accept_well_formed_rename_and_plain_records():
    assert gitops._parse_name_status_z(b"R100\0f\0g\0M\0h\0") == {"g": "R100", "h": "M"}
    assert gitops._parse_numstat_z(b"0\t0\t\x00f\x00g\x001\t1\th\x00") == [
        ("0", "0", "g", "f"),
        ("1", "1", "h", None),
    ]
    assert gitops._parse_name_status_z(b"") == {}
    assert gitops._parse_numstat_z(b"") == []


def test_commit_diff_returns_patch(repo):
    commit(repo, "f.txt", "one\n", "first")
    c = commit(repo, "f.txt", "one\ntwo\n", "second")

    d = gitops.commit_diff(str(repo), c)

    assert "+two" in d["diff"], d["diff"]
    assert d["truncated"] is False


def test_commit_diff_keeps_crlf_line_endings_in_patch_content(repo):
    """Text-mode subprocess output performs universal-newline translation,
    which would silently turn a CRLF file's `+line\\r\\n` into `+line\\n`."""
    commit(repo, "f.txt", "seed\n", "seed")
    (repo / "dos.txt").write_bytes(b"one\r\ntwo\r\n")
    # A global core.autocrlf=input would normalise the CRLF away at add time.
    run_git(repo, "-c", "core.autocrlf=false", "add", "dos.txt")
    run_git(repo, "commit", "-q", "-m", "crlf file")
    assert (
        b"\r\n"
        in subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-p", "HEAD:dos.txt"],
            capture_output=True,
            check=False,
        ).stdout
    ), "fixture: the blob itself must be CRLF"

    d = gitops.commit_diff(str(repo), run_git(repo, "rev-parse", "HEAD"))

    assert "+one\r\n+two\r\n" in d["diff"], repr(d["diff"])


def test_truncated_diff_is_within_max_bytes_and_ends_on_a_line_boundary(repo):
    commit(repo, "f.txt", "", "empty")
    c = commit(repo, "f.txt", ("é" * 20 + "\n") * 20, "twenty lines of é")

    small = gitops.commit_diff(str(repo), c, max_bytes=300)

    assert small["truncated"] is True
    assert len(small["diff"].encode("utf-8")) <= 300
    assert small["diff"].endswith("é" * 20)  # cut between lines, not mid-line


# --------------------------------------------------------------------------- #
# Refusals                                                                     #
# --------------------------------------------------------------------------- #


def test_shallow_repository_refused(tmp_path):
    """At a shallow boundary git reports no parent, which would silently turn
    that commit into a root and orphan the history beneath it."""
    (tmp_path / "origin").mkdir()
    origin = new_repo(tmp_path / "origin")
    for i in range(4):
        commit(origin, "f.txt", "\n".join(str(n) for n in range(i + 1)), f"c{i}")
    shallow = tmp_path / "shallow"

    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
        capture_output=True,
        check=True,
    )

    with pytest.raises(GitError, match="(?i)shallow"):
        gitops.check_repo_writable(str(shallow))


def test_unmerged_index_refused(repo):
    c = commit(repo, "f.txt", "1", "one")
    blob = run_git(repo, "rev-parse", f"{c}:f.txt")
    proc = subprocess.run(
        ["git", "-C", str(repo), "update-index", "--index-info"],
        input=f"100644 {blob} 1\tf.txt\n100644 {blob} 2\tf.txt\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    with pytest.raises(GitError, match="(?i)unmerged"):
        gitops.check_repo_writable(str(repo))


def test_replace_refs_refused(repo):
    a = commit(repo, "f.txt", "1", "one")
    b = commit(repo, "f.txt", "1\n2", "two")
    run_git(repo, "replace", a, b)

    with pytest.raises(GitError, match="(?i)replacement"):
        gitops.check_repo_writable(str(repo))


def test_grafts_file_is_refused(repo):
    """Grafts rewrite parentage behind git's back and --no-replace-objects
    does not disable them."""
    c = commit(repo, "f.txt", "1", "one")
    git_dir = run_git(repo, "rev-parse", "--absolute-git-dir")
    (Path(git_dir) / "info" / "grafts").write_text(f"{c}\n")

    with pytest.raises(GitError, match="grafts"):
        gitops.rewrite_messages(str(repo), "main", {c: "new"}, limit=10)

    assert run_git(repo, "rev-parse", "main") == c
    assert backup_refs(repo) == []


def test_oversized_base_range_refused(repo, monkeypatch):
    """Refuse, never silently clamp: a save rewrites the WHOLE range, so showing
    a truncated view would mean consenting to one thing and getting another."""
    commit(repo, "f.txt", "0", "base")
    run_git(repo, "branch", "start")
    monkeypatch.setattr(gitops, "MAX_COMMITS", 3)
    for i in range(5):
        commit(repo, "f.txt", f"line {i}\n" * (i + 1), f"c{i}")

    with pytest.raises(GitError, match="more than 3") as exc_info:
        gitops.get_commits(str(repo), "main", base="start")

    # The refusal must say which side is which, and offer the swap when the
    # reverse range is the small one.
    assert "not on 'start'" in str(exc_info.value)
    assert "Did you mean main..start" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Backups and identity                                                         #
# --------------------------------------------------------------------------- #


def test_backups_are_real_branches_and_listed(repo):
    commit(repo, "f.txt", "1", "one")
    c = commit(repo, "f.txt", "1\n2", "two")

    res = gitops.rewrite_messages(str(repo), "main", {c: "two (reworded)"}, limit=10)

    assert res.backup_ref.startswith(f"refs/heads/{gitops.BACKUP_NAMESPACE}/")
    # Visible to plain `git branch` — a recovery point nobody can find is not a
    # recovery point.
    assert gitops.BACKUP_NAMESPACE in run_git(repo, "branch")
    backups = gitops.list_backups(str(repo))
    assert len(backups) == 1 and backups[0]["ref"] == res.backup_ref, backups
    assert backups[0]["branch"] == "main", backups
    # ...but kept out of the editable branch list.
    assert gitops.list_branches(str(repo))["branches"] == ["main"]

    gitops.delete_backup(str(repo), res.backup_ref)

    assert gitops.list_backups(str(repo)) == []


def test_undo_and_resave_within_one_second_gets_a_distinct_backup(repo, monkeypatch):
    monkeypatch.setattr(gitops.time, "time", lambda: 1_700_000_000)
    commit(repo, "f.txt", "1", "one")
    c = commit(repo, "f.txt", "1\n2", "two")
    first = gitops.rewrite_messages(str(repo), "main", {c: "two (reworded)"}, limit=10)
    run_git(repo, "update-ref", "refs/heads/main", c)  # the user undoes the save

    second = gitops.rewrite_messages(
        str(repo), "main", {c: "two (reworded again)"}, limit=10
    )

    assert second.backup_ref == first.backup_ref + "-2"
    assert sorted(backup_refs(repo)) == sorted([first.backup_ref, second.backup_ref])
    assert [b["branch"] for b in gitops.list_backups(str(repo))] == ["main", "main"]


def test_backups_are_dated_and_ordered_by_the_time_in_their_name(repo):
    """`%(creatordate)` would be the *commit's* date, identical for two backups
    of the same tip; the time a backup was taken is only in its name."""
    tip = commit(repo, "f.txt", "1", "one")
    older = f"refs/heads/{gitops.BACKUP_NAMESPACE}/main-1700000000-{tip[:9]}"
    newer = f"refs/heads/{gitops.BACKUP_NAMESPACE}/main-1800000000-{tip[:9]}"
    resave = f"refs/heads/{gitops.BACKUP_NAMESPACE}/main-1800000000-{tip[:9]}-2"
    stray = f"refs/heads/{gitops.BACKUP_NAMESPACE}/not-a-backup-name"
    for ref in (newer, older, stray, resave):
        run_git(repo, "update-ref", ref, tip)

    backups = gitops.list_backups(str(repo))

    assert [b["ref"] for b in backups] == [resave, newer, older]
    assert [b["created"] for b in backups] == [1800000000, 1800000000, 1700000000]
    assert all(b["branch"] == "main" and b["sha"] == tip for b in backups)


@pytest.mark.parametrize(
    "bad", ["refs/heads/main", "refs/heads/commit-editor-backup/../main", "main"]
)
def test_delete_backup_refuses_other_refs(repo, bad):
    commit(repo, "f.txt", "1", "one")

    with pytest.raises(GitError):
        gitops.delete_backup(str(repo), bad)

    assert run_git(repo, "rev-parse", "--verify", "main")  # still there


def test_delete_backup_refuses_a_checked_out_backup(repo):
    commit(repo, "f.txt", "1", "one")
    c = commit(repo, "f.txt", "1\n2", "two")
    res = gitops.rewrite_messages(str(repo), "main", {c: "two (reworded)"}, limit=10)
    run_git(repo, "checkout", "-q", res.backup_ref[len("refs/heads/") :])

    with pytest.raises(GitError):
        gitops.delete_backup(str(repo), res.backup_ref)

    assert run_git(repo, "rev-parse", "HEAD") == c
    assert backup_refs(repo) == [res.backup_ref]


def test_delete_backup_of_a_symbolic_ref_does_not_delete_its_target(repo):
    tip = commit(repo, "f.txt", "1", "one")
    symref = f"refs/heads/{gitops.BACKUP_NAMESPACE}/x-1-abcdef012"
    run_git(repo, "symbolic-ref", symref, "refs/heads/main")

    gitops.delete_backup(str(repo), symref)

    assert run_git(repo, "rev-parse", "refs/heads/main") == tip
    assert backup_refs(repo) == []


def test_repo_identity_shared_across_worktrees(tmp_path):
    (tmp_path / "main").mkdir()
    main_repo = new_repo(tmp_path / "main")
    commit(main_repo, "f.txt", "1", "one")
    linked = tmp_path / "linked"

    run_git(main_repo, "worktree", "add", "-q", "-b", "wt", str(linked))

    assert gitops.repo_identity(str(main_repo)) == gitops.repo_identity(str(linked))


def test_git_routing_env_is_sanitised(tmp_path, monkeypatch):
    """A stray GIT_DIR in the environment must not redirect our commands."""
    (tmp_path / "real").mkdir()
    real = new_repo(tmp_path / "real")
    commit(real, "f.txt", "1", "the real repo")
    (tmp_path / "other").mkdir()
    other = new_repo(tmp_path / "other")
    commit(other, "g.txt", "1", "the WRONG repo")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    subjects_seen = [c.subject for c in gitops.get_commits(str(real), "main", limit=5)]

    assert subjects_seen == ["the real repo"], subjects_seen


def test_pushed_reflects_this_branchs_upstream(tmp_path):
    """`pushed` is the force-push warning, so it must come from a real commit
    set for this branch's upstream, not from a string the ref name could
    overwrite."""
    (tmp_path / "up").mkdir()
    run_git(tmp_path / "up", "init", "-q", "--bare", "-b", "main")
    (tmp_path / "wk").mkdir()
    wk = new_repo(tmp_path / "wk")
    commit(wk, "f.txt", "1", "pushed commit")
    run_git(wk, "remote", "add", "origin", str(tmp_path / "up"))
    run_git(wk, "push", "-q", "-u", "origin", "main")
    commit(wk, "f.txt", "1\n2", "local only")

    commits = {c.subject: c for c in gitops.get_commits(str(wk), "main", limit=10)}

    assert commits["pushed commit"].pushed is True, "upstream commit not flagged"
    assert commits["local only"].pushed is False, "local commit wrongly flagged"
    assert commits["pushed commit"].publication_known is True


def test_on_remote_is_distinct_from_pushed(repo, tmp_path):
    """Reachable from some other remote-tracking ref means "shared somewhere",
    not "on this branch's upstream" — only the latter implies a force-push."""
    a = commit(repo, "f.txt", "1", "on upstream")
    b = commit(repo, "f.txt", "1\n2", "only on other remote")
    run_git(repo, "remote", "add", "origin", str(tmp_path / "never-fetched"))
    run_git(repo, "config", "branch.main.remote", "origin")
    run_git(repo, "config", "branch.main.merge", "refs/heads/main")
    run_git(repo, "update-ref", "refs/remotes/origin/main", a)
    run_git(repo, "update-ref", "refs/remotes/other/x", b)

    commits = {c.subject: c for c in gitops.get_commits(str(repo), "main", limit=10)}

    assert commits["only on other remote"].on_remote is True
    assert commits["only on other remote"].pushed is False
    assert commits["on upstream"].on_remote is True
    assert commits["on upstream"].pushed is True


def test_backup_restore_target_survives_slashes(repo):
    """A backup for `feature/fix` must not claim to restore `feature_fix`."""
    commit(repo, "f.txt", "1", "one")
    run_git(repo, "checkout", "-q", "-b", "feature/fix")
    c = commit(repo, "f.txt", "1\n2", "wip")
    run_git(repo, "branch", "feature_fix")  # decoy: what "/" -> "_" would hit
    decoy_before = run_git(repo, "rev-parse", "feature_fix")

    gitops.rewrite_messages(str(repo), "feature/fix", {c: "proper message"}, limit=5)

    backup = gitops.list_backups(str(repo))[0]
    assert backup["branch"] == "feature/fix", backup
    # The decoy branch must be untouched.
    assert run_git(repo, "rev-parse", "feature_fix") == decoy_before


def test_get_git_root_ignores_inherited_routing_env(tmp_path, monkeypatch):
    """get_git_root runs BEFORE every other call, so if it honoured a stray
    GIT_WORK_TREE the later sanitised calls would target the wrong repo."""
    (tmp_path / "real").mkdir()
    real = new_repo(tmp_path / "real")
    commit(real, "f.txt", "1", "real")
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))

    root = gitops.get_git_root(str(real))

    assert root and os.path.realpath(root) == os.path.realpath(str(real)), root


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
