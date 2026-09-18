"""End-to-end UI tests for the git commit message editor.

Drives the real Flask app (``python -m git_commit_editor``) with headless Playwright/Chromium
against a deterministic, throwaway git fixture repository built fresh for
every test. The tests find things the way a user does — by role and label —
plus a few ids the app exposes (``#modal-confirm``, ``#draft-status``,
``#orphan-drafts``, ``#banner``).

Self-contained: this file owns its own server, fixture-repo builder and
browser fixtures. It does not use ``conftest.py``.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, Route, expect, sync_playwright

# ---------------------------------------------------------------------------
# Fixture repository
#
# Fixed author/committer identity and dates, so commit SHAs (and therefore
# card order and content) are the same on every run.
# ---------------------------------------------------------------------------

FIXTURE_BASE_EPOCH = 1767607200  # 2026-01-05T10:00:00Z; dates advance 1h/commit
AUTHOR_NAME = "Ada Fixture"
AUTHOR_EMAIL = "ada@example.invalid"

# Card order once loaded (newest first):
IDX_TEST_LOADER = 0  # "Add a test for the config loader"
IDX_DOC_TESTS = 1  # "Document how to run the tests"
IDX_MERGE = 2  # "Merge branch 'topic/side' into feature/wip"
IDX_FIRST_TEST = 3  # "Add a first test" (from topic/side)
IDX_GITIGNORE = 4  # "Automatic commit" (.gitignore)
IDX_RENAME = 5  # "Rename app.py to main.py" -> R100 in Files
IDX_LONG_SUBJECT = 6  # 105-char subject
IDX_CONFIG_LOADER = 7  # "Add a config loader" (multi-paragraph, co-author trailer)
IDX_WIP = 8  # "wip"
IDX_FIRST_AUTOMATIC = 9  # "Automatic commit" (src/app.py, oldest on the branch)


def _git_base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME=AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=AUTHOR_EMAIL,
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_MERGE_AUTOEDIT="no",
    )
    return env


def _git(repo: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    """Run one git command against ``repo`` with a fixed, sandboxed identity."""
    env = {**_git_base_env(), **(env_extra or {})}
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class _RepoBuilder:
    """Builds the fixture repo commit by commit with fixed, advancing dates."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tick = 0
        self.env = _git_base_env()

    def _stamp(self) -> str:
        return f"{FIXTURE_BASE_EPOCH + self.tick * 3600} +0000"

    def run(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def write(self, relpath: str, content: str) -> None:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def append(self, relpath: str, content: str) -> None:
        with open(self.path / relpath, "a") as fh:
            fh.write(content)

    def commit(self, message: str) -> None:
        stamp = self._stamp()
        env = {**self.env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        subprocess.run(
            [
                "git",
                "-C",
                str(self.path),
                "commit",
                "-q",
                "--no-gpg-sign",
                "-m",
                message,
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.tick += 1

    def merge_no_ff(self, branch: str, message: str) -> None:
        stamp = self._stamp()
        env = {**self.env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        subprocess.run(
            [
                "git",
                "-C",
                str(self.path),
                "merge",
                "-q",
                "--no-ff",
                "--no-gpg-sign",
                "-m",
                message,
                branch,
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        self.tick += 1


def build_fixture_repo(target: Path) -> None:
    """Build the deterministic fixture repo used by every test in this file.

    ``main`` gets 3 commits; ``feature/wip`` gets ~10 of its own, including two
    "Automatic commit", a "wip", a multi-paragraph message with a bullet list
    and a Co-authored-by trailer, a 105-char subject, a rename, and a --no-ff
    merge. Fixed identity and dates throughout, so SHAs never change between
    runs.
    """
    target.mkdir(parents=True, exist_ok=True)
    r = _RepoBuilder(target)
    subprocess.run(
        ["git", "-C", str(target), "init", "-q", "-b", "main"],
        env=r.env,
        check=True,
        capture_output=True,
        text=True,
    )
    r.run("config", "commit.gpgsign", "false")
    r.run("config", "core.hooksPath", "/dev/null")

    # ---- main: 3 commits ----------------------------------------------------
    r.write("README.md", "# Fixture project\n\nA small project used for screenshots.\n")
    r.run("add", "README.md")
    r.commit("Initial commit")

    r.write("build.sh", "#!/bin/sh\necho build\n")
    r.run("add", "build.sh")
    r.commit("Add build script")

    r.append("README.md", "\n## Build\n\nRun ./build.sh\n")
    r.run("add", "README.md")
    r.commit("Document the build step")

    # ---- feature/wip ---------------------------------------------------------
    r.run("checkout", "-q", "-b", "feature/wip")

    r.write("src/app.py", 'def main():\n    print("hello")\n')
    r.run("add", "src/app.py")
    r.commit("Automatic commit")

    r.write(
        "src/app.py",
        'def main():\n    print("hello, world")\n\n\nif __name__ == "__main__":\n'
        "    main()\n",
    )
    r.run("add", "src/app.py")
    r.commit("wip")

    r.write(
        "src/config.py",
        "import os\n\n\ndef load_config(path):\n    with open(path) as fh:\n"
        "        return fh.read()\n",
    )
    r.run("add", "src/config.py")
    r.commit(
        "Add a config loader\n\n"
        "The loader reads a plain-text file and returns its contents. It does not\n"
        "parse anything yet; that comes in a later change once the format is agreed.\n\n"
        "- reads the file the caller names\n"
        "- returns the raw text\n"
        "- raises the usual OSError on a missing file\n\n"
        "Co-authored-by: Pat Example <pat@example.invalid>"
    )

    r.write(
        "src/config.py",
        "import os\n\n\ndef load_config(path):\n    with open(path) as fh:\n"
        "        text = fh.read()\n    return os.path.expandvars(text)\n",
    )
    r.run("add", "src/config.py")
    r.commit(
        "Refactor the configuration loading path so that environment overrides "
        "are applied after the file defaults"
    )

    r.run("mv", "src/app.py", "src/main.py")
    r.commit("Rename app.py to main.py")

    r.write(".gitignore", "coverage\n*.pyc\n")
    r.run("add", ".gitignore")
    r.commit("Automatic commit")

    # A side branch merged back with --no-ff so the range contains a merge.
    r.run("checkout", "-q", "-b", "topic/side")
    r.write("src/test_main.py", "def test_main():\n    assert True\n")
    r.run("add", "src/test_main.py")
    r.commit("Add a first test")
    r.run("checkout", "-q", "feature/wip")
    r.merge_no_ff("topic/side", "Merge branch 'topic/side' into feature/wip")
    r.run("branch", "-q", "-D", "topic/side")

    r.append("README.md", "\n## Tests\n\nRun pytest.\n")
    r.run("add", "README.md")
    r.commit("Document how to run the tests")

    r.write(
        "src/test_main.py",
        "def test_main():\n    assert True\n\n\ndef test_config_loader(tmp_path):\n"
        "    assert True\n",
    )
    r.run("add", "src/test_main.py")
    r.commit("Add a test for the config loader")


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(
    proc: subprocess.Popen, base_url: str, timeout_s: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(
                f"server process exited early (code {proc.returncode}):\n{output}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"server never became ready at {base_url}: {last_error}")


@pytest.fixture(scope="session")
def server():
    """Start the app once for the whole file; each test types its own repo path.

    Launched with no ``--repo``, and with its working directory set to a bare
    temp directory (not this checkout) so the server has no default repo to
    auto-load — every test loads its own fixture repo explicitly through the
    UI, as the brief requires.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="gce-server-cwd-") as cwd:
        proc = subprocess.Popen(
            [sys.executable, "-m", "git_commit_editor", "--port", str(port)],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(proc, base_url)
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "fixture-repo"
    build_fixture_repo(target)
    return target


# ---------------------------------------------------------------------------
# Browser fixtures — deliberately local to this file (self-contained: no
# conftest.py dependency).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _playwright():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_playwright):
    b = _playwright.chromium.launch(headless=True)
    try:
        yield b
    finally:
        b.close()


@pytest.fixture()
def context(browser):
    ctx = browser.new_context()
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture()
def page(context):
    pg = context.new_page()
    try:
        yield pg
    finally:
        pg.close()


# ---------------------------------------------------------------------------
# Small helpers shared by the tests
# ---------------------------------------------------------------------------


def open_app(page: Page, base_url: str) -> None:
    page.goto(base_url)


def load_repo(page: Page, repo_path: Path) -> None:
    """Fill the repository path, click Load repo, and wait for the first card.

    Waits for the button to be enabled first: defends against the (harmless)
    case where a stray auto-load from a prefilled default repo is still in
    flight, which would otherwise make a same-tick click on "Load repo" a
    no-op (the app guards against re-entrant loads).
    """
    load_btn = page.get_by_role("button", name="Load repo")
    expect(load_btn).to_be_enabled()
    page.get_by_label("Repository path").fill(str(repo_path))
    load_btn.click()
    expect(page.locator("#commit-list li.commit").first).to_be_visible()


def commit_card(page: Page, index: int) -> Locator:
    return page.get_by_role("group", name=re.compile(r"^Commit [0-9a-f]{9} by ")).nth(
        index
    )


def message_box(card: Locator) -> Locator:
    return card.get_by_role("textbox", name=re.compile(r"^Message"))


def held_route(page: Page, routes: list[Route], n: int = 1) -> Route:
    """The n-th route a ``page.route`` handler appended to ``routes``.

    Handlers run only while some Playwright call is in progress, so a plain
    Python poll would never see them. Once ``expect_request`` has seen the
    request, one more round trip is enough: the route message was sent
    alongside it and the connection is ordered.
    """
    page.evaluate("0")
    assert len(routes) >= n, f"expected {n} held route(s), have {len(routes)}"
    return routes[n - 1]


# Counts /api/preview responses the app has finished READING. The app's handler
# runs in the microtasks queued behind res.json(), which drain before the next
# task — so once a poll from the test sees the count go up, the handler has
# run, whatever it did or (correctly) declined to do. A response event alone
# fires on headers and would let an assertion race the handler.
PREVIEW_PROBE_JS = """() => {
    window.__previewsRead = 0;
    const realFetch = window.fetch;
    window.fetch = async (...args) => {
        const res = await realFetch(...args);
        if (String(args[0]).includes("/api/preview")) {
            const realJson = res.json.bind(res);
            res.json = async () => {
                try {
                    return await realJson();
                } finally {
                    window.__previewsRead += 1;
                }
            };
        }
        return res;
    };
}"""


def install_preview_probe(page: Page) -> None:
    page.evaluate(PREVIEW_PROBE_JS)


def release_preview(page: Page, route: Route) -> None:
    """Let a held /api/preview through and return once the app has handled it."""
    before = page.evaluate("window.__previewsRead")
    with page.expect_response("**/api/preview"):
        route.continue_()
    page.wait_for_function("n => window.__previewsRead === n", arg=before + 1)


# Every custom property declared on :root, as computed right now.
THEME_TOKENS_JS = """() => {
    const names = new Set();
    for (const sheet of document.styleSheets)
        for (const rule of sheet.cssRules)
            if (rule.selectorText === ":root")
                for (const prop of rule.style) if (prop.startsWith("--")) names.add(prop);
    const cs = getComputedStyle(document.documentElement);
    return Object.fromEntries([...names].map((n) => [n, cs.getPropertyValue(n).trim()]));
}"""

# How many line boxes the last 40 characters of an element's text occupy.
SHA_LINE_COUNT_JS = """(el) => {
    const node = el.firstChild;
    const range = document.createRange();
    range.setStart(node, node.textContent.length - 40);
    range.setEnd(node, node.textContent.length);
    return range.getClientRects().length;
}"""


# ---------------------------------------------------------------------------
# 1. Load shows only the branch's own commits, base auto-detected
# ---------------------------------------------------------------------------


def test_load_shows_only_branch_commits_with_base_detected(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    groups = page.get_by_role("group", name=re.compile(r"^Commit [0-9a-f]{9} by "))
    expect(groups).to_have_count(10)

    expect(page.locator("#list-info")).to_contain_text("not in")
    expect(page.locator("#list-info")).to_contain_text("main")

    base_field = page.get_by_label(re.compile(r"^Base"))
    expect(base_field).to_have_value("main")

    expect(page.locator(".tl-newest")).to_contain_text("newest")
    expect(page.locator("#tl-oldest-label")).to_contain_text("branch start (main)")


# ---------------------------------------------------------------------------
# 2. Save rewrites messages, creates a backup, and reloads
# ---------------------------------------------------------------------------


def test_save_rewrites_messages_creates_backup_and_reloads(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    box = message_box(card)
    expect(box).to_have_value("wip")

    new_message = (
        "Tidy up the main entry point and make the greeting configurable via "
        "the environment\n"
        "This second line should have a blank line above it but does not."
    )
    box.fill(new_message)

    page.get_by_role("button", name="Save changes").click()
    confirm = page.locator("#modal-confirm")
    expect(confirm).to_be_enabled()
    with page.expect_request("**/api/save") as save_request:
        confirm.click()

    # What is sent is the whole request the dialog described, not just edits.
    sent = json.loads(save_request.value.post_data)
    assert set(sent) == {"repo", "branch", "base", "limit", "expected_old_tip", "edits"}
    assert sent["branch"] == "feature/wip"
    assert sent["base"] == "main"
    assert re.fullmatch(r"[0-9a-f]{40}", sent["expected_old_tip"])
    assert list(sent["edits"].values()) == [new_message]

    toast = page.locator("#toast")
    expect(toast).to_contain_text("Messages rewritten")
    expect(page.locator("#edit-count")).to_have_text("No edits")
    expect(page.locator("#save-log-list li")).to_have_count(1)
    expect(page.locator("#backups-list li")).to_have_count(1)

    # The undo command carries a 40-character SHA. Where it fits on a line it
    # stays whole rather than being broken mid-hash: in the toast at desktop
    # width and in the save log even at phone width — where the page must not
    # scroll sideways and the toast's command must stay inside the toast.
    toast_command = toast.locator(".copy-row code").first
    log_command = page.locator("#save-log-list .copy-row code").first
    assert toast_command.evaluate(SHA_LINE_COUNT_JS) == 1
    page.set_viewport_size({"width": 390, "height": 844})
    assert (
        page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        == 0
    )
    assert log_command.evaluate(SHA_LINE_COUNT_JS) == 1
    assert page.evaluate(
        "document.querySelector('#toast .copy-row code').getBoundingClientRect().right"
        " <= document.querySelector('#toast').getBoundingClientRect().right"
    )
    page.set_viewport_size({"width": 1280, "height": 720})

    # "wip" is an ancestor, not the tip: rewriting it replays every descendant
    # with a new hash but the SAME message, so the tip's own subject is
    # unaffected. Find the rewritten commit by its (unchanged) topological
    # position in the range, then read ITS message.
    shas = _git(repo, "log", "--topo-order", "--format=%H", "main..feature/wip").split()
    edited_sha = shas[IDX_WIP]
    rewritten = _git(repo, "log", "--format=%B", "-1", edited_sha)
    expected = (
        "Tidy up the main entry point and make the greeting configurable via "
        "the environment\n"
        "\n"
        "This second line should have a blank line above it but does not."
    )
    assert rewritten.rstrip("\n") == expected
    # The tip's own message is untouched (only its hash changed, because an
    # ancestor's hash changed).
    tip_message = _git(repo, "log", "--format=%B", "-1", "feature/wip")
    assert tip_message.rstrip("\n") == "Add a test for the config loader"

    backup_names = (
        _git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/commit-editor-backup/",
        )
        .strip()
        .splitlines()
    )
    assert len(backup_names) == 1
    assert re.match(
        r"^commit-editor-backup/feature/wip-\d+-[0-9a-f]{9}$", backup_names[0]
    )


# ---------------------------------------------------------------------------
# 3. A save is refused when the branch moved since it was loaded
# ---------------------------------------------------------------------------


def test_stale_tip_save_is_refused_and_text_kept(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    box = message_box(card)
    edited_text = "A local edit that must survive a refused save"
    box.fill(edited_text)

    page.get_by_role("button", name="Save changes").click()
    confirm = page.locator("#modal-confirm")
    # Preview succeeds here: the branch has not moved yet.
    expect(confirm).to_be_enabled()

    # Move the branch behind the page's back.
    _git(
        repo,
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "-m",
        "Sneaky concurrent commit",
    )

    confirm.click()

    expect(page.locator("#toast")).to_contain_text("Save failed")
    expect(box).to_have_value(edited_text)

    backup_names = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/commit-editor-backup/",
    ).strip()
    assert backup_names == ""


# ---------------------------------------------------------------------------
# 4. A stale-tip preview keeps the confirm button disabled
# ---------------------------------------------------------------------------


def test_preview_failure_keeps_confirm_disabled(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    message_box(card).fill("An edit whose preview is computed against a moved branch")

    # Move the branch BEFORE opening the modal, so the preview itself fails.
    _git(
        repo,
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "-m",
        "Sneaky concurrent commit",
    )

    page.get_by_role("button", name="Save changes").click()
    dialog = page.get_by_role("dialog", name="Rewrite commit messages?")
    expect(dialog.locator("#modal-impact")).to_contain_text("Could not compute impact")

    confirm = page.locator("#modal-confirm")
    expect(confirm).to_have_text("Cannot rewrite")
    expect(confirm).to_be_disabled()


# ---------------------------------------------------------------------------
# 5. A draft survives a reload and is restored
# ---------------------------------------------------------------------------


def test_draft_survives_reload_and_is_restored(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    draft_text = "Give this commit a clearer subject before it lands"
    message_box(card).fill(draft_text)
    expect(page.locator("#draft-status")).to_contain_text("1 draft")

    page.reload()
    load_repo(page, repo)

    expect(page.locator("#toast")).to_contain_text("Drafts restored")
    expect(page.locator("#draft-status")).to_have_text(
        "1 draft · saved in this browser"
    )

    restored_card = commit_card(page, IDX_WIP)
    expect(message_box(restored_card)).to_have_value(draft_text)
    expect(restored_card.locator(".commit-tag.edited")).to_be_visible()


# ---------------------------------------------------------------------------
# 6. Subject-length and blank-line-2 hints
# ---------------------------------------------------------------------------


def test_subject_hints_amber_over_50_red_over_72_and_blank_line_2(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    box = message_box(card)
    hint = card.locator(".commit-hint")

    subject_51 = "Rework the loader so a missing config file produces"
    assert len(subject_51) == 51
    box.fill(subject_51)
    expect(card.locator(".subject-count.over")).to_have_count(1)
    expect(card.locator(".subject-count.err")).to_have_count(0)
    expect(hint).to_contain_text("51 characters")
    expect(hint).to_contain_text("50 is the convention")
    expect(box).to_have_attribute("aria-invalid", "false")

    subject_73 = (
        "Rework the loader so a missing configuration file produces a much clearer"
    )
    assert len(subject_73) == 73
    box.fill(subject_73)
    expect(card.locator(".subject-count.over")).to_have_count(1)
    expect(card.locator(".subject-count.err")).to_have_count(1)
    expect(hint).to_contain_text("73 characters")
    expect(hint).to_contain_text(re.compile(r"truncates? past 72"))
    expect(box).to_have_attribute("aria-invalid", "false")

    box.fill("Fix the loader startup message\nThis line should not be here yet.")
    expect(card.locator(".subject-count.over")).to_have_count(0)
    expect(card.locator(".subject-count.err")).to_have_count(0)
    expect(hint).to_contain_text("Line 2 should be blank")
    expect(box).to_have_attribute("aria-invalid", "false")


# ---------------------------------------------------------------------------
# 7. An empty message blocks save with a specific reason
# ---------------------------------------------------------------------------


def test_empty_message_blocks_save_with_reason(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    message_box(card).fill("")

    save_btn = page.get_by_role("button", name="Can't save — 1 empty")
    expect(save_btn).to_be_disabled()
    expect(message_box(card)).to_have_attribute("aria-invalid", "true")


# ---------------------------------------------------------------------------
# 8. Multi-line message is fully visible on first load
# ---------------------------------------------------------------------------


def test_multiline_message_is_fully_visible_on_first_load(page, server, repo):
    """A textarea is sized to its content on first load, not to the minimum
    height it reports while still detached."""
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_CONFIG_LOADER)
    box = message_box(card)
    heights = box.evaluate("el => ({client: el.clientHeight, scroll: el.scrollHeight})")

    assert heights["client"] >= heights["scroll"], (
        f"textarea clipped on first load: clientHeight={heights['client']} < "
        f"scrollHeight={heights['scroll']}"
    )


# ---------------------------------------------------------------------------
# 9. Edited-only filter, and the hidden-edit warning
# ---------------------------------------------------------------------------


def test_edited_only_filter_and_hidden_edit_warning(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    message_box(card).fill("A change that a filter will hide")

    page.get_by_label("Edited only").check()
    expect(page.locator("#filter-count")).to_contain_text("1 of 10")

    page.get_by_role("searchbox", name="Filter commits").fill(
        "doesnotmatchanything12345"
    )
    expect(page.locator("#filter-count")).to_contain_text(
        "1 edited commit hidden by this filter"
    )
    expect(page.locator("#no-matches")).to_be_visible()


# ---------------------------------------------------------------------------
# 10. Files and Diff panels load lazily, and Files shows the rename
# ---------------------------------------------------------------------------


def test_files_and_diff_load_lazily_and_show_rename(page, server, repo):
    stat_requests: list[str] = []
    diff_requests: list[str] = []

    def record(request):
        if "/api/commit-stat" in request.url:
            stat_requests.append(request.url)
        elif "/api/commit-diff" in request.url:
            diff_requests.append(request.url)

    page.on("request", record)

    open_app(page, server)
    load_repo(page, repo)

    assert stat_requests == []
    assert diff_requests == []

    card = commit_card(page, IDX_RENAME)
    card.get_by_role("button", name="Files").click()
    expect(card.locator(".stat-row")).to_have_count(1)
    assert len(stat_requests) == 1

    expect(card.locator(".stat-status.s-R")).to_be_visible()
    expect(card.locator(".stat-path")).to_contain_text("src/app.py")
    expect(card.locator(".stat-path")).to_contain_text("src/main.py")

    assert diff_requests == []
    card.get_by_role("button", name="Diff").click()
    expect(
        card.get_by_role("region", name=re.compile(r"^Patch for commit"))
    ).to_be_visible()
    assert len(diff_requests) == 1


# ---------------------------------------------------------------------------
# 11. Cmd+S opens the modal; Escape closes it and restores focus
# ---------------------------------------------------------------------------


def test_keyboard_cmd_s_opens_modal_escape_closes_and_restores_focus(
    page, server, repo
):
    open_app(page, server)
    load_repo(page, repo)

    card = commit_card(page, IDX_WIP)
    box = message_box(card)
    box.fill("A change made so Cmd+S has something to save")

    page.keyboard.press("Meta+s")
    dialog = page.get_by_role("dialog", name="Rewrite commit messages?")
    expect(dialog).to_be_visible()

    cancel = dialog.get_by_role("button", name="Cancel")
    expect(cancel).to_be_focused()

    confirm = page.locator("#modal-confirm")
    expect(confirm).to_be_enabled()

    page.keyboard.press("Tab")
    expect(confirm).to_be_focused()
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()  # wraps back to the first control

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(box).to_be_focused()


# The key falls through to the browser exactly when the page leaves it
# un-prevented: this records that per press, since headless Chromium shows
# no "Save Page As" dialog to look for.
RECORD_SAVE_KEY_JS = """() => {
    window.__saveKeyPrevented = [];
    window.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
            window.__saveKeyPrevented.push(e.defaultPrevented);
        }
    });
}"""


def test_cmd_s_never_reaches_the_browser(page, server, repo):
    open_app(page, server)
    page.evaluate(RECORD_SAVE_KEY_JS)
    dialog = page.get_by_role("dialog", name="Rewrite commit messages?")

    page.keyboard.press("Meta+s")  # nothing loaded
    expect(dialog).to_be_hidden()

    load_repo(page, repo)
    page.keyboard.press("Meta+s")  # loaded, nothing edited
    expect(dialog).to_be_hidden()

    message_box(commit_card(page, IDX_WIP)).fill(
        "An edit, so there is something to save"
    )
    page.keyboard.press("Meta+s")  # edits pending
    expect(dialog).to_be_visible()
    page.keyboard.press("Meta+s")  # dialog already open
    expect(dialog).to_be_visible()
    expect(dialog).to_have_count(1)

    page.keyboard.press("Escape")
    page.keyboard.press("Control+s")  # the non-Apple spelling
    expect(dialog).to_be_visible()

    assert page.evaluate("window.__saveKeyPrevented") == [True] * 5


def test_keyboard_cmd_enter_confirms_the_dialog(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    message_box(commit_card(page, IDX_WIP)).fill("Saved from the keyboard alone")
    page.keyboard.press("Meta+s")
    expect(page.locator("#modal-confirm")).to_be_enabled()
    with page.expect_request("**/api/save"):
        page.keyboard.press("Meta+Enter")

    expect(page.locator("#toast")).to_contain_text("Messages rewritten")
    expect(page.locator("#edit-count")).to_have_text("No edits")


# ---------------------------------------------------------------------------
# 12. Reset message (per card) and Discard edits (all) restore originals
# ---------------------------------------------------------------------------


def test_reset_message_and_discard_all_restore_originals(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    card_a = commit_card(page, IDX_WIP)
    box_a = message_box(card_a)
    original_a = box_a.input_value()
    box_a.fill("Temporary edit for card A, to be reset individually")
    expect(card_a.locator(".commit-tag.edited")).to_be_visible()

    card_a.get_by_role("button", name="Reset message").click()
    expect(box_a).to_have_value(original_a)
    expect(card_a.locator(".commit-tag.edited")).to_be_hidden()

    card_b = commit_card(page, IDX_CONFIG_LOADER)
    box_b = message_box(card_b)
    original_b = box_b.input_value()
    box_b.fill("Temporary edit for card B, to be discarded with everything else")
    expect(card_b.locator(".commit-tag.edited")).to_be_visible()

    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Discard edits").click()

    expect(page.locator("#edit-count")).to_have_text("No edits")
    expect(page.locator("#draft-status")).to_be_hidden()
    expect(box_b).to_have_value(original_b)


# ---------------------------------------------------------------------------
# 13. Orphan drafts drawer when the view narrows
# ---------------------------------------------------------------------------


def test_orphan_drafts_drawer_when_view_narrows(page, server, repo):
    open_app(page, server)
    load_repo(page, repo)

    oldest_card = commit_card(page, IDX_FIRST_AUTOMATIC)
    orphan_text = "A draft on the oldest commit that will fall out of view"
    message_box(oldest_card).fill(orphan_text)

    page.get_by_label(re.compile(r"^Base")).fill("")
    page.get_by_label(re.compile(r"^Max commits")).fill("3")
    page.get_by_role("button", name="Load commits").click()

    groups = page.get_by_role("group", name=re.compile(r"^Commit [0-9a-f]{9} by "))
    expect(groups).to_have_count(3)

    orphans = page.locator("#orphan-drafts")
    expect(orphans).to_be_visible()
    expect(orphans).to_contain_text("1 draft outside this view")
    expect(orphans).to_contain_text(orphan_text)


# ---------------------------------------------------------------------------
# 14. A cancelled preview must not enable confirm for a different, later edit
# ---------------------------------------------------------------------------


def test_cancelled_preview_does_not_enable_confirm_for_a_different_edit(
    page, server, repo
):
    """A preview response for a dialog that was cancelled must not enable
    Confirm, or repaint the impact, in the dialog that is open now."""
    open_app(page, server)
    load_repo(page, repo)
    install_preview_probe(page)

    held_routes: list[Route] = []
    page.route("**/api/preview", lambda route: held_routes.append(route))

    card_a = commit_card(page, IDX_WIP)
    message_box(card_a).fill("First edit, whose preview will be released last")
    with page.expect_request("**/api/preview"):
        page.keyboard.press("Meta+s")

    dialog = page.get_by_role("dialog", name="Rewrite commit messages?")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()

    card_b = commit_card(page, IDX_CONFIG_LOADER)
    message_box(card_b).fill("Second edit, whose preview must win")
    with page.expect_request("**/api/preview"):
        page.keyboard.press("Meta+s")
    expect(dialog).to_be_visible()

    confirm = page.locator("#modal-confirm")
    impact = page.locator("#modal-impact")

    # The stale response lands first and is fully handled: the dialog, which
    # now describes TWO edits, must still be waiting on its own preview.
    release_preview(page, held_route(page, held_routes, 1))
    expect(confirm).to_be_disabled()
    expect(confirm).to_have_text("Calculating…")
    expect(impact).to_contain_text("Calculating impact")

    # Then the current one, which is the one that counts.
    release_preview(page, held_route(page, held_routes, 2))
    expect(confirm).to_be_enabled()
    expect(impact).to_contain_text("You edited 2 messages")


# ---------------------------------------------------------------------------
# 15. Two tabs, two different commits: neither draft clobbers the other
# ---------------------------------------------------------------------------


def test_second_tab_draft_is_not_clobbered_by_first_tab(page, server, repo):
    """Two tabs on the same branch, each drafting a different commit: reloading
    one restores both (each draft has its own storage key)."""
    tab_a = page
    open_app(tab_a, server)
    load_repo(tab_a, repo)

    tab_b = tab_a.context.new_page()
    open_app(tab_b, server)
    load_repo(tab_b, repo)

    try:
        text_x = "Tab A's edit to the wip commit"
        text_y = "Tab B's edit to the config-loader commit"
        message_box(commit_card(tab_a, IDX_WIP)).fill(text_x)
        message_box(commit_card(tab_b, IDX_CONFIG_LOADER)).fill(text_y)

        tab_a.reload()
        load_repo(tab_a, repo)

        expect(tab_a.locator("#draft-status")).to_contain_text("2 drafts")
        expect(message_box(commit_card(tab_a, IDX_WIP))).to_have_value(text_x)
        expect(message_box(commit_card(tab_a, IDX_CONFIG_LOADER))).to_have_value(text_y)
    finally:
        tab_b.close()


# ---------------------------------------------------------------------------
# 16. A save from one tab must not destroy a newer draft typed in another tab
#     while that save was in flight
# ---------------------------------------------------------------------------


def test_save_from_one_tab_keeps_a_newer_draft_from_another(page, server, repo):
    """A save's post-save cleanup drops only the text it submitted: a newer
    draft another tab stored for the same commit meanwhile is kept."""
    tab_a = page
    open_app(tab_a, server)
    load_repo(tab_a, repo)

    tab_b = tab_a.context.new_page()
    open_app(tab_b, server)
    load_repo(tab_b, repo)

    try:
        text_a = "Tab A's version of the wip message"
        message_box(commit_card(tab_a, IDX_WIP)).fill(text_a)

        held_saves: list[Route] = []
        tab_a.route("**/api/save", lambda route: held_saves.append(route))

        tab_a.get_by_role("button", name="Save changes").click()
        confirm = tab_a.locator("#modal-confirm")
        expect(confirm).to_be_enabled()
        with tab_a.expect_request("**/api/save"):
            confirm.click()

        # While A's save is held in flight, B types a newer, still-unsaved
        # draft for the very same commit.
        newer_text = "Tab B's newer draft, typed while A's save is in flight"
        message_box(commit_card(tab_b, IDX_WIP)).fill(newer_text)
        expect(tab_b.locator("#draft-status")).to_contain_text("1 draft")

        with tab_a.expect_response("**/api/save"):
            held_route(tab_a, held_saves).continue_()
        expect(tab_a.locator("#toast")).to_contain_text("Messages rewritten")

        tab_b.reload()
        load_repo(tab_b, repo)

        # The commit B drafted against was rewritten by A, so its SHA is no
        # longer in the view: B's text is an orphan draft, not gone.
        orphans = tab_b.locator("#orphan-drafts")
        expect(orphans).to_be_visible()
        expect(orphans).to_contain_text("1 draft outside this view")
        expect(orphans).to_contain_text(newer_text)
        expect(message_box(commit_card(tab_b, IDX_WIP))).to_have_value(text_a)
    finally:
        tab_b.close()


# ---------------------------------------------------------------------------
# 17. A reload that lands under the open dialog invalidates its preview
# ---------------------------------------------------------------------------


def test_reload_under_the_dialog_keeps_confirm_disabled(page, server, repo):
    """A dialog opened against one tip/range must not confirm against another,
    even when the edit set it lists is unchanged."""
    open_app(page, server)
    load_repo(page, repo)
    install_preview_probe(page)

    # Edit the newest commit: it stays in view when the range narrows, so the
    # edit set the dialog describes is the same before and after the reload.
    message_box(commit_card(page, IDX_TEST_LOADER)).fill(
        "An edit on the newest commit, which stays in view"
    )

    held_commits: list[Route] = []
    held_previews: list[Route] = []
    page.route("**/api/commits", lambda route: held_commits.append(route))
    page.route("**/api/preview", lambda route: held_previews.append(route))

    # A narrower range is requested, and while that is in flight the dialog
    # opens with a preview against the range currently on screen.
    page.get_by_label(re.compile(r"^Base")).fill("")
    page.get_by_label(re.compile(r"^Max commits")).fill("3")
    with page.expect_request("**/api/commits"):
        page.get_by_role("button", name="Load commits").click()
    with page.expect_request("**/api/preview"):
        page.keyboard.press("Meta+s")

    dialog = page.get_by_role("dialog", name="Rewrite commit messages?")
    expect(dialog).to_be_visible()
    confirm = page.locator("#modal-confirm")
    impact = page.locator("#modal-impact")
    expect(confirm).to_be_disabled()

    # The reload lands first: three cards, the edited one still among them.
    with page.expect_response("**/api/commits"):
        held_route(page, held_commits).continue_()
    groups = page.get_by_role("group", name=re.compile(r"^Commit [0-9a-f]{9} by "))
    expect(groups).to_have_count(3)
    expect(page.locator("#edit-count")).to_have_text("1 commit edited")
    expect(confirm).to_be_disabled()
    expect(confirm).to_have_text("Commits changed")
    expect(impact).to_contain_text("commits changed")

    # Then the preview computed for the old range: it must change nothing.
    release_preview(page, held_route(page, held_previews))
    expect(confirm).to_be_disabled()
    expect(confirm).to_have_text("Commits changed")
    expect(impact).not_to_contain_text("You edited")


# ---------------------------------------------------------------------------
# 18. With no explicit choice, the theme follows the OS
# ---------------------------------------------------------------------------


def test_system_theme_follows_the_os(browser, server):
    """With no data-theme set, a light OS gets exactly the light tokens an
    explicit light choice sets, and a dark OS gets the dark ones."""
    light = browser.new_context(color_scheme="light")
    dark = browser.new_context(color_scheme="dark")
    try:
        page = light.new_page()
        page.goto(server)
        assert page.get_attribute("html", "data-theme") is None
        body = page.locator("body")
        expect(body).to_have_css("background-color", "rgb(243, 245, 247)")
        system_tokens = page.evaluate(THEME_TOKENS_JS)

        # The toggle cycles system -> dark -> light. An explicit dark choice
        # must win over a light OS; an explicit light choice must be the same
        # token set the OS preference produced.
        toggle = page.get_by_role("button", name=re.compile(r"^Colour theme"))
        toggle.click()
        assert page.get_attribute("html", "data-theme") == "dark"
        expect(body).to_have_css("background-color", "rgb(15, 18, 22)")
        toggle.click()
        assert page.get_attribute("html", "data-theme") == "light"
        assert page.evaluate(THEME_TOKENS_JS) == system_tokens

        page = dark.new_page()
        page.goto(server)
        assert page.get_attribute("html", "data-theme") is None
        expect(page.locator("body")).to_have_css("background-color", "rgb(15, 18, 22)")
    finally:
        light.close()
        dark.close()


# ---------------------------------------------------------------------------
# 19. A draft that storage refuses is reported on its own, and kept in memory
# ---------------------------------------------------------------------------


def test_failed_draft_write_is_reported_per_draft_and_kept(page, server, repo):
    """One draft that cannot reach storage is reported as unsaved on its own;
    the others are still saved, and it survives a same-branch reload."""
    open_app(page, server)
    load_repo(page, repo)

    wip = commit_card(page, IDX_WIP)
    wip_sha = wip.get_attribute("data-sha")
    # Storage refuses writes for this one commit only.
    page.evaluate(
        """(sha) => {
            const real = Storage.prototype.setItem;
            Storage.prototype.setItem = function (key, value) {
                if (key.endsWith(sha)) throw new DOMException("full", "QuotaExceededError");
                return real.call(this, key, value);
            };
        }""",
        wip_sha,
    )

    lost_text = "A draft that storage refuses"
    kept_text = "A draft that storage accepts"
    status = page.locator("#draft-status")
    message_box(wip).fill(lost_text)
    expect(status).to_have_text("1 draft · NOT saved — this tab only")
    message_box(commit_card(page, IDX_CONFIG_LOADER)).fill(kept_text)
    expect(status).to_have_text("2 drafts · 1 NOT saved — this tab only")

    # Reloading the same branch keeps the unsaved draft from memory and the
    # saved one from storage.
    page.get_by_role("button", name="Load commits").click()
    expect(page.locator("#toast")).to_contain_text("Restored 2 unsaved messages")
    expect(message_box(commit_card(page, IDX_WIP))).to_have_value(lost_text)
    expect(message_box(commit_card(page, IDX_CONFIG_LOADER))).to_have_value(kept_text)
    expect(status).to_have_text("2 drafts · 1 NOT saved — this tab only")

    # Dropping the unsaved draft clears its flag; nothing else was ever at risk.
    commit_card(page, IDX_WIP).get_by_role("button", name="Reset message").click()
    expect(status).to_have_text("1 draft · saved in this browser")


# ---------------------------------------------------------------------------
# 20. A failed backup listing is said out loud
# ---------------------------------------------------------------------------


def test_backups_listing_failure_shows_a_warning(page, server, repo):
    """When the backup list cannot be fetched the page says so, rather than
    hiding the panel as if there were none."""
    open_app(page, server)
    page.route(
        "**/api/backups",
        lambda route: route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps({"error": "simulated listing failure"}),
        ),
    )
    load_repo(page, repo)

    banner = page.locator("#banner")
    expect(banner).to_be_visible()
    expect(banner).to_have_text("Could not list backups: simulated listing failure")
    expect(banner).to_have_class(re.compile(r"\bwarn\b"))
    expect(page.locator("#backups")).to_be_hidden()


if __name__ == "__main__":
    # `python tests/test_ui.py <dir>` builds the fixture repository at <dir>,
    # which shots.yml uses to take the README screenshot.
    build_fixture_repo(Path(sys.argv[1]))
