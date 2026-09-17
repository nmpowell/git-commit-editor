"""
Git Commit Message Editor

A small Flask web application to view and edit the commit messages on a branch
of a git repository. You provide a path inside the repo, pick a branch, and
edit any commit's message; saving rewrites the affected commits (and replays
their descendants) while preserving authorship, dates and trees.

History is rewritten in place on the selected branch. A backup branch under
`refs/heads/commit-editor-backup/` and a reflog entry are created on every
save, so changes are easy to undo. Intended as a local tool (binds 127.0.0.1).
"""

import ipaddress
import os
import re
import secrets
import textwrap
from typing import Any
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from . import gitops
from .gitops import GitError

app = Flask(__name__)
# History rewriting is consequential; bound the request body to a sane size.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB


# --------------------------------------------------------------------------- #
# Commit-message wrapping                                                     #
# --------------------------------------------------------------------------- #

# Default column at which to hard-wrap message *bodies* on save. Git's "50/72
# rule" wraps bodies at 72 so `git log` (which indents messages by 4) still fits
# an 80-column terminal. The subject line is never wrapped. Override with
# --wrap-width; a value <= 0 disables wrapping entirely.
WRAP_WIDTH = 72
# Seeded here so routes can read config[...] outright; main() overrides both.
app.config.setdefault("WRAP_WIDTH", WRAP_WIDTH)
app.config.setdefault("DEFAULT_REPO", "")

# A list item: up to 3 leading spaces, a bullet (-, *, +) or a number (1. / 1)),
# then whitespace and the item text. Four or more leading spaces is treated as
# an indented/preformatted block instead and left untouched. At most two
# digits: a year ("2024. That was when…") opening a sentence is prose, and
# nobody hand-numbers a commit message past 99.
_BULLET_RE = re.compile(r"^( {0,3})([-*+]|\d{1,2}[.)])(\s+)(.*)$")

# A "Key: value" line — the shape of a git trailer (Signed-off-by, Co-authored-
# by, …). Such lines must never be joined into a paragraph or wrapped mid-value.
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*:\s+\S")

# Trailer keys common enough that a lone one (e.g. an automated Co-authored-by
# footer surrounded by blank lines) is recognised without needing a neighbour.
_KNOWN_TRAILER_KEYS = frozenset(
    {
        "signed-off-by",
        "co-authored-by",
        "acked-by",
        "reviewed-by",
        "tested-by",
        "reported-by",
        "suggested-by",
        "helped-by",
        "cc",
        "change-id",
        "reviewed-on",
    }
)


def _is_preformatted(line: str, hang: int = 3) -> bool:
    """True if ``line`` is an indented block to leave verbatim.

    A leading tab always is. Spaces are relative to ``hang``, the deepest
    indent a line can have and still be prose: 3 at top level (four or more
    is an indented block), or the marker's width for a line under a list
    item, whose continuation lines hang at that width. One predicate for both
    places so a tab-indented code line under a bullet cannot be classed as
    code by the outer loop but as continuation text by the list loop.
    """
    return line[:1] == "\t" or len(line) - len(line.lstrip(" ")) > hang


def _is_trailer(lines: list[str], idx: int, final_para_start: int) -> bool:
    """True if ``lines[idx]`` should be treated as a git trailer line.

    A ``Key: value`` line counts as a trailer when its key is a well-known
    trailer key, or when it sits next to another ``Key: value`` line *in the
    final paragraph* — the only place git itself reads trailers from. A lone
    ``Note: …`` that starts a prose sentence, or a ``Before:``/``After:`` pair
    mid-body, is therefore wrapped normally.
    """
    if not _TRAILER_RE.match(lines[idx]):
        return False
    key = lines[idx].split(":", 1)[0].lower()
    if key in _KNOWN_TRAILER_KEYS:
        return True
    if idx < final_para_start:
        return False
    prev_is = idx > final_para_start and bool(_TRAILER_RE.match(lines[idx - 1]))
    next_is = idx + 1 < len(lines) and bool(_TRAILER_RE.match(lines[idx + 1]))
    return prev_is or next_is


def wrap_commit_message(message: str, width: int = WRAP_WIDTH) -> str:
    """Hard-wrap a commit message's body so it reads well in ``git log``.

    Follows git's conventions:

    * The **subject** (first line) is never wrapped — subjects belong on one line.
    * **Blank lines** delimit paragraphs and are preserved.
    * **Prose paragraphs** are re-flowed: their lines are joined and greedily
      re-wrapped at ``width``, so superfluous manual line breaks (a break placed
      earlier than the column limit) are removed. Long tokens such as URLs and
      dotted.paths, and hyphenated words, are never split.
    * **List items** (``- ``/``* ``/``1. ``) wrap with a hanging indent.
    * **Trailers** (``Signed-off-by:`` …) and **indented blocks** (code, quoted
      output) are left exactly as written.
    * A **blank line after the subject** is inserted if the author didn't
      leave one, so the body can never be folded into the subject (see below).

    ``width <= 0`` disables wrapping; only line endings are normalised (the
    missing-blank-line insertion below still applies).
    """
    text = gitops.normalize_message(message)
    lines = text.split("\n")
    body = lines[1:]
    # Git treats everything up to the first blank line as the subject. A
    # message typed as "Subject\nBody starts here." — with no blank line — is
    # therefore committed with the body folded straight into the subject:
    # `git log --oneline` shows "Subject Body starts here." instead of just
    # "Subject", silently producing a wrong commit. Insert the missing
    # separator here, ahead of the width<=0 early-out below, since this is a
    # structural rule rather than a wrapping one.
    if body and body[0].strip():
        body.insert(0, "")
        text = "\n".join([lines[0]] + body)

    if width <= 0:
        return text

    out = [lines[0].rstrip()]  # subject: verbatim, never wrapped
    n = len(body)
    para: list[str] = []  # buffered prose lines awaiting a re-flow

    def flush_prose() -> None:
        if para:
            joined = " ".join(s.strip() for s in para)
            out.extend(
                textwrap.wrap(
                    joined,
                    width=width,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
            para.clear()

    blank_indexes = [k for k, s in enumerate(body) if not s.strip()]
    final_para_start = blank_indexes[-1] + 1 if blank_indexes else 0

    i = 0
    in_list = False  # are we inside a run of list items?
    while i < n:
        line = body[i]

        if not line.strip():  # blank line -> paragraph break
            flush_prose()
            out.append("")
            in_list = False
            i += 1
            continue

        if _is_preformatted(line):  # indented block: leave verbatim
            flush_prose()
            out.append(line.rstrip())
            in_list = False
            i += 1
            continue

        bullet = _BULLET_RE.match(line)
        # A leading "- "/"* "/"1. " is a list marker only in genuine list
        # context: at a paragraph start, continuing an open list, after an
        # intro line ending in ":", or when the next line is also a marker.
        # This stops a mid-sentence em-dash — e.g. "consumer - so the …" that
        # wrapping happens to push to a line start — from being misread as a
        # bullet (and keeps wrapping idempotent).
        at_block_start = i == 0 or not body[i - 1].strip()
        prev_ends_colon = i > 0 and body[i - 1].rstrip().endswith(":")
        next_is_marker = i + 1 < n and bool(_BULLET_RE.match(body[i + 1]))
        if bullet and (in_list or at_block_start or prev_ends_colon or next_is_marker):
            flush_prose()
            in_list = True
            prefix = bullet.group(1) + bullet.group(2) + bullet.group(3)
            item = [bullet.group(4).strip()]
            i += 1
            # A continuation line hangs at or inside the marker's width (or
            # the top-level 3, whichever is deeper); a tab or anything deeper
            # is a code block under the item, left verbatim.
            hang = max(3, len(prefix))
            while (
                i < n
                and body[i].strip()
                and not _is_preformatted(body[i], hang)
                and not _BULLET_RE.match(body[i])
                and not _is_trailer(body, i, final_para_start)
            ):
                item.append(body[i].strip())
                i += 1
            out.extend(
                textwrap.wrap(
                    " ".join(s for s in item if s),
                    width=width,
                    initial_indent=prefix,
                    subsequent_indent=" " * len(prefix),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                or [prefix.rstrip()]
            )
            continue

        if _is_trailer(body, i, final_para_start):  # trailer: never wrapped or joined
            flush_prose()
            out.append(line.rstrip())
            in_list = False
            i += 1
            continue

        para.append(line)  # ordinary prose
        in_list = False
        i += 1

    flush_prose()
    # Drop any trailing blank lines we may have emitted; git strips them anyway.
    while len(out) > 1 and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _resolve_repo(payload: dict[str, Any]) -> str:
    """Resolve and validate the repo path from a request body.

    No fallback to ``DEFAULT_REPO``: that only prefills the page. A request
    that arrives without a repository has lost track of which one the user was
    looking at, and must not be quietly pointed at a different one.
    """
    repo_input = (payload.get("repo") or "").strip()
    if not repo_input:
        raise GitError("A repository path is required.")
    root = gitops.get_git_root(repo_input)
    if not root:
        raise GitError(f"Not a git repository with a working tree: {repo_input}")
    return root


# One token per process, embedded in the page and required on every API POST.
# It is not the primary defence (host validation below is), but it does block a
# blind cross-site POST that arrives with no usable Origin: an attacker page
# cannot read this value and cannot set a custom header without a CORS
# preflight, which this app never approves.
CSRF_TOKEN = secrets.token_urlsafe(32)

# Host header values this app will answer to, refused *before* any response
# body is produced. Checking Origin against request.host would not do: under
# DNS rebinding both are attacker-controlled and agree.
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _host_is_loopback(host: str) -> bool:
    """True if a Host header names loopback, on any port.

    Checked by *name*, not by resolving: a name that merely resolves to
    127.0.0.1 is exactly the DNS-rebinding case this exists to reject.
    """
    if host in _LOOPBACK_NAMES:  # before splitting: a bare "::1" is all colons
        return True
    hostname = (
        host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    )
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    if hostname in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@app.before_request
def _security_guard() -> ResponseReturnValue | None:
    # Deliberately applies to "/" too, not just the API: a rebinding attacker
    # that can load the page can read the CSRF token out of it. A loopback
    # test rather than an allowlist built from the CLI port, so it holds
    # whatever entry point starts the app (`flask run` never sees main()).
    if not _host_is_loopback(request.host):
        return jsonify({"error": "Invalid Host header."}), 400

    if request.path.startswith("/api/") and request.method == "POST":
        origin = request.headers.get("Origin")
        if origin is not None:
            # Compare scheme+host+port. The literal "null" (sandboxed iframes,
            # some redirects) parses to an empty netloc and so is refused too.
            parsed = urlparse(origin)
            if parsed.netloc != request.host or parsed.scheme not in ("http", "https"):
                return jsonify({"error": "Cross-origin request refused."}), 403
        if not request.is_json:
            return jsonify({"error": "Expected Content-Type: application/json."}), 415
        token = request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, CSRF_TOKEN):
            return (
                jsonify(
                    {"error": "Invalid or missing request token. Reload the page."}
                ),
                403,
            )
    return None


@app.after_request
def _security_headers(resp: Response) -> Response:
    # Cheap defence in depth. 'self' only: this app loads no third-party code.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.errorhandler(GitError)
def _handle_git_error(err: GitError) -> ResponseReturnValue:
    return jsonify({"error": str(err)}), 400


@app.errorhandler(Exception)
def _handle_unexpected(err: Exception) -> ResponseReturnValue:
    if isinstance(err, HTTPException):
        # The UI reads every API failure as {"error": …}; a 413/404 rendered as
        # HTML would surface as a parse failure rather than its own message.
        if request.path.startswith("/api/"):
            return jsonify({"error": err.description}), err.code or 500
        return err
    app.logger.exception("Unhandled error")
    return jsonify({"error": "Unexpected server error. Check the server logs."}), 500


@app.route("/")
def index() -> str:
    # wrap_width is passed through so the editor's "subject too long" hint is
    # driven by the width the server will actually wrap at, rather than a
    # constant in app.js that silently disagrees when --wrap-width is set.
    return render_template(
        "index.html",
        default_repo=app.config["DEFAULT_REPO"],
        wrap_width=app.config["WRAP_WIDTH"],
        csrf_token=CSRF_TOKEN,
        default_limit=gitops.DEFAULT_LIMIT,
        max_commits=gitops.MAX_COMMITS,
    )


@app.route("/api/branches", methods=["POST"])
def api_branches() -> Response:
    payload = request.get_json(silent=True) or {}
    repo = _resolve_repo(payload)
    info = gitops.list_branches(repo)
    return jsonify(
        {
            "repo_path": repo,
            "repo_id": gitops.repo_identity(repo),
            "branches": info["branches"],
            "current": info["current"],
            "default_branch": gitops.detect_default_branch(repo),
            "state": gitops.get_repo_state(repo, info["current"]),
        }
    )


@app.route("/api/commits", methods=["POST"])
def api_commits() -> Response:
    payload = request.get_json(silent=True) or {}
    repo = _resolve_repo(payload)
    branch = gitops.validate_local_branch(repo, payload.get("branch"))
    base = gitops.validate_base(repo, payload.get("base"))
    limit = gitops.parse_limit(payload.get("limit"))

    tip = gitops.branch_tip(repo, branch)
    commits = gitops.get_commits(repo, branch, base=base, limit=limit)
    return jsonify(
        {
            "repo_path": repo,
            "branch": branch,
            "base": base,
            "limit": limit,
            "tip": tip,
            "count": len(commits),
            "commits": [c.to_dict() for c in commits],
            "state": gitops.get_repo_state(repo, branch),
        }
    )


@app.route("/api/commit-stat", methods=["POST"])
def api_commit_stat() -> Response:
    payload = request.get_json(silent=True) or {}
    repo = _resolve_repo(payload)
    return jsonify(gitops.commit_stat(repo, payload.get("sha")))


@app.route("/api/backups", methods=["POST"])
def api_backups() -> Response:
    """List the backup branches this tool has created in the repo."""
    payload = request.get_json(silent=True) or {}
    repo = _resolve_repo(payload)
    return jsonify({"backups": gitops.list_backups(repo)})


@app.route("/api/backup-delete", methods=["POST"])
def api_backup_delete() -> Response:
    """Delete one backup branch. gitops refuses anything outside the namespace."""
    payload = request.get_json(silent=True) or {}
    repo = _resolve_repo(payload)
    ref = (payload.get("ref") or "").strip()
    gitops.delete_backup(repo, ref)
    return jsonify({"deleted": ref, "backups": gitops.list_backups(repo)})


@app.route("/api/commit-diff", methods=["POST"])
def api_commit_diff() -> Response:
    """Full patch for one commit, fetched only when the user opens it."""
    payload = request.get_json(silent=True) or {}
    repo = _resolve_repo(payload)
    return jsonify(gitops.commit_diff(repo, payload.get("sha")))


def _save_args(
    payload: dict[str, Any],
) -> tuple[str, str, dict[str, Any], str | None, int]:
    """Shared validation (and message-wrapping) for /api/save and /api/preview."""
    repo = _resolve_repo(payload)
    branch = gitops.validate_local_branch(repo, payload.get("branch"))
    edits = payload.get("edits") or {}
    if not isinstance(edits, dict) or not edits:
        raise GitError("No edited commit messages were provided.")
    # Auto-wrap each edited message on the way in, so the save and its preview
    # operate on identical text. Non-str values pass through untouched for
    # gitops to reject with its own precise error.
    width = app.config["WRAP_WIDTH"]
    edits = {
        sha: (wrap_commit_message(msg, width) if isinstance(msg, str) else msg)
        for sha, msg in edits.items()
    }
    base = gitops.validate_base(repo, payload.get("base"))
    limit = gitops.parse_limit(payload.get("limit"))
    return repo, branch, edits, base, limit


@app.route("/api/preview", methods=["POST"])
def api_preview() -> Response:
    """Dry-run: report the true blast radius without moving any ref.

    Takes the same ``expected_old_tip`` guard as the save it previews. Without
    it a preview computed after the branch moved would silently describe the
    *current* history rather than the one the user is looking at — and the
    blast radius shown in the confirmation dialog is exactly what they are
    consenting to.
    """
    payload = request.get_json(silent=True) or {}
    repo, branch, edits, base, limit = _save_args(payload)
    expected_old_tip = (payload.get("expected_old_tip") or "").strip()
    if expected_old_tip:
        gitops.validate_sha(expected_old_tip, "expected_old_tip")
    result = gitops.rewrite_messages(
        repo,
        branch,
        edits,
        base=base,
        limit=limit,
        expected_old_tip=expected_old_tip or None,
        dry_run=True,
    )
    return jsonify(result.to_dict())


@app.route("/api/save", methods=["POST"])
def api_save() -> Response:
    payload = request.get_json(silent=True) or {}
    repo, branch, edits, base, limit = _save_args(payload)

    # expected_old_tip is REQUIRED: it is the concurrency guard (CAS), and the
    # UI always sends it. A missing value means a malformed or replayed request.
    expected_old_tip = (payload.get("expected_old_tip") or "").strip()
    if not expected_old_tip:
        raise GitError(
            "expected_old_tip is required to guard against concurrent changes."
        )
    gitops.validate_sha(expected_old_tip, "expected_old_tip")

    result = gitops.rewrite_messages(
        repo,
        branch,
        edits,
        base=base,
        limit=limit,
        expected_old_tip=expected_old_tip,
    )
    return jsonify(result.to_dict())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Git Commit Message Editor")

    from importlib.metadata import version

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('git-commit-editor')}",
    )

    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5050, help="Port to bind to")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--repo",
        default=os.getcwd(),
        help="Default git repository path to prefill in the UI",
    )
    parser.add_argument(
        "--wrap-width",
        type=int,
        default=WRAP_WIDTH,
        help=(
            "Hard-wrap commit-message bodies at this column when saving "
            f"(default {WRAP_WIDTH}; 0 disables wrapping)."
        ),
    )
    args = parser.parse_args()

    # This tool has no authentication and will happily rewrite history in any
    # repository the process can write to. Anyone who can reach the port can
    # read the CSRF token straight out of the page, so a "I know what I'm doing"
    # flag would buy nothing — binding off-loopback is refused outright. Use an
    # SSH tunnel if you genuinely need remote access.
    try:
        bind = ipaddress.ip_address(args.host)
        loopback = bind.is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        parser.error(
            f"refusing to bind {args.host}: this tool is unauthenticated and "
            "rewrites git history. Bind 127.0.0.1 and use an SSH tunnel "
            "(ssh -L) if you need to reach it from another machine."
        )

    app.config["DEFAULT_REPO"] = os.path.abspath(os.path.expanduser(args.repo))
    app.config["WRAP_WIDTH"] = args.wrap_width

    print("Starting Git Commit Message Editor…")
    print(f"Default repository: {app.config['DEFAULT_REPO']}")
    print(f"Wrapping message bodies at {args.wrap_width or 'off'} columns")
    print(f"Open http://{args.host}:{args.port} in your browser")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
