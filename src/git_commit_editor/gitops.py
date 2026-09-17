"""
Git operations for the Commit Message Editor.

The interesting part is :func:`rewrite_messages`. Editing a commit message
changes that commit's hash, which means every descendant commit (whose parent
hash therefore changes) must be replayed too. We do this without touching the
working tree by rebuilding commits with ``git commit-tree`` from oldest to
newest, remapping each commit's parents through an old->new SHA map. Because we
only change messages, every rewritten commit keeps the *same tree*, so the
working tree of a checked-out branch stays consistent after the ref moves.

This is deliberately **reword-only**: it never reorders, squashes, splits, or
drops commits. That restriction is what guarantees identical trees and a clean
``git status`` after a save.

All operations are confined to the given repository and shell out to ``git``
with argument lists (never ``shell=True``).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict, overload

# Field separator for `for-each-ref --format` output: 0x1f (unit separator)
# cannot appear in a ref name or object id, so a split on it is unambiguous.
# (`git log` output is parsed differently — see get_commits.)
_FS = "\x1f"

# Limits (mirrored by the frontend where relevant).
MAX_COMMITS = 1000
DEFAULT_LIMIT = 30


def parse_limit(raw: Any) -> int:
    """Parse a client-supplied commit limit and clamp it to ``[1, MAX_COMMITS]``.

    An absent or unparseable value means "not given" and falls back to
    ``DEFAULT_LIMIT``; a number outside the bounds is clamped, so ``0`` reads
    as 1 rather than as "not given". ``OverflowError`` is the JSON literal
    ``1e999``, which decodes to ``inf`` and has no integer to clamp.
    """
    try:
        limit = int(raw)
    except TypeError, ValueError, OverflowError:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_COMMITS))


# Backups live under refs/heads/ (i.e. they are real branches) rather than a
# private ref namespace, so `git branch`, tab-completion and every git GUI show
# them. A recovery point nobody can find is not a recovery point. They are
# filtered back out of this tool's own branch list.
BACKUP_NAMESPACE = "commit-editor-backup"
MAX_MSG_LEN = 65536

# A full-length object id: 40 (sha-1) or 64 (sha-256) hex chars. The client
# only ever echoes back ids this tool produced, and the engine compares them by
# full-string equality, so an abbreviation is a malformed request.
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class GitError(Exception):
    """A git command failed or the repository is in an unexpected state."""


@overload
def run_git(
    repo: str,
    args: list[str],
    *,
    check: bool = ...,
    stdin: str | None = ...,
    env: dict[str, str] | None = ...,
    binary: Literal[False] = ...,
) -> subprocess.CompletedProcess[str]: ...
@overload
def run_git(
    repo: str,
    args: list[str],
    *,
    check: bool = ...,
    stdin: str | None = ...,
    env: dict[str, str] | None = ...,
    binary: Literal[True],
) -> subprocess.CompletedProcess[bytes]: ...
def run_git(
    repo: str,
    args: list[str],
    *,
    check: bool = True,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    binary: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Run ``git -C <repo> <args>`` capturing text output.

    Raises :class:`GitError` if ``check`` is true and git exits non-zero, or if
    the ``git`` executable cannot be found. A predictable environment is pinned
    so no editor/pager/credential prompt can block a captured call.

    ``errors="surrogateescape"`` keeps undecodable bytes from raising here, but
    it is *not* a lossless round-trip: ``commit-tree`` re-interprets invalid
    UTF-8 on its stdin (see ``_commit_tree``), so messages are decoded and
    re-encoded explicitly rather than relying on this.

    ``binary=True`` returns raw bytes and skips text mode entirely. Text mode
    performs **universal newline translation**, which rewrites a ``\r`` inside a
    *filename* to ``\n`` — two distinct files then collide. Anything parsing
    NUL-delimited paths must use ``binary=True``.

    Repository-routing variables inherited from the environment are **removed**,
    not merely overridden: ``-C <repo>`` does not neutralise ``GIT_DIR`` and
    friends, so a caller's stray ``GIT_DIR`` would silently redirect every one
    of these commands at a different repository.
    """
    routing = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_GRAFT_FILE",
    )
    pinned = {"GIT_PAGER": "cat", "GIT_EDITOR": ":", "GIT_TERMINAL_PROMPT": "0"}
    full_env = {k: v for k, v in os.environ.items() if k not in routing}
    full_env.update(pinned)
    full_env.update(env or {})
    result: subprocess.CompletedProcess[Any]
    try:
        # --no-replace-objects: refs/replace/* can substitute a different
        # commit *and tree*, which would defeat the whole "only the message
        # changes" guarantee. Never rewrite through a replacement.
        if binary:
            result = subprocess.run(
                ["git", "--no-replace-objects", "-C", repo, *args],
                capture_output=True,
                input=(
                    stdin.encode("utf-8", "surrogateescape")
                    if stdin is not None
                    else None
                ),
                env=full_env,
                check=False,
            )
        else:
            result = subprocess.run(
                ["git", "--no-replace-objects", "-C", repo, *args],
                capture_output=True,
                input=stdin,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                env=full_env,
                check=False,
            )
    except FileNotFoundError:
        raise GitError("git executable not found on PATH.")
    if check and result.returncode != 0:
        err = result.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        message = (err or "").strip()
        raise GitError(message or f"git {' '.join(args)} failed")
    return result


def repo_identity(repo: str) -> str:
    """A stable opaque id for the repository, for scoping client-side drafts.

    Derived from the **common** git directory, not the working-tree root, so
    that all linked worktrees of one repository share an identity and so that
    the id does not change with how the user spelled the path. Hashed because it
    ends up in browser storage keys and there is no reason to put an absolute
    filesystem path there.
    """
    common = run_git(
        repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    ).stdout.strip()
    return hashlib.sha256(os.path.realpath(common).encode("utf-8")).hexdigest()[:32]


def get_git_root(path: str) -> str | None:
    """Return the absolute toplevel of the git repo containing ``path``.

    Goes through :func:`run_git` rather than calling subprocess directly: every
    API resolves its repository here *first*, so if this call inherited
    ``GIT_WORK_TREE``/``GIT_DIR`` it would hand every later (sanitised) call a
    root belonging to a different repository — sanitising only the later calls
    would be worthless.
    """
    if not path:
        return None
    p = Path(path).expanduser()
    cwd = str(p if p.is_dir() else p.parent)
    if not os.path.isdir(cwd):
        return None
    try:
        result = run_git(cwd, ["rev-parse", "--show-toplevel"], check=False)
    except GitError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# --------------------------------------------------------------------------- #
# Input validation (security-relevant: these values flow into git arguments)  #
# --------------------------------------------------------------------------- #


@overload
def safe_ref(name: str | None, *, label: str, required: Literal[True]) -> str: ...
@overload
def safe_ref(
    name: str | None, *, label: str, required: Literal[False] = ...
) -> str | None: ...
def safe_ref(name: str | None, *, label: str, required: bool = False) -> str | None:
    """Reject ref names that could be mistaken for git options or are malformed.

    Returns the trimmed name, or ``None`` when empty and not ``required``. A
    leading ``-`` could be parsed as an option; whitespace/control characters
    never appear in valid ref names.
    """
    if not name or not name.strip():
        if required:
            raise GitError(f"{label} is required.")
        return None
    name = name.strip()
    if name.startswith("-"):
        raise GitError(f"{label} must not start with '-'.")
    if any(c.isspace() or ord(c) < 0x20 for c in name):
        raise GitError(f"{label} contains invalid whitespace or control characters.")
    return name


def validate_sha(sha: object, label: str) -> str:
    """Ensure a client-supplied object id is a full hex id before it reaches git."""
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise GitError(f"Invalid {label}: {sha!r}")
    return sha


def ref_exists(repo: str, ref: str) -> bool:
    """True if ``ref`` resolves to a commit in the repo."""
    return (
        run_git(
            repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False
        ).returncode
        == 0
    )


def branch_tip(repo: str, branch: str) -> str:
    """The commit a local branch points at (raises if it does not exist)."""
    return run_git(
        repo, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"]
    ).stdout.strip()


def validate_local_branch(repo: str, branch: str | None) -> str:
    """Validate that ``branch`` is a well-formed, existing local branch.

    The value flows into ``refs/heads/{branch}`` for the ref move, so a crafted
    value (e.g. ``../../refs/remotes/origin/main``) must be rejected.
    """
    branch = safe_ref(branch, label="branch", required=True)
    fmt_ok = (
        run_git(repo, ["check-ref-format", "--branch", branch], check=False).returncode
        == 0
    )
    if not fmt_ok:
        raise GitError(f"Invalid branch name: {branch!r}")
    exists = (
        run_git(
            repo,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            check=False,
        ).returncode
        == 0
    )
    if not exists:
        raise GitError(f"Local branch does not exist: {branch}")
    # show-ref and rev-parse both resolve *through* a symbolic ref, so up to
    # here an alias for main (or for a remote-tracking ref) looks like an
    # ordinary branch — and a rewrite of it would move its target instead.
    symbolic = (
        run_git(
            repo, ["symbolic-ref", "--quiet", f"refs/heads/{branch}"], check=False
        ).returncode
        == 0
    )
    if symbolic:
        raise GitError(
            f"Branch '{branch}' is a symbolic ref (an alias for another ref); "
            "rewrite the branch it points at instead."
        )
    return branch


def validate_base(repo: str, base: str | None) -> str | None:
    """Validate an optional base ref: a plain ref/commit-ish that resolves.

    A base may be a local branch, remote-tracking ref, tag, or commit — but not
    a revision/range expression, which would corrupt the ``base..branch`` range.
    """
    base = safe_ref(base, label="base ref")
    if base is None:
        return None
    if any(tok in base for tok in ("..", "^", "~", ":", "@{")):
        raise GitError(
            f"Base ref must be a plain ref, not a revision expression: {base!r}"
        )
    if not ref_exists(repo, base):
        raise GitError(f"Base ref does not resolve to a commit: {base}")
    return base


def detect_default_branch(repo: str) -> str | None:
    """Best-effort guess at the repo's main/integration branch.

    Used as the default base so we show only the commits unique to the selected
    branch (``base..branch``) rather than every commit it shares with main.
    Order: the remote's default branch, then common local names, then
    ``init.defaultBranch``, then remote-tracking equivalents.
    """
    candidates: list[str] = []
    head = run_git(
        repo, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False
    ).stdout.strip()
    if head:  # e.g. refs/remotes/origin/main -> main
        candidates.append(head.rsplit("/", 1)[-1])
    configured = run_git(
        repo, ["config", "--get", "init.defaultBranch"], check=False
    ).stdout.strip()
    candidates += ["main", "master", "develop", "trunk"]
    if configured:
        candidates.append(configured)
    candidates = list(dict.fromkeys(candidates))  # de-dup, preserve order

    local = set(list_branches(repo)["branches"])
    for c in candidates:
        if c in local:
            return c
    for c in candidates:
        if ref_exists(repo, f"origin/{c}"):
            return f"origin/{c}"
    return None


# --------------------------------------------------------------------------- #
# Branch / commit listing                                                     #
# --------------------------------------------------------------------------- #


def _current_branch(repo: str) -> str | None:
    """The checked-out local branch, or ``None`` for a detached HEAD.

    The full ref with ``refs/heads/`` stripped, not ``--short``: "short" is an
    *unambiguous* abbreviation, so once a tag called main exists it reports
    the branch as ``heads/main`` and nothing matches the name any more.
    """
    ref = run_git(repo, ["symbolic-ref", "--quiet", "HEAD"], check=False).stdout.strip()
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else None


def list_branches(repo: str) -> dict[str, Any]:
    """Return local branches and the currently checked-out branch (if any).

    Backup branches this tool created are filtered out: they are recovery
    points, not things you would want to edit, and they would otherwise swamp
    the branch dropdown after a few saves.
    """
    # %(refname) with the prefix stripped, not %(refname:short), for the same
    # reason as _current_branch.
    out = run_git(repo, ["for-each-ref", "--format=%(refname)", "refs/heads/"]).stdout
    branches = [
        line[len("refs/heads/") :]
        for line in out.splitlines()
        if line and not line.startswith(f"refs/heads/{BACKUP_NAMESPACE}/")
    ]
    return {"branches": branches, "current": _current_branch(repo)}


# Backup name shape: <namespace>/<branch>-<unixtime>-<oldtip9>[-<n>], where
# <branch> keeps its slashes and <n> disambiguates a resave in the same second.
# The two functions below are the only places that know this shape.
_BACKUP_NAME_RE = re.compile(
    r"^(?P<branch>.+)-(?P<time>\d+)-[0-9a-f]{9}(?:-(?P<n>\d+))?$"
)


def _backup_ref(branch: str, old_tip: str, unixtime: int) -> str:
    return f"refs/heads/{BACKUP_NAMESPACE}/{branch}-{unixtime}-{old_tip[:9]}"


def _parse_backup_name(short_name: str) -> re.Match[str] | None:
    """Match a backup branch's short name against the shape above.

    ``None`` for a ref under the namespace that this tool did not name: it
    carries no branch to restore and no time it was taken, so it is not a
    backup this tool can describe.
    """
    return _BACKUP_NAME_RE.match(short_name[len(BACKUP_NAMESPACE) + 1 :])


def list_backups(repo: str) -> list[dict[str, Any]]:
    """Backup branches created by this tool, newest first.

    ``created`` is the unix time in the backup's *name*. ``%(creatordate)``
    would be the date of the commit it points at — the same for every backup
    of one tip, and nothing to do with when the backup was taken.
    """
    out = run_git(
        repo,
        [
            "for-each-ref",
            f"--format=%(refname){_FS}%(objectname)",
            f"refs/heads/{BACKUP_NAMESPACE}/",
        ],
        check=False,
    ).stdout
    keyed: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for line in out.splitlines():
        ref, _, sha = line.partition(_FS)
        short_name = ref[len("refs/heads/") :]
        match = _parse_backup_name(short_name)
        if match is None:
            continue
        created = int(match.group("time"))
        # Same-second resaves order by their -<n> suffix (absent = 1).
        order = (created, int(match.group("n") or 1))
        keyed.append(
            (
                order,
                {
                    "ref": ref,
                    "name": short_name,
                    "branch": match.group("branch"),
                    "sha": sha,
                    "short": sha[:9],
                    "created": created,
                },
            )
        )
    keyed.sort(key=lambda item: item[0], reverse=True)
    return [backup for _, backup in keyed]


def delete_backup(repo: str, ref: str) -> None:
    """Delete one backup branch. Refuses anything outside the backup namespace.

    ``git branch -D`` rather than ``update-ref -d``: it refuses a branch that is
    checked out in any worktree (deleting it would leave that HEAD dangling)
    and deletes a symbolic ref itself rather than the branch it points at.
    """
    prefix = f"refs/heads/{BACKUP_NAMESPACE}/"
    if not ref.startswith(prefix) or ".." in ref or ref.endswith("/"):
        raise GitError("Refusing to delete a ref outside the backup namespace.")
    short_name = safe_ref(ref[len("refs/heads/") :], label="backup ref", required=True)
    run_git(repo, ["branch", "-D", "--", short_name])


def _subject(message: str) -> str:
    """The first line of a message; the empty string for an empty message."""
    return message.splitlines()[0] if message else ""


@dataclass
class Commit:
    sha: str
    tree: str
    parents: list[str]
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str
    message: str  # raw full message (subject + body), trailing newline stripped
    signed: bool = False
    pushed: bool = False  # reachable from THIS branch's upstream
    on_remote: bool = False  # reachable from some other remote-tracking ref
    publication_known: bool = False  # False => no upstream and no remotes to check

    @property
    def short(self) -> str:
        return self.sha[:9]

    @property
    def subject(self) -> str:
        return _subject(self.message)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def is_root(self) -> bool:
        return len(self.parents) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "short": self.short,
            "tree": self.tree,
            "parents": self.parents,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "author_date": self.author_date,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "committer_date": self.committer_date,
            "subject": self.subject,
            "message": self.message,
            "is_merge": self.is_merge,
            "is_root": self.is_root,
            "signed": self.signed,
            "pushed": self.pushed,
            "on_remote": self.on_remote,
            "publication_known": self.publication_known,
        }


def check_range_size(repo: str, branch: str, base: str | None) -> None:
    """Refuse a ``base..branch`` range larger than ``MAX_COMMITS``.

    Deliberately a refusal, not a clamp. Saving rewrites everything in the
    range, so quietly showing the newest 1,000 of 5,000 commits would mean the
    user consents to one thing and gets another. The "latest N" mode is a
    bounded window by definition and needs no such check.
    """
    if not base:
        return
    out = run_git(
        repo,
        [
            "rev-list",
            "--count",
            f"--max-count={MAX_COMMITS + 1}",
            f"{base}..refs/heads/{branch}",
            "--",
        ],
    ).stdout.strip()
    if int(out) <= MAX_COMMITS:
        return

    # Overwhelmingly the most common cause is the two refs being the wrong way
    # round — asking for "what's on main that isn't on my branch" instead of
    # "what's on my branch". Check the reverse and say so outright, because
    # "choose a closer base" is useless advice when the base is already right
    # and it is the *branch* that is wrong.
    reverse = run_git(
        repo,
        [
            "rev-list",
            "--count",
            f"--max-count={MAX_COMMITS + 1}",
            f"refs/heads/{branch}..{base}",
            "--",
        ],
        check=False,
    ).stdout.strip()
    hint = (
        f" Did you mean {branch}..{base}? That is {reverse} commit"
        f"{'' if reverse == '1' else 's'} — set Branch to '{base}' and Base to "
        f"'{branch}'."
        if reverse.isdigit() and int(reverse) <= MAX_COMMITS
        else " Choose a base closer to the branch tip, or clear Base to use the "
        "most recent N commits instead."
    )
    raise GitError(
        f"The range {base}..{branch} contains more than {MAX_COMMITS} commits "
        f"— these are the commits on '{branch}' that are not on '{base}'.{hint}"
    )


def branch_upstream(repo: str, branch: str) -> dict[str, Any]:
    """The configured upstream and push destination for ``branch``.

    Uses ``for-each-ref`` on the *named* branch rather than ``@{upstream}``,
    which always refers to the checked-out branch — not necessarily the one
    being edited.
    """
    out = run_git(
        repo,
        [
            "for-each-ref",
            f"--format=%(upstream){_FS}%(push)",
            f"refs/heads/{branch}",
        ],
        check=False,
    ).stdout.strip()
    upstream, _, push = out.partition(_FS)
    return {"upstream": upstream or None, "push": push or None}


class _Publication(TypedDict):
    # The *un*published sets (None when the reference does not exist) and the
    # refs they were computed against, so the caller can say how it knows.
    not_on_upstream: set[str] | None
    not_on_any_remote: set[str] | None
    upstream_ref: str | None
    push_ref: str | None


def _unpublished_shas(repo: str, branch: str) -> _Publication:
    """Which commits on ``branch`` are already published, and how we know.

    Two distinct facts, deliberately not conflated:

    * ``upstream`` — reachable from this branch's *own* upstream. This is what
      actually means "rewriting needs a force-push".
    * ``remotes`` — reachable from some *other* remote-tracking ref. The commit
      has been shared somewhere, which is worth saying, but it does not imply
      anything about this branch's upstream.

    Absence of evidence is not evidence of absence: remote-tracking refs can be
    stale or incomplete. ``None`` means "unknown", and the UI says so rather
    than implying the commits are private.
    """
    info = branch_upstream(repo, branch)

    # NB these are the *un*published sets — the commits rev-list reports as
    # reachable from the branch but NOT from the reference. Deriving them this
    # way avoids enumerating the branch's entire history just to subtract:
    # a commit is published iff it is absent from this set.
    #
    # Keys are named for the *sets*, distinct from the ref-name keys below, so
    # no merge of the two dicts can ever put a string where a set belongs.
    not_on_upstream: set[str] | None = None
    if info["upstream"] and ref_exists(repo, info["upstream"]):
        not_on_upstream = set(
            run_git(
                repo,
                ["rev-list", f"refs/heads/{branch}", "--not", info["upstream"], "--"],
                check=False,
            ).stdout.split()
        )

    not_on_any_remote: set[str] | None = None
    if run_git(
        repo, ["for-each-ref", "--count=1", "refs/remotes/"], check=False
    ).stdout.strip():
        not_on_any_remote = set(
            run_git(
                repo,
                ["rev-list", f"refs/heads/{branch}", "--not", "--remotes", "--"],
                check=False,
            ).stdout.split()
        )

    return {
        "not_on_upstream": not_on_upstream,
        "not_on_any_remote": not_on_any_remote,
        "upstream_ref": info["upstream"],
        "push_ref": info["push"],
    }


def _signed_shas(repo: str, shas: list[str]) -> set[str]:
    """The subset of ``shas`` whose commit header carries a signature.

    Read from the raw object rather than ``%G?``: that reports *verification*
    status and says ``N`` for a signature git cannot check (no key, no
    allowed-signers file) — exactly the case in which a rewrite would silently
    strip a signature the user needs warning about.
    """
    if not shas:
        return set()
    out = run_git(
        repo, ["cat-file", "--batch"], stdin="\n".join(shas) + "\n", binary=True
    ).stdout
    signed: set[str] = set()
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].split()
        if len(header) != 3:
            raise GitError(f"Unexpected cat-file record: {out[pos:header_end]!r}")
        sha, _, size = header
        start = header_end + 1
        end = start + int(size)
        commit_headers = out[start:end].split(b"\n\n", 1)[0]
        if any(
            line.startswith((b"gpgsig ", b"gpgsig-sha256 "))
            for line in commit_headers.split(b"\n")
        ):
            signed.add(sha.decode("ascii"))
        pos = end + 1  # cat-file appends a newline after each object
    return signed


def get_commits(
    repo: str, branch: str, *, base: str | None = None, limit: int = DEFAULT_LIMIT
) -> list[Commit]:
    """Return commits on ``branch`` newest-first (topological order).

    If ``base`` is given, returns ``base..branch`` (commits on the branch but
    not on base). Otherwise returns the most recent ``limit`` commits. Each
    commit is flagged with whether it is signed (GPG or SSH) and whether it
    has been pushed to a remote.
    """
    # Fields are newline-separated: git strips newlines from idents, whereas a
    # control byte such as %x1f is legal inside an author name and would shift
    # every field. %B (raw body) MUST stay last so the parser's split(…, 10)
    # leaves the message's own newlines in the final field.
    fmt = "%H%n%T%n%P%n%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%B"
    check_range_size(repo, branch, base)
    range_args = (
        [f"{base}..refs/heads/{branch}"]
        if base
        else ["-n", str(limit), f"refs/heads/{branch}"]
    )
    # Trailing `--` asserts no pathspecs follow, disambiguating a ref that might
    # otherwise look like a path. (A *leading* `--` would wrongly turn the range
    # into a pathspec.)
    args = ["log", "--topo-order", "-z", f"--format={fmt}", *range_args, "--"]
    # Pin the output encoding: i18n.logOutputEncoding in the user's config would
    # otherwise make git emit something other than UTF-8, which our decoder
    # assumes. With this set, %B is UTF-8 whatever the commit's own encoding.
    out = run_git(repo, ["-c", "i18n.logOutputEncoding=UTF-8", *args]).stdout

    records = []
    for record in out.split("\x00"):
        if not record:
            continue
        fields = record.split("\n", 9)
        if len(fields) != 10:
            raise GitError(f"Malformed commit record from git log: {record[:80]!r}")
        records.append(fields)

    published = _unpublished_shas(repo, branch)
    signed = _signed_shas(repo, [fields[0] for fields in records])
    commits: list[Commit] = []
    for sha, tree, parents, an, ae, adate, cn, ce, cdate, message in records:
        commits.append(
            Commit(
                sha=sha,
                tree=tree,
                parents=parents.split() if parents else [],
                author_name=an,
                author_email=ae,
                author_date=adate,
                committer_name=cn,
                committer_email=ce,
                committer_date=cdate,
                message=message.rstrip("\n"),
                signed=sha in signed,
                # Published == NOT in the "unpublished" set for that reference.
                pushed=(
                    published["not_on_upstream"] is not None
                    and sha not in published["not_on_upstream"]
                ),
                on_remote=(
                    published["not_on_any_remote"] is not None
                    and sha not in published["not_on_any_remote"]
                ),
                publication_known=published["not_on_upstream"] is not None
                or published["not_on_any_remote"] is not None,
            )
        )
    return commits


def _diff_tree_args(repo: str, sha: str) -> tuple[list[str], bool]:
    """Revision arguments that make ``diff-tree`` describe one commit.

    ``diff-tree <sha>`` prints **nothing at all** for a root commit (no parent
    to diff against) and for a merge (ambiguous — which parent?), in both cases
    exiting 0. Those silent empties are the bug this function exists to avoid:

    * root commit  -> ``--root``, which diffs against the empty tree.
    * merge commit -> an explicit ``<sha>^1 <sha>`` two-tree diff, i.e. what the
      merge brought onto this branch (the same thing ``git log --first-parent``
      shows). Returns ``True`` so the caller can label it.
    """
    out = run_git(repo, ["rev-list", "--parents", "-n", "1", sha]).stdout
    parents = out.split()[1:]  # first token is the commit itself
    if len(parents) > 1:
        return [f"{sha}^1", sha], True
    return ["--root", sha], False


def _nul_tokens(out: bytes) -> list[str]:
    """Split ``-z`` output into decoded tokens.

    Bytes in, decoded per token: text mode would translate a ``\\r`` inside a
    filename into ``\\n``, silently merging two distinct files. Only the empty
    tail after the terminating NUL is dropped; an empty token anywhere else
    is a malformed record for the callers to refuse.
    """
    toks = [t.decode("utf-8", "surrogateescape") for t in out.split(b"\0")]
    if toks and toks[-1] == "":
        toks.pop()
    return toks


def _parse_numstat_z(out: bytes) -> list[tuple[str, str, str, str | None]]:
    """Parse ``--numstat -z`` into ``(added, deleted, path, old_path)`` rows.

    ``-z`` is not a nicety: without it git *quotes and escapes* unusual paths,
    and a path containing a tab would be misparsed. The NUL format is
    unambiguous. A normal entry is one record ``added\\tdeleted\\tpath``; a
    rename/copy has an empty third field and its two paths follow as the next
    two NUL-separated records.
    """
    toks = _nul_tokens(out)
    rows: list[tuple[str, str, str, str | None]] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        # split("\t", 2) — NOT a bare split: a *filename* may legitimately
        # contain a tab, and an unrestricted split truncates it at that tab.
        parts = tok.split("\t", 2)
        if len(parts) < 3:
            raise GitError(f"Malformed numstat record from git: {tok!r}")
        added, deleted, path = parts[0], parts[1], parts[2]
        old: str | None = None
        if path == "":  # rename/copy: old and new follow as separate records
            paths = toks[i + 1 : i + 3]
            if len(paths) < 2 or not all(paths):
                raise GitError(
                    f"Malformed numstat rename record from git: {toks[i : i + 3]!r}"
                )
            old, path = paths
            i += 3
        else:
            i += 1
        rows.append((added, deleted, path, old))
    return rows


def _parse_name_status_z(out: bytes) -> dict[str, str]:
    """Parse ``--name-status -z`` into ``{new_path: status}``.

    ``R``/``C`` entries carry a similarity score (``R080``) and *two* paths; the
    status is recorded against the **new** path so it joins with the numstat
    rows, which key on the new path too. (Without ``-z`` a rename would read
    ``old.txt => new.txt`` on one side and ``new.txt`` on the other.)

    A short record raises, as in :func:`_parse_numstat_z`: recording a rename
    against ``""`` or against its *old* path would then fail to join.
    """
    toks = _nul_tokens(out)
    status_by_path: dict[str, str] = {}
    i = 0
    while i < len(toks):
        st = toks[i]
        n_paths = 2 if st[:1] in ("R", "C") else 1
        paths = toks[i + 1 : i + 1 + n_paths]
        if not st or len(paths) < n_paths or not all(paths):
            raise GitError(
                f"Malformed name-status record from git: {toks[i : i + 1 + n_paths]!r}"
            )
        status_by_path[paths[-1]] = st
        i += 1 + n_paths
    return status_by_path


def commit_stat(repo: str, sha: object) -> dict[str, Any]:
    """Return a name-status + numstat summary for one commit (for UI preview).

    Uses ``diff-tree`` rather than ``show``: git rejects ``git show --no-patch
    --name-status`` outright ("options '--name-only', '--name-status',
    '--check', and '-s' cannot be used together"). Both calls below are
    checked, so a flag conflict fails loudly instead of yielding empty output
    that would label every file ``M``.
    """
    sha = validate_sha(sha, "sha")
    if not ref_exists(repo, sha):
        raise GitError(f"Unknown commit: {sha[:9]}")

    revs, vs_first_parent = _diff_tree_args(repo, sha)
    # --no-textconv: textconv filters are for human reading and would make the
    # line counts describe the filtered text rather than what is committed.
    common = [
        "diff-tree",
        "--no-commit-id",
        "-r",
        "-M",
        "-z",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
    ]
    numstat = run_git(repo, [*common, "--numstat", *revs, "--"], binary=True).stdout
    names = run_git(repo, [*common, "--name-status", *revs, "--"], binary=True).stdout

    status_by_path = _parse_name_status_z(names)
    files = []
    total_add = total_del = 0
    for added, deleted, path, old in _parse_numstat_z(numstat):
        # "-" means a binary file, for which git reports no line counts.
        total_add += 0 if added == "-" else int(added)
        total_del += 0 if deleted == "-" else int(deleted)
        status = status_by_path.get(path)
        if status is None:
            # The two calls describe the same diff, so every numstat path must
            # have a status. A mismatch means our parsing is wrong; say so
            # rather than fall back to a silent "M" that would hide it.
            raise GitError(f"No status for {path!r} in diff of {sha[:9]}.")
        entry = {
            "path": path,
            "added": added,
            "deleted": deleted,
            "status": status,
            "binary": added == "-" and deleted == "-",
        }
        if old is not None:
            entry["old_path"] = old
        files.append(entry)

    return {
        "sha": sha,
        "files": files,
        "file_count": len(files),
        "additions": total_add,
        "deletions": total_del,
        "vs_first_parent": vs_first_parent,
    }


# A single commit's patch can be enormous (a vendored dependency, a lockfile,
# a generated file). Cap what we ship to the browser and say so, rather than
# stalling the UI on a 40 MB diff nobody will read.
MAX_DIFF_BYTES = 512 * 1024


def commit_diff(
    repo: str, sha: object, max_bytes: int = MAX_DIFF_BYTES
) -> dict[str, Any]:
    """Return the full patch for one commit, for on-demand display in the UI.

    Fetched per commit only when the user opens it, not eagerly for the whole
    range: a range can be hundreds of commits and the patches are far larger
    than the messages this tool exists to edit.
    """
    sha = validate_sha(sha, "sha")
    if not ref_exists(repo, sha):
        raise GitError(f"Unknown commit: {sha[:9]}")

    revs, vs_first_parent = _diff_tree_args(repo, sha)
    # Bytes, not text: text mode's universal-newline translation would turn a
    # CRLF file's "+line\r\n" into "+line\n" and the patch would misdescribe
    # what is committed.
    raw = run_git(
        repo,
        [
            "diff-tree",
            "--no-commit-id",
            "-r",
            "-M",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            # Deliberately no --stat: the "files" toggle already shows that, and
            # repeating it above every patch is noise.
            *revs,
            "--",
        ],
        binary=True,
    ).stdout
    truncated = len(raw) > max_bytes
    if truncated:
        # Cut in bytes (the cap is a byte budget) on a newline, which is always
        # a character boundary in UTF-8, so no line is half-rendered.
        raw = raw[:max_bytes].rsplit(b"\n", 1)[0]
    text = raw.decode("utf-8", "surrogateescape")
    return {
        "sha": sha,
        "diff": text,
        "truncated": truncated,
        "vs_first_parent": vs_first_parent,
    }


def normalize_message(msg: str) -> str:
    """Normalise line endings and leading/trailing blank lines for storage.

    Leading blank lines go for the same reason ``git commit`` drops them: git
    reads the subject from the first non-blank line, so keeping them would make
    the stored subject and the displayed one disagree. Trailing lines that are
    blank *or whitespace-only* go too — a lone ``" "`` line at the end is not
    content — so the stored text matches what commit-tree writes (one trailing
    newline). Trailing spaces on a line of text are left alone.
    """
    text = msg.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\A(?:[ \t]*\n)+", "", text)
    return re.sub(r"(?:\n[ \t]*)+\Z", "", text)


def _commit_tree(
    repo: str, commit: Commit, new_parents: list[str], message: str
) -> str:
    """Create a new commit object identical to ``commit`` but with a new
    message and parents. Returns the new SHA."""
    args = ["commit-tree", commit.tree]
    for parent in new_parents:
        args += ["-p", parent]
    # Committer identity and date are carried over as well as the author's:
    # the contract is "only the message changed", not "rebased just now".
    env = {
        "GIT_AUTHOR_NAME": commit.author_name,
        "GIT_AUTHOR_EMAIL": commit.author_email,
        "GIT_AUTHOR_DATE": commit.author_date,
        "GIT_COMMITTER_NAME": commit.committer_name,
        "GIT_COMMITTER_EMAIL": commit.committer_email,
        "GIT_COMMITTER_DATE": commit.committer_date,
    }
    # commit-tree takes the message from stdin verbatim and performs NO cleanup
    # (unlike `git commit`), so the message is stored exactly as typed — even if
    # it is all comment-like `#` lines. Validation upstream rejects empty ones.
    #
    # i18n.commitEncoding is pinned to UTF-8 for two reasons. First, we hand
    # git UTF-8 (that is what we decoded and what the browser sent back), so
    # inheriting a legacy i18n.commitEncoding from the user's config would make
    # git label UTF-8 bytes with the wrong encoding header. Second, with it
    # unset git "repairs" bytes it cannot read as UTF-8 by reinterpreting them
    # as Latin-1 — a silent corruption of the message we asked it to store.
    result = run_git(
        repo, ["-c", "i18n.commitEncoding=UTF-8", *args], stdin=message + "\n", env=env
    )
    return result.stdout.strip()


@dataclass
class RewriteResult:
    branch: str
    old_tip: str
    new_tip: str
    rewritten: list[dict[str, Any]] = field(
        default_factory=list
    )  # {old, new, subject, ...}
    backup_ref: str | None = None
    changed: bool = True
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "old_tip": self.old_tip,
            "old_tip_short": self.old_tip[:9],
            "new_tip": self.new_tip,
            "new_tip_short": self.new_tip[:9],
            "rewritten": self.rewritten,
            "rewritten_count": len(self.rewritten),
            "message_change_count": sum(
                1 for r in self.rewritten if r.get("message_changed")
            ),
            "backup_ref": self.backup_ref,
            "changed": self.changed,
            "dry_run": self.dry_run,
        }


# --------------------------------------------------------------------------- #
# Repository state                                                             #
# --------------------------------------------------------------------------- #


def get_operation_state(repo: str) -> list[str]:
    """Return active git operations that make history rewriting unsafe."""
    git_dir = run_git(repo, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
    g = Path(git_dir)
    in_progress = {
        "rebase (rebase-merge)": g / "rebase-merge",
        "rebase (rebase-apply)": g / "rebase-apply",
        "merge": g / "MERGE_HEAD",
        "cherry-pick": g / "CHERRY_PICK_HEAD",
        "revert": g / "REVERT_HEAD",
        "bisect": g / "BISECT_LOG",
    }
    return [name for name, p in in_progress.items() if p.exists()]


def check_repo_writable(repo: str) -> None:
    """Raise :class:`GitError` if the repo is in a state unsafe to rewrite in.

    Each refusal below is a case where a rewrite would otherwise *silently*
    produce wrong history rather than fail, which is the worst outcome for a
    tool like this.
    """
    # Every worktree, not just this one: a rebase stopped on a conflict in a
    # linked worktree keeps its state (and its claim on the branch) over there,
    # and `git rebase --abort` there would undo a rewrite made from here.
    for path, _ in _worktrees(repo):
        if not os.path.isdir(path):
            continue  # pruned or unmounted; git ignores it too
        active = get_operation_state(path)
        if active:
            raise GitError(
                f"Repository has an operation in progress in {path}: "
                + ", ".join(active)
                + ". Finish or abort it before editing commit messages."
            )
        # A conflicted index means the user is mid-something even if no
        # operation marker survived.
        unmerged = run_git(path, ["ls-files", "--unmerged", "-z"], check=False).stdout
        if unmerged.strip("\0"):
            raise GitError(
                f"The index in {path} has unmerged entries. Resolve the conflict "
                "before editing commit messages."
            )

    # Shallow clone: at the shallow boundary git reports NO parent for a commit
    # that really has one. Replaying such a commit would write it as a genuine
    # root and silently orphan everything beneath it.
    if (
        run_git(
            repo, ["rev-parse", "--is-shallow-repository"], check=False
        ).stdout.strip()
        == "true"
    ):
        raise GitError(
            "This is a shallow clone. Rewriting would turn the shallow boundary "
            "into a root commit and lose its history. Run `git fetch --unshallow` "
            "first."
        )

    # Replacement refs and grafts rewrite parentage (and trees) behind git's
    # back. We pass --no-replace-objects everywhere, but grafts are NOT disabled
    # by that flag, and a repo relying on either is not one to rewrite blindly.
    if run_git(
        repo,
        ["for-each-ref", "--count=1", "--format=%(refname)", "refs/replace/"],
        check=False,
    ).stdout.strip():
        raise GitError(
            "This repository has replacement refs (refs/replace/*). Rewriting "
            "through them is not supported — remove them or use git-filter-repo."
        )
    graft_path = run_git(
        repo,
        ["rev-parse", "--path-format=absolute", "--git-path", "info/grafts"],
        check=False,
    ).stdout.strip()
    if graft_path and os.path.exists(graft_path):
        raise GitError(
            "This repository has a grafts file (.git/info/grafts), which changes "
            "commit parentage. Rewriting is not supported until it is removed."
        )


def _worktrees(repo: str) -> list[tuple[str, str | None]]:
    """``(path, checked-out ref or None)`` for every worktree of the repository.

    ``-z`` and bytes: without ``-z`` git C-quotes a path containing a newline
    (``"…/wt\\nline2"``), which names no directory on disk, so the worktree —
    and any conflicted rebase in it — would be skipped as pruned. With ``-z``
    each attribute is NUL-terminated and records are separated by an empty
    attribute (``\\0\\0``); binary mode keeps a ``\\r`` in the path intact.
    """
    out = run_git(
        repo, ["worktree", "list", "--porcelain", "-z"], check=False, binary=True
    ).stdout
    worktrees: list[tuple[str, str | None]] = []
    for block in out.split(b"\0\0"):
        path: str | None = None
        branch: str | None = None
        for attr in block.split(b"\0"):
            if attr.startswith(b"worktree "):
                path = os.fsdecode(attr[len(b"worktree ") :])
            elif attr.startswith(b"branch "):
                branch = attr[len(b"branch ") :].decode("utf-8", "surrogateescape")
        if path:
            worktrees.append((path, branch))
    return worktrees


def branch_checked_out_elsewhere(repo: str, branch: str) -> list[str]:
    """Return paths of OTHER worktrees that have ``branch`` checked out.

    Moving the branch ref while it is checked out in a linked worktree leaves
    that worktree's HEAD pointing at the rewritten tip; its working tree is
    untouched, so ``git status`` there may show spurious changes.
    """
    main_root = run_git(
        repo, ["rev-parse", "--show-toplevel"], check=False
    ).stdout.strip()
    return [
        path
        for path, ref in _worktrees(repo)
        if ref == f"refs/heads/{branch}"
        and os.path.realpath(path) != os.path.realpath(main_root)
    ]


def get_repo_state(repo: str, branch: str | None = None) -> dict[str, Any]:
    """Status used to warn the user in the UI before/while editing."""
    current = _current_branch(repo)
    dirty = bool(run_git(repo, ["status", "--porcelain"], check=False).stdout.strip())
    operations = get_operation_state(repo)
    other_worktrees = branch_checked_out_elsewhere(repo, branch) if branch else []
    return {
        "current_branch": current,
        "dirty": dirty,
        "is_checked_out": bool(branch) and current == branch,
        "operation_in_progress": operations[0] if operations else None,
        "other_worktrees": other_worktrees,
    }


# --------------------------------------------------------------------------- #
# The rewrite engine                                                          #
# --------------------------------------------------------------------------- #


def _apply_rewrite_atomic(
    repo: str, branch: str, old_tip: str, new_tip: str, backup_ref: str
) -> None:
    """Create the backup ref and move the branch in ONE atomic transaction.

    The ``update`` line is a verified compare-and-swap: if the branch no longer
    points at ``old_tip`` the whole transaction aborts and the backup ref is
    never created. ``-z`` (NUL-delimited) avoids any whitespace ambiguity.

    ``option no-deref`` applies to the next ref-naming command: without it
    ``update`` follows a symbolic ref and moves the ref it *points at*.
    ``validate_local_branch`` refuses symbolic branches before we get here;
    this makes the transaction itself safe even if one slipped through.
    """
    stdin = (
        "start\0"
        f"create {backup_ref}\0{old_tip}\0"
        "option no-deref\0"
        f"update refs/heads/{branch}\0{new_tip}\0{old_tip}\0"
        "prepare\0"
        "commit\0"
    )
    # The top-level -m sets the reflog message for the branch move in the txn.
    run_git(
        repo,
        [
            "update-ref",
            "-m",
            "git-commit-editor: reword commit message(s)",
            "-z",
            "--stdin",
        ],
        stdin=stdin,
    )


def rewrite_messages(
    repo: str,
    branch: str,
    edits: dict[str, str],
    *,
    base: str | None = None,
    limit: int = DEFAULT_LIMIT,
    expected_old_tip: str | None = None,
    dry_run: bool = False,
) -> RewriteResult:
    """Rewrite commit messages on ``branch``.

    ``edits`` maps a commit SHA to its new full message. Only commits within the
    same range used for display (``base``/``limit``) may be edited. Every
    descendant of an edited commit is replayed so the branch stays valid. A
    backup ref is created and the branch ref is moved in one atomic transaction.

    With ``dry_run=True`` everything is computed (including the replayed commit
    objects, so SHAs and the true blast radius are known) but the ref
    transaction is skipped — used to preview impact before confirming.
    """
    check_repo_writable(repo)

    # The candidate set is exactly what the user was shown, newest-first.
    commits = get_commits(repo, branch, base=base, limit=limit)
    if not commits:
        raise GitError("No commits found in the selected range.")

    by_sha = {c.sha: c for c in commits}
    old_tip = branch_tip(repo, branch)

    if expected_old_tip:
        validate_sha(expected_old_tip, "expected_old_tip")
    if expected_old_tip and expected_old_tip != old_tip:
        raise GitError(
            f"Branch '{branch}' has moved since it was loaded "
            f"(expected {expected_old_tip[:9]}, now {old_tip[:9]}). "
            "Reload commits and try again."
        )

    # Validate edits: known SHA, sane length, non-empty message.
    clean_edits: dict[str, str] = {}
    for sha, message in edits.items():
        validate_sha(sha, "commit id")
        if sha not in by_sha:
            raise GitError(
                f"Commit {sha[:9]} is not in the editable range; reload and retry."
            )
        if not isinstance(message, str) or len(message) > MAX_MSG_LEN:
            raise GitError(f"Message for {sha[:9]} is missing or too long.")
        normalised = normalize_message(message)
        if not normalised.strip():
            raise GitError(
                f"Commit {sha[:9]} would have an empty message; that is not allowed."
            )
        if normalised != normalize_message(by_sha[sha].message):
            clean_edits[sha] = normalised

    if not clean_edits:
        return RewriteResult(
            branch=branch, old_tip=old_tip, new_tip=old_tip, rewritten=[], changed=False
        )

    # Replay oldest-first (commits list is newest-first topo order).
    new_by_old: dict[str, str] = {}
    rewritten: list[dict[str, Any]] = []
    for commit in reversed(commits):
        new_parents: list[str] = []
        parent_changed = False
        for parent in commit.parents:
            mapped = new_by_old.get(parent, parent)
            new_parents.append(mapped)
            if mapped != parent:
                parent_changed = True

        new_message = clean_edits.get(commit.sha)
        if new_message is None and not parent_changed:
            new_by_old[commit.sha] = commit.sha  # untouched, maps to itself
            continue

        message = new_message if new_message is not None else commit.message
        new_sha = _commit_tree(repo, commit, new_parents, message)
        new_by_old[commit.sha] = new_sha
        if new_sha != commit.sha:
            rewritten.append(
                {
                    "old": commit.sha,
                    "old_short": commit.short,
                    "new": new_sha,
                    "new_short": new_sha[:9],
                    "subject": _subject(message),
                    "message_changed": new_message is not None,
                }
            )

    new_tip = new_by_old.get(old_tip, old_tip)
    if new_tip == old_tip:
        # Every edited commit is an ancestor of the tip, so a replay that
        # changed something must have remapped the tip; anything else means an
        # edit was dropped, which must never pass as "nothing to do".
        raise GitError(
            "Internal error: the edit did not propagate to the branch tip; "
            "nothing was changed."
        )

    if dry_run:
        return RewriteResult(
            branch=branch,
            old_tip=old_tip,
            new_tip=new_tip,
            rewritten=rewritten,
            backup_ref=None,
            changed=True,
            dry_run=True,
        )

    # The branch name keeps its slashes: flattening "/" to "_" is not reversible
    # ("feature/fix" and "feature_fix" would share a backup name). An undo and
    # resave within the same second would reuse the name, so disambiguate.
    backup_ref = _backup_ref(branch, old_tip, int(time.time()))
    candidate, n = backup_ref, 1
    while ref_exists(repo, candidate):
        n += 1
        candidate = f"{backup_ref}-{n}"
    backup_ref = candidate
    _apply_rewrite_atomic(repo, branch, old_tip, new_tip, backup_ref)

    return RewriteResult(
        branch=branch,
        old_tip=old_tip,
        new_tip=new_tip,
        rewritten=rewritten,
        backup_ref=backup_ref,
        changed=True,
    )
