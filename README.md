# Git Commit Message Editor

A small Flask + vanilla-JS web app to **view and edit the commit messages on a
branch** of any local git repository. Point it at a repo, pick a branch, edit
any message inline, and save — it rewrites the affected commits (and replays
their descendants) while preserving authors, dates and file contents.

Built for the common chore of cleaning up messy commit messages (e.g. a string
of `Automatic commit` / `wip` messages) before opening a PR, without wrestling
with interactive rebase.

> **Reword-only.** This tool only changes commit *messages*. It never reorders,
> squashes, splits, or drops commits — and that restriction is exactly what
> keeps `git status` clean after a save (every rewritten commit keeps an
> identical tree). For structural history surgery, use `git rebase -i`.

![Screenshot of the Git Commit Message Editor: the page header reading Commit messages with a theme toggle; a repository path box, branch selector, base and max-commits fields with status chips; a list of commit cards for a feature/wip branch, each showing a short SHA, author, date, badges such as merge, an editable message textarea, a subject-length counter, and Files/Diff toggles; a save bar along the bottom.](https://raw.githubusercontent.com/nmpowell/git-commit-editor/main/screenshot.webp)

## Install and run

The quickest way to try it, with [uv](https://docs.astral.sh/uv/) installed:

```bash
cd path/to/your/repo
uvx git-commit-editor
# then open http://127.0.0.1:5050
```

Run it from inside the repository you want to edit — `--repo` defaults to the
current directory. To install it properly instead of running it ad hoc:

```bash
uv tool install git-commit-editor   # or: pip install git-commit-editor
git-commit-editor --repo /path/to/your/repo
```

`python -m git_commit_editor` also works, and `git-commit-editor --version`
prints the installed version.

| flag | default | meaning |
|------|---------|---------|
| `--host` | `127.0.0.1` | bind address; only loopback names/IPs are accepted, anything else is refused |
| `--port` | `5050` | port |
| `--repo` | cwd | default repository path prefilled in the UI |
| `--wrap-width` | `72` | hard-wrap message *bodies* at this column on save (`0` disables) |
| `--debug` | off | Flask debug mode |
| `--version` | — | print the version and exit |

Tested on Python 3.14. Requires `git` on `PATH`.

## Features

**Editing**

- Any local branch (a symbolic-ref alias for another branch is refused, so
  the branch it points at is never moved by proxy). The base is auto-detected,
  so you see only the commits that belong to the branch, and that stays
  correct after a rebase.
- One editor per commit, sized to its message. Files and the full diff load
  on demand.
- Bodies wrap at 72 columns on save; lists, trailers, code blocks (space- or
  tab-indented) and URLs are left alone. A missing blank line after the
  subject is inserted.
- Live hints: subject count turns amber past 50 and red past 72; a missing
  blank line is flagged. Only an empty message blocks saving.
- Drafts survive a reload, a crash and a second tab. Drafts for commits outside
  the current view are kept in a drawer, not dropped.
- Filter by message, author or hash. The count warns when a filter hides an
  edited commit, because saving applies every edit in the range.

**Safety**

- Authors, committers, dates and trees are preserved. Merges and root commits
  are handled.
- The backup and the branch move are one atomic `update-ref` transaction with a
  compare-and-swap. If the branch moved since you loaded it, nothing happens.
- Backups are real branches (`commit-editor-backup/…`), listed in the UI with
  the date each was taken (from its name), newest first, and a copyable
  restore command; visible to `git branch`.
- The confirmation dialog shows how many commits will be rewritten, including
  untouched descendants, and warns about signed, pushed and checked-out-elsewhere
  commits. It stays disabled if the preview fails.
- Refuses shallow clones, unmerged indexes, replace refs, grafts, and
  in-progress git operations — checked in every linked worktree, not just the
  one you pointed at — and ranges over 1,000 commits.
- Loopback only. Non-loopback binding is refused; a loopback-only Host check,
  a CSRF token and a CSP guard the rest.

**Interface**

- A timeline rail marks newest and oldest; edited commits show violet on it.
- Dark and light themes from one token set, every colour pair at WCAG AA.
- Labelled controls, live regions, a skip link, a visible focus ring, 44px
  touch targets, and `prefers-reduced-motion`. Works at 390px and 1440px.
- `Cmd/Ctrl+S` saves, `Cmd/Ctrl+Enter` confirms.

## Using it

1. **Repository path** — any path inside the repo; it's resolved to the repo
   root automatically.
2. **Branch** — choose from the local branches (the checked-out one is
   preselected).
3. **Base** — by default this is **auto-detected**: the remote's default branch
   via `origin/HEAD`, else a local `main`/`master`/`develop`/`trunk`, else
   `init.defaultBranch`, else those same names again as `origin/<name>`
   remote-tracking refs. It is the *left side of a `base..branch` range*: the
   editor shows commits reachable from your branch but **not** from Base — i.e.
   the commits that belong to this branch. Commits shared with Base are hidden
   and can't be edited. This stays correct even if the branch was rebased onto
   Base. Editing the main branch itself, or clearing this field, falls back to
   the most recent N commits. Switching branches re-suggests it the same way,
   unless you typed one yourself.
4. **Max commits** — the cap used only when no Base is set (default 30, hard
   max 1000). A `base..branch` range larger than the cap is **refused**, not
   silently truncated: saving rewrites the whole range, so showing you part of
   it would mean consenting to one thing and getting another.
   These controls stay visible; the repo's state (branch count, checked-out
   branch, working tree clean or dirty) sits with them as status chips.
5. **Filter** — narrow the list by message, author or hash, or tick **Edited
   only** to see just what you've changed. Purely client-side over the loaded
   commits; it hides cards and edits nothing. Because saving applies *every*
   edit in the loaded range rather than only the visible ones, the count says so
   explicitly: `3 of 9 · 1 edited commit hidden by this filter`.
6. Each commit shows its short SHA, author, relative date and badges (`merge`,
   `root`, `signed`, `pushed`, `on a remote`, `edited`), with its message
   editor **open by default** and sized to the message rather than to a fixed
   number of rows — a one-word `wip` gets one line, not six. **Hide message**
   collapses one to its subject alone, **Collapse all** collapses the lot, and
   a collapsed commit stays collapsed as you filter and reload. A commit you
   have edited always opens, and its collapsed summary shows the message you
   gave it rather than the committed one.

   **Files** and **Diff** load that commit's changed-file summary and full
   patch, lazily and independently, so a 30-commit range costs one request
   rather than thirty. Open panels stay open across a reload of the list.

   The editor is one textarea holding the whole message, deliberately — a split
   subject/body pair breaks pasting a complete message, and undo across the
   boundary. The subject is instead shown as the card's heading, with a live
   character count that turns amber past 50 characters (the convention) and
   red past 72, where GitHub truncates it (git itself never truncates a
   subject). Both are advice, not blocks — you can still save. A missing
   blank line after the subject is flagged too, and inserted on save.
7. **Save changes** (or `Cmd/Ctrl+S`) → a confirmation dialog shows the true
   **blast radius** (how many commits get rewritten, including unchanged
   descendants) and any signed/pushed/worktree warnings → confirm
   (`Cmd/Ctrl+Enter`). A toast (auto-dismissed after 20 s) shows the old→new
   tip, the backup ref, and a click-to-copy undo command. The same details are
   stacked, newest first, in a **Save log** at the bottom of the page, so
   nothing is lost when the toast goes. Entries stay there until you press the
   log's **Clear** button or reload the page. Save errors stay on screen until
   dismissed and are logged too.

**Colour theme** — the button top right cycles System / Dark / Light and
remembers your choice; System follows the operating system's light/dark
setting. Both themes are one set of custom properties, and every
foreground/background pair in each meets WCAG AA (text ≥ 4.5:1, non-text
≥ 3:1).

**Accessibility** — every editor's accessible name is `Message for commit
<short sha>`; each card is a named group; disclosures carry `aria-expanded`/
`aria-controls`; filter, draft-save and lazy-load events announce through
dedicated live regions (polite, errors assertive) — the toast itself isn't
one, so nothing doubles up; dates carry `<time datetime>`; there's a skip
link, a 3px focus ring, 44px coarse-pointer targets (buttons, icon buttons,
toast close, inputs, selects), and `prefers-reduced-motion`.

If the preview *fails*, Confirm stays disabled — and stays enabled only for
the exact edits that preview covered, disabling again if you keep typing. The
blast radius is exactly what you're asked to consent to, so you're never
offered a rewrite neither of us can describe.

### Unsaved drafts survive a reload

Typed-but-unsaved messages are kept in browser `localStorage`, so a reload, a
crash or a closed tab doesn't lose your work. The rules are deliberately simple:

- Drafts are scoped to **repository + branch** and indexed by **commit SHA**.
  Base and Max commits are *view* settings — narrowing the view hides drafts, it
  never destroys them.
- A draft is restored whenever its commit is in the loaded range, **even if the
  branch tip has moved**. Appending a commit doesn't invalidate text attached to
  an untouched SHA. (Saving still carries its own compare-and-swap: a draft
  being attached is not consent to the rewrite.)
- Drafts whose commit *isn't* in the current view appear in a **drafts outside
  this view** drawer rather than being discarded — often the cause is just a
  narrow Max commits, which is recoverable by raising it.
- A successful save clears a submitted draft only if its stored text still
  matches what was submitted — a newer draft typed in another tab survives. A
  *failed* save keeps everything: the rewrite may have succeeded with only the
  response lost.
- The **raw** text you typed is stored, not a normalised form — one storage
  key per commit, so two tabs editing different commits never collide — so a
  restored draft is exactly what you wrote.
- Switching branch or repository asks for confirmation first if any draft
  failed to persist.

The save bar shows `N drafts · saved in this browser`. If storage is unavailable
(private window, disabled site data, quota) it says `NOT saved — this tab only`
instead and the leave-page warning switches back on. Drafts live in one browser
profile and one origin: `localhost:5050` and `127.0.0.1:5050` are different
stores.

### Message wrapping

On save, each **edited** message's body is hard-wrapped to `--wrap-width`
columns (default 72 — git's "50/72" convention; `0` disables). The rules mirror
how `git log` expects a message to read:

- The **subject** (first line) is never wrapped.
- **Blank lines** delimit paragraphs and are kept.
- Within a paragraph, lines are **re-flowed**: joined and greedily re-wrapped,
  so a break placed earlier than the column limit (from a manual edit or a
  paste) is removed and the paragraph is re-wrapped cleanly.
- **Lists** (`- `/`* `/`1. `, numbers of up to two digits — a year such as
  `2024.` opening a sentence is prose) wrap with a hanging indent; a
  continuation line more indented than the marker, or tab-indented, is a
  nested code block and is left verbatim. A single list item directly under
  an intro line ending in `:` is a list item, not prose.
- **Trailers** (`Signed-off-by:`, `Co-authored-by:` …) are left intact — never
  joined or split mid-token. A `Key: value` line counts as one when it sits
  next to another, but only in the **final paragraph** (the one place git
  itself reads trailers from); well-known trailer keys are recognised anywhere.
- **Indented/code blocks** (four or more spaces, or a tab), **URLs** and
  **hyphenated words** are also left intact.
- **Leading blank lines are stripped** first, as `git commit` itself does, so
  the subject is always the first non-blank line — and one is **inserted
  after the subject if the author didn't leave one**, even with wrapping
  disabled (`--wrap-width 0`): git treats everything up to the first blank
  line as the subject, so without it the body folds straight into it.

Only edited messages are wrapped — an untouched commit's text carries over
unchanged (bar trailing blank or whitespace-only lines, which are stripped),
even though a changed ancestor still rebuilds its hash via the descendant
replay. Wrapping runs server-side, so you see the result once the save reloads
the list.

## How editing works (and why it's safe)

Changing a commit message changes that commit's hash, so every **descendant**
commit must be rebuilt too — *including descendants whose own message you didn't
touch* (their hash changes because a parent's hash changed). The backend does
this without `rebase` and without touching your working tree:

- Commits are rebuilt oldest-first with `git commit-tree`, remapping each
  commit's parents through an old→new SHA map. Merge commits keep all parents.
- Only **messages** change, so every rebuilt commit reuses the original
  **tree** object. The branch tip moves to a commit with an identical tree, so
  a checked-out branch's working tree and index stay consistent (`git status`
  stays clean) — even with a dirty index, staged changes are preserved.
- **Author** and **committer** identity and dates are always preserved: the
  contract is "only the message changed", never "rebased just now".
- Only commits in the range you were shown can be edited (full-length ids
  only, never an abbreviation), and anything older is a fixed boundary that's
  never rewritten. A request with no repository path is refused outright —
  `--repo` only prefills the page, never a silent fallback.

### Safety / undo

Every save:

- **Refuses** if the repo is in a state where a rewrite would silently produce
  wrong history:
  - an **in-progress operation** (mid-rebase/merge/cherry-pick/revert/bisect)
    or an **unmerged index** (a conflict in progress even if no marker
    survived) — checked in **every linked worktree** of the repository, not
    just the one you pointed the tool at;
  - a **shallow clone** — at the shallow boundary git reports no parent, so
    replaying that commit would write it as a genuine root and orphan
    everything beneath it;
  - **replacement refs** (`refs/replace/*`) or a **grafts file**, either of
    which rewrites parentage and trees behind git's back. (Every git call also
    passes `--no-replace-objects`; note that flag does *not* disable grafts,
    which is why they are checked separately.)
- Performs the backup-ref creation **and** the branch move in a single atomic
  `git update-ref --stdin` transaction with a compare-and-swap: if the branch
  moved since you loaded it, the whole thing aborts and no backup ref is left
  behind.
- Writes a **timestamped backup branch**
  `commit-editor-backup/<branch>-<unixtime>-<oldtip>[-<n>]` plus a reflog entry.
  These are real branches under `refs/heads/`, so `git branch`, tab-completion
  and any git GUI show them — a recovery point nobody can find is not a
  recovery point. They are filtered back out of this tool's own branch picker.
  Backups accumulate (a small recovery stack): an undo followed by a resave
  within the same second gets a `-2`/`-3` suffix rather than colliding.

The page has a **Backups** section listing every backup in the repo with the
date it was taken (from its name), newest first, the tip it points at, a
copyable restore command and a delete button.
Delete refuses a backup that is checked out anywhere, and — since it deletes
the branch itself rather than following it — never deletes a symbolic ref's
target by mistake. (Restoring is a copyable command rather than a one-click
button: moving a branch back is itself history surgery and deserves a
deliberate paste, not a stray click next to "delete".)

To undo a save, copy the command from the toast, the save log or the Backups
list, or run one of these yourself. The copied command names the repository
explicitly (`git -C <repo>`), so it's safe to paste from any directory even if
your shell is sitting in another repo:

```bash
# If the branch is NOT checked out (safe regardless of working-tree state):
git -C /path/to/repo update-ref refs/heads/<branch> <old-tip-shown-in-save-log>
#   or restore straight from the backup branch:
git -C /path/to/repo update-ref refs/heads/<branch> refs/heads/commit-editor-backup/<backup-name>

# If the branch IS checked out (requires a clean working tree):
git reset --hard <old-tip>
#   the reflog snippet `git reset --hard <branch>@{1}` is only safe when the
#   working tree is clean and that branch is currently checked out.
```

List / clean up backups from the shell when you're happy (or use the delete
buttons in the Backups section):

```bash
git branch --list 'commit-editor-backup/*'
git branch --list 'commit-editor-backup/*' | xargs -r git branch -D
```

### Caveats

- **Rewriting shared history**: if the commits were already pushed/shared,
  rewriting them means a force-push (`git push --force-with-lease`) and
  collaborators reconciling. Two *different* facts are reported separately:
  `pushed` means reachable from **this branch's own upstream** (that is what
  actually implies a force-push), while `on a remote` means reachable from some
  other remote-tracking ref — shared somewhere, but not on this branch's
  upstream. Absence of a badge is not proof a commit is private: remote-tracking
  refs can be stale or incomplete, and the tool never fetches behind your back.
  Prefer this tool on un-pushed local work.
- **Signatures are dropped** — rewritten commits are not re-signed. A commit is
  flagged `signed` by reading its raw object header for `gpgsig`/
  `gpgsig-sha256` directly, so both GPG- and SSH-signed commits are caught with
  no verification setup (keyring, allowed-signers file) needed; flagged
  commits get a modal warning before you rewrite them.
- **`git worktree`**: moving a branch checked out in *another* linked worktree
  leaves that worktree's HEAD on the rewritten tip (its files untouched), which
  can show as spurious `git status` output there. The UI detects and warns.
- **Message cleanup is intentionally bypassed** at the git layer: unlike
  `git commit`, `commit-tree` stores the message verbatim (no comment-stripping).
  The app layer does apply body wrapping on save (see **Message wrapping**);
  empty/whitespace-only messages are rejected, and trailing lines that are
  blank or whitespace-only are stripped (the stored object ends in a single
  newline, as `commit-tree` writes it).
- **No authentication; loopback only.** Instead of an "I know what I'm doing"
  flag, this tool refuses to bind anywhere but loopback and checks the Host
  header the same way (by name — `localhost`/`127.0.0.1`/`::1` — not by
  resolving it, with no allowlist config for anything else) on every route
  including `/`; use an SSH tunnel (`ssh -L`) for remote access. Also guarded:
  a per-process **CSRF token** on every API POST (not the primary defence —
  anyone who can reach the port could read it out of the page too), required
  `Content-Type: application/json`, rejection of cross-origin/`null` origins,
  a 5 MB request cap, and a `Content-Security-Policy` (`script-src 'self'`, no
  `'unsafe-inline'` — config reaches the page as a JSON block, not inline
  script) plus `X-Content-Type-Options`/`Referrer-Policy`/`Cache-Control`.
- **Repository-routing environment variables are stripped** from every git call
  (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, object-dir
  and namespace overrides). `-C <repo>` does *not* neutralise them, so a stray
  `GIT_DIR` in your shell would otherwise silently redirect every command at a
  different repository.
- **Commit message encoding**: reads are pinned to `i18n.logOutputEncoding=UTF-8`
  and writes to `i18n.commitEncoding=UTF-8`, so a legacy setting in the user's
  git config can neither scramble what you see nor mislabel what is stored.
  Without the latter, git silently "repairs" bytes it cannot read as UTF-8 by
  reinterpreting them as Latin-1.

## Development

    git clone https://github.com/nmpowell/git-commit-editor
    cd git-commit-editor
    uv sync --locked
    uv run playwright install chromium   # once; add --with-deps on Linux
    uv run pytest                        # backend + Playwright end-to-end, ~60 s
    uv run mypy
    uv run ruff check .
    uv run ruff format --check .

For the fast loop, skip the browser tests: `uv run pytest --ignore=tests/test_ui.py` (about 20 s).

Layout:

```
src/git_commit_editor/
  app.py            Flask routes, request guards, on-save body wrapping, the CLI (`main`)
  gitops.py         all git logic: listing, parsing, validators, rewrite engine
  templates/        index.html
  static/           style.css, app.js
tests/
  conftest.py       shared repo/commit fixtures for test_gitops.py and test_app.py
  test_gitops.py    gitops tests: rewrite engine, validators, safety guards
  test_app.py       wrapping tests + Flask request-guard tests
  test_ui.py        Playwright end-to-end browser tests, self-contained
shots.yml           regenerates screenshot.webp (see below)
```

The screenshot is generated, not taken by hand, so it stays at one window size:

    shot-scraper multi shots.yml

This builds a throwaway demo repository at `/tmp/git-commit-editor-demo` (the same fixture the UI tests use), starts the app against it on port 8345, and writes `screenshot.webp`. Needs [shot-scraper](https://shot-scraper.datasette.io/) 1.12 or later installed separately (`uv tool install shot-scraper && shot-scraper install`; earlier versions write JPEG data whatever the file extension).

Releases: bump `version` in `pyproject.toml` (`uv version --bump patch`), commit as `Release <version>`, push, wait for the Test workflow, then publish a GitHub Release whose tag equals the version. The Publish workflow runs the tests again and uploads to PyPI through Trusted Publishing.

`tests/test_gitops.py` (using the repo/commit helpers in `tests/conftest.py`) builds
throwaway repos and checks: linear / tip-only / root / compound edits,
multi-line + `\x1f` + unicode round-trips, no-ops, author and committer
identity/date preservation, merge replay, dirty-index rewrite, the atomic
transaction (failed CAS leaves no backup; a symbolic ref is never
dereferenced), dry-run previews, and the input validators (ref / SHA / branch
/ base, including abbreviated-id and symbolic-branch rejection). It also
covers every guard in **Safety / undo** above — including its reach into every
linked worktree (even one whose path contains a newline), the same-second
backup-suffix disambiguation and name-derived ordering, `delete_backup`'s
refusals, and both GPG- and SSH-signed commits being flagged from the raw
signature header — plus per-file statuses (`A`/`D`/`R` with rename
old-paths), a tab in a filename not truncating the path, truncated `-z`
records being refused, and `commit_diff`'s truncation and CRLF fidelity.

`tests/test_app.py` checks the wrapper — subject never wrapped, paragraph re-flow,
blank-line handling (preservation, insertion, and leading blanks stripped),
idempotency, and lists/trailers/code blocks/URLs/hyphenated words left intact
(including the intro-colon list-item, tab-indented code, two-digit marker and
final-paragraph trailer rules above)
— plus end-to-end wrapping through `/api/save`. It also covers the request
guards: CSRF token, content-type, the loopback-only Host check (including on
`/`), cross-origin/`null` origins, a request with no repository path, the
security headers, and the wrap width reaching the page.

`tests/test_ui.py` drives the real app with headless Playwright/Chromium against a
deterministic fixture repo, covering: loading a branch with base detection;
save → backup → reload; a stale-tip save refused with the typed text kept; a
failed preview leaving Confirm disabled; drafts surviving a reload and two
tabs without a save clobbering a newer one; the subject/blank-line hints; an
empty message blocking save; correct first-load sizing; the filter; lazy
Files/Diff; keyboard shortcuts; resetting a message or discarding all edits;
and the orphan-drafts drawer.

## Licence

Apache 2.0 — see [LICENSE](https://github.com/nmpowell/git-commit-editor/blob/main/LICENSE).
