"""Tests for app.wrap_commit_message and the save-time wrapping wiring.

Run under pytest (`pytest test_app.py`), or directly as a script
(`python test_app.py`), which delegates to pytest via the __main__ guard
below. The repo helpers and the `client`/`post_json` fixtures live in
conftest.py so the end-to-end tests here can exercise a real save against a
real git repo.
"""

from __future__ import annotations

import json
import re
import sys

import pytest

from conftest import backup_refs, commit, run_git
from git_commit_editor import app as appmod
from git_commit_editor import gitops
from git_commit_editor.app import wrap_commit_message

# The reference message supplied as "correct" output. It is greedily wrapped at
# a maximum line length of 69 columns (the longest line is 69 chars, and the
# next word on every line would overflow 69), so it must round-trip unchanged
# through wrap_commit_message(…, 69).
EXAMPLE = """\
Keep sidebar actions visible but disabled on configuration form views

Prior to this change, the Self Serve Configurations Framework only
passed the sidebar "extra actions" to Detail and List views, so the
buttons vanished when navigating to Edit, Create, Confirmation or
one-time-only pages; and several form templates still wrapped their
card in a nested col-12 div, so the card changed width between
read-only and form pages. Together these caused a distracting layout
shift, and the audit_url/extra_actions API was duplicated between
DetailView and ListView.

This change moves the sidebar API onto ConfigurationViewMixin with an
explicit get_sidebar_context() hook, renders extra actions as
disabled buttons on form views (extra_actions_disabled), removes the
vestigial col-12 wrappers (keeping the configuration-content id used
as an HTMX target), and wires the SCIM identity provider pages - the
framework's only extra-actions consumer - so the List/Create and
Detail/Edit/Delete/Regenerate journeys each share one action set
built by module-level helpers. The JPN dev recipe-preset templates
carried the same vestigial wrapper and are fixed the same way."""


def _words(s: str) -> list[str]:
    """Whitespace-delimited tokens, for asserting no word is lost or duplicated."""
    return sorted(s.split())


def test_subject_never_wrapped():
    long_subject = (
        "This is an intentionally very long subject line that exceeds any "
        "sane wrap width by a wide margin"
    )

    w = wrap_commit_message(long_subject + "\n\nBody.", 50)

    assert w.split("\n")[0] == long_subject


def test_reflow_removes_superfluous_breaks():
    # A paragraph broken up earlier than the column limit by hand is re-flowed.
    msg = (
        "Subject\n\nThis paragraph has\nbeen broken up early\ninto short lines by hand."
    )

    w = wrap_commit_message(msg, 72)

    assert (
        w
        == "Subject\n\nThis paragraph has been broken up early into short lines by hand."
    )


def test_paragraph_breaks_preserved():
    msg = "Subject\n\nPara one.\n\nPara two.\n\nPara three."

    w = wrap_commit_message(msg, 72)

    assert w == msg  # three short paragraphs -> unchanged
    assert w.count("\n\n") == 3


def test_reference_example_is_greedy_stable_at_69():
    # The reference output was wrapped at 69 columns; wrapping it again is a no-op.
    assert wrap_commit_message(EXAMPLE, 69) == EXAMPLE


def test_reflow_reconstructs_reference_at_69():
    # Collapse each body paragraph onto one line (as if all breaks were wrong),
    # then prove wrapping rebuilds the reference exactly.
    paras = EXAMPLE.split("\n\n")
    mangled = (
        paras[0] + "\n\n" + "\n\n".join(" ".join(p.split("\n")) for p in paras[1:])
    )
    assert mangled != EXAMPLE  # sanity: we really did mangle it

    assert wrap_commit_message(mangled, 69) == EXAMPLE


def test_reference_example_valid_and_idempotent_at_72():
    w = wrap_commit_message(EXAMPLE, 72)

    body_lines = w.split("\n")[1:]
    assert all(len(line) <= 72 for line in body_lines)
    assert _words(w) == _words(EXAMPLE)  # no word lost or added
    assert w.split("\n")[0] == EXAMPLE.split("\n")[0]  # subject intact
    assert wrap_commit_message(w, 72) == w  # idempotent


@pytest.mark.parametrize("width", [40, 69, 72, 80])
def test_idempotent_across_widths(width):
    once = wrap_commit_message(EXAMPLE, width)

    assert wrap_commit_message(once, width) == once


def test_trailers_are_not_wrapped_or_joined():
    msg = (
        "Subject line\n\n"
        "Body paragraph that is perfectly ordinary prose right here.\n\n"
        "Signed-off-by: Alice Longname <alice.longname@example.com>\n"
        "Co-authored-by: Bob Q. Contributor <bob.contributor@example.com>"
    )

    lines = wrap_commit_message(msg, 40).split("\n")  # deliberately narrow

    assert "Signed-off-by: Alice Longname <alice.longname@example.com>" in lines
    assert "Co-authored-by: Bob Q. Contributor <bob.contributor@example.com>" in lines


def test_adjacent_key_value_prose_mid_body_is_wrapped():
    """Git only reads trailers from the final paragraph; a Before:/After: pair
    in the middle of the body is prose."""
    msg = (
        "Subject\n\n"
        "Before: the handler blocked the whole request queue badly.\n"
        "After: the handler returns early and frees the queue.\n\n"
        "Signed-off-by: A <a@example.com>"
    )

    w = wrap_commit_message(msg, 50)

    assert w == (
        "Subject\n\n"
        "Before: the handler blocked the whole request\n"
        "queue badly. After: the handler returns early and\n"
        "frees the queue.\n\n"
        "Signed-off-by: A <a@example.com>"
    )


def test_note_prefixed_prose_is_still_wrapped():
    # A lone "Note:" that begins a prose sentence is not a trailer, so it wraps.
    msg = (
        "Subject\n\n"
        "Note: this sentence begins with a label but is ordinary prose that "
        "should be joined and re-wrapped as a single paragraph."
    )

    w = wrap_commit_message(msg, 50)

    body = w.split("\n")[1:]
    assert len(body) >= 3  # actually wrapped, not left as one long line
    assert all(len(line) <= 50 for line in body)
    assert _words(w) == _words(msg.replace("\r\n", "\n"))


def test_bullets_wrap_with_hanging_indent():
    msg = (
        "Subject\n\n"
        "- this is a fairly long bullet item that will need to wrap across "
        "more than one line at a narrow width\n"
        "- second item"
    )

    lines = wrap_commit_message(msg, 40).split("\n")

    assert lines[2].startswith("- ")
    assert lines[3].startswith("  ") and not lines[3].lstrip().startswith("-")
    assert "- second item" in lines
    assert all(len(line) <= 40 for line in lines[2:])


def test_bullet_continuation_lines_are_joined():
    msg = (
        "Subject\n\n- first part of a bullet\n  that was manually split\n- next bullet"
    )

    lines = wrap_commit_message(msg, 72).split("\n")

    assert lines[2] == "- first part of a bullet that was manually split"
    assert lines[3] == "- next bullet"


@pytest.mark.parametrize("marker", ["1.", "1)"])
def test_numbered_list_wraps_with_hanging_indent(marker):
    msg = (
        "Subject\n\n"
        f"{marker} this numbered item is long enough that it has to wrap onto "
        "a second line at forty columns\n"
        f"{marker} short item"
    )

    lines = wrap_commit_message(msg, 40).split("\n")

    assert lines[2].startswith(f"{marker} ")
    assert lines[3].startswith("   ") and lines[3][3] != " "  # hanging indent of 3
    assert f"{marker} short item" in lines
    assert all(len(line) <= 40 for line in lines[2:])


@pytest.mark.parametrize("marker", ["9.", "10.", "99)"])
def test_one_or_two_digit_number_is_a_list_marker(marker):
    msg = f"Subject\n\n{marker} an item long enough to wrap at forty columns wide"

    lines = wrap_commit_message(msg, 40).split("\n")

    assert lines[2].startswith(f"{marker} ")
    assert lines[3].startswith(" " * (len(marker) + 1)), lines


@pytest.mark.parametrize("number", ["2024.", "100.", "1234)"])
def test_three_or_more_digit_number_opening_a_paragraph_is_prose(number):
    """A year or a big count at the start of a sentence is not a list."""
    msg = (
        f"Subject\n\n{number} That is when the old parser was written, "
        "and it has needed replacing ever since."
    )

    w = wrap_commit_message(msg, 40)

    body = w.split("\n")[2:]
    assert len(body) >= 2  # long enough to have wrapped
    assert all(not line.startswith(" ") for line in body), w  # no hanging indent
    assert _words(w) == _words(msg)
    # A well-known trailer key needs no neighbouring Key: value line.
    trailer = "Co-authored-by: Some Body <s@example.com>"
    assert len(trailer) > 40  # would otherwise be wrapped at this width
    msg = f"Subject\n\nBody prose.\n\n{trailer}\n\n"

    lines = wrap_commit_message(msg, 40).split("\n")

    assert trailer in lines


def test_mid_sentence_dash_is_not_a_bullet():
    # A parenthetical dash must survive wrapping without becoming a list item,
    # even when a line break lands right before it.
    msg = (
        "Subject\n\n"
        "We wire the identity provider pages - the only extra-actions "
        "consumer - so the create and edit journeys share one action set."
    )

    once = wrap_commit_message(msg, 40)

    for line in once.split("\n")[1:]:
        assert not line.startswith("  "), once  # no bullet hanging-indent introduced
    assert wrap_commit_message(once, 40) == once  # idempotent
    assert _words(once) == _words(msg.replace("\r\n", "\n"))


def test_intro_line_then_bullets_without_blank():
    msg = "Subject\n\nThis change does three things:\n- adds X\n- removes Y\n- fixes Z"

    lines = wrap_commit_message(msg, 72).split("\n")

    assert lines[2:] == [
        "This change does three things:",
        "- adds X",
        "- removes Y",
        "- fixes Z",
    ]


@pytest.mark.parametrize(
    "msg, expected",
    [
        (
            "Subject\n\nThis change does one thing:\n- adds the X handler",
            "Subject\n\nThis change does one thing:\n- adds the X handler",
        ),
        ("Subject\n\nSteps:\n1. run it", "Subject\n\nSteps:\n1. run it"),
    ],
)
def test_single_list_item_after_intro_line_stays_a_list_item(msg, expected):
    assert wrap_commit_message(msg, 72) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "Subject\n\n- item\n    code_one()\n    code_two()",
        "Subject\n\n- item\n    code_one()\n        nested()\n- next item",
        # A leading tab is preformatted wherever it appears; the continuation
        # loop must stop at it exactly as the outer loop would.
        "Subject\n\n- item\n\tcode_one()\n\tcode_two()",
        "Subject\n\n1. item\n\tcode()\n2. next",
    ],
    ids=["spaces", "spaces_nested", "tabs", "tabs_numbered"],
)
def test_indented_code_after_a_list_item_is_kept_verbatim(msg):
    assert wrap_commit_message(msg, 72) == msg


@pytest.mark.parametrize(
    "msg, expected",
    [
        # Hanging at (or inside) the marker's width is a continuation, even
        # when that is three spaces under a two-wide "- " marker.
        ("Subject\n\n- item\n   continued", "Subject\n\n- item continued"),
        ("Subject\n\n10. item\n    continued", "Subject\n\n10. item continued"),
        # Deeper than the marker AND at least four spaces is code.
        ("Subject\n\n1. item\n    code()", "Subject\n\n1. item\n    code()"),
    ],
    ids=["three_under_dash", "four_under_two_digit", "four_under_one_digit"],
)
def test_list_continuation_hangs_at_the_marker_width(msg, expected):
    assert wrap_commit_message(msg, 72) == expected


def test_long_url_is_not_broken():
    url = "https://example.com/a/very/long/path/that/keeps/going/well/past/the/limit"

    w = wrap_commit_message(f"Subject\n\nSee {url} for details.", 40)

    assert url in w  # the URL token survives intact


@pytest.mark.parametrize("token", ["one-time-only", "recipe-preset", "module-level"])
def test_hyphenated_words_not_split(token):
    msg = "Subject\n\nUse the one-time-only recipe-preset module-level helper now."

    tokens = wrap_commit_message(msg, 20).split()

    assert token in tokens


def test_indented_block_left_verbatim():
    msg = (
        "Subject\n\n"
        "Normal prose paragraph.\n\n"
        "    def f():\n"
        "        return 42  # this indented block must not be reflowed at all"
    )

    lines = wrap_commit_message(msg, 40).split("\n")

    assert "    def f():" in lines
    assert (
        "        return 42  # this indented block must not be reflowed at all" in lines
    )


def test_width_zero_disables_wrapping():
    msg = (
        "Subject\r\n\r\nA long line that would otherwise wrap but must not "
        "because wrapping is turned off here."
    )

    assert wrap_commit_message(msg, 0) == msg.replace("\r\n", "\n")


def test_crlf_and_trailing_blanks_normalised():
    msg = "Subject\r\n\r\nBody line one.\r\nBody line two.\n\n\n"

    w = wrap_commit_message(msg, 72)

    assert "\r" not in w
    assert not w.endswith("\n")
    assert w == "Subject\n\nBody line one. Body line two."


def test_leading_blank_lines_are_dropped_so_the_subject_is_the_real_one():
    msg = "\n\nreal subject\n\nbody"

    assert wrap_commit_message(msg, 72) == "real subject\n\nbody"


def test_missing_blank_line_after_subject_is_inserted():
    # No blank line typed between subject and body -> one is inserted, and the
    # body text itself is otherwise untouched.
    msg = "Fix the parser\nThis body starts on line 2."

    w = wrap_commit_message(msg, 72)

    assert w == "Fix the parser\n\nThis body starts on line 2."


def test_blank_line_already_present_is_unchanged():
    msg = "Fix the parser\n\nThis body starts on line 2."

    assert wrap_commit_message(msg, 72) == msg


def test_subject_only_message_unchanged_by_blank_line_rule():
    msg = "Fix the parser"

    assert wrap_commit_message(msg, 72) == msg


def test_blank_line_insertion_is_idempotent():
    msg = "Fix the parser\nThis body starts on line 2."

    once = wrap_commit_message(msg, 72)
    twice = wrap_commit_message(once, 72)

    assert twice == once


def test_blank_line_inserted_even_with_width_zero():
    # The blank line is a structural rule, not a wrapping one, so it must
    # still be inserted on the width<=0 (wrapping disabled) early-out path.
    msg = "Fix the parser\nThis body starts on line 2."

    assert (
        wrap_commit_message(msg, 0) == "Fix the parser\n\nThis body starts on line 2."
    )


def test_save_endpoint_inserts_missing_blank_line_end_to_end(repo, post_json):
    """End-to-end proof against real git: a message saved with no blank line
    after the subject must not have its body folded into the subject, i.e.
    `git log -1 --format=%s` must return only the subject line."""
    c = commit(repo, "f.txt", "1", "orig subject")
    message = "Fix the parser\nThis body starts on line 2."

    resp = post_json(
        "/api/save",
        json={
            "repo": str(repo),
            "branch": "main",
            "edits": {c: message},
            "expected_old_tip": c,
        },
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    subject = run_git(repo, "log", "-1", "--format=%s", "main")
    body = run_git(repo, "log", "-1", "--format=%b", "main")
    assert subject == "Fix the parser"
    assert body.startswith("This body starts on line 2.")


def test_save_endpoint_wraps_on_save(repo, post_json, monkeypatch):
    monkeypatch.setitem(appmod.app.config, "WRAP_WIDTH", 50)
    c = commit(repo, "f.txt", "1", "orig subject")
    body = "New subject stays put\n\n" + " ".join(["word"] * 40)

    resp = post_json(
        "/api/save",
        json={
            "repo": str(repo),
            "branch": "main",
            "edits": {c: body},
            "expected_old_tip": c,
        },
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    stored = run_git(repo, "log", "-1", "--format=%B", "main").rstrip("\n")
    out_lines = stored.split("\n")
    assert out_lines[0] == "New subject stays put"
    assert all(len(line) <= 50 for line in out_lines)
    assert len([line for line in out_lines[2:] if line]) > 1  # body wrapped


def test_bare_repository_is_reported_as_having_no_working_tree(tmp_path, post_json):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    run_git(bare, "init", "-q", "--bare")

    r = post_json("/api/branches", json={"repo": str(bare)})

    assert r.status_code == 400
    assert r.get_json()["error"] == (
        f"Not a git repository with a working tree: {bare}"
    )


@pytest.mark.parametrize("payload", [{}, {"repo": ""}, {"repo": "   "}])
def test_empty_repo_in_a_request_is_refused_even_with_a_default_repo(
    repo, post_json, monkeypatch, payload
):
    """DEFAULT_REPO only prefills the page; a request that lost its repository
    must never be redirected at the default one."""
    commit(repo, "f.txt", "1", "one")
    monkeypatch.setitem(appmod.app.config, "DEFAULT_REPO", str(repo))

    r = post_json("/api/branches", json=payload)

    assert r.status_code == 400
    assert r.get_json()["error"] == "A repository path is required."


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #


def _three_commits(repo):
    a = commit(repo, "f.txt", "1", "a")
    b = commit(repo, "f.txt", "1\n2", "b")
    c = commit(repo, "f.txt", "1\n2\n3", "c")
    return a, b, c


def test_commits_route_returns_tip_count_and_newest_first(repo, post_json):
    a, b, c = _three_commits(repo)

    r = post_json("/api/commits", json={"repo": str(repo), "branch": "main"})

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["tip"] == run_git(repo, "rev-parse", "main") == c
    assert data["count"] == 3
    assert [x["sha"] for x in data["commits"]] == [c, b, a]
    assert data["commits"][0]["sha"] == data["tip"]


@pytest.mark.parametrize(
    "raw, expected",
    [(5000, 1000), (0, 1), ("abc", 30), (None, 30)],
    ids=["above_max", "below_min", "unparseable", "absent"],
)
def test_limit_is_clamped_to_bounds(repo, post_json, raw, expected):
    assert (gitops.MAX_COMMITS, gitops.DEFAULT_LIMIT) == (1000, 30)
    commit(repo, "f.txt", "1", "one")

    r = post_json(
        "/api/commits", json={"repo": str(repo), "branch": "main", "limit": raw}
    )

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["limit"] == expected


@pytest.mark.parametrize("literal", ["1e999", "-1e999"], ids=["inf", "neg_inf"])
def test_limit_overflow_literal_falls_back_to_the_default(repo, post_json, literal):
    """Python's json decodes 1e999 to inf, which int() cannot convert; that
    must read as "not given", not surface as a 500. Sent as a raw body: the
    client's json= would serialise inf as the non-JSON `Infinity`."""
    commit(repo, "f.txt", "1", "one")
    body = f'{{"repo": {json.dumps(str(repo))}, "branch": "main", "limit": {literal}}}'

    r = post_json("/api/commits", data=body, content_type="application/json")

    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["limit"] == gitops.DEFAULT_LIMIT


def test_save_requires_expected_old_tip(repo, post_json):
    _a, b, c = _three_commits(repo)

    r = post_json(
        "/api/save",
        json={"repo": str(repo), "branch": "main", "edits": {b: "b new"}},
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    assert "expected_old_tip is required" in r.get_json()["error"]
    assert run_git(repo, "rev-parse", "main") == c
    assert backup_refs(repo) == []


def test_preview_reports_blast_radius_and_moves_nothing(repo, post_json):
    a, _b, c = _three_commits(repo)

    r = post_json(
        "/api/preview",
        json={
            "repo": str(repo),
            "branch": "main",
            "edits": {a: "a new"},
            "expected_old_tip": c,
        },
    )

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["rewritten_count"] == 3  # a + the two descendants replayed
    assert data["message_change_count"] == 1
    assert data["dry_run"] is True
    assert data["backup_ref"] is None
    assert run_git(repo, "rev-parse", "main") == c
    assert backup_refs(repo) == []


@pytest.mark.parametrize(
    "target",
    ["refs/heads/main", "refs/remotes/origin/protected"],
    ids=["local", "remote"],
)
def test_save_to_a_symbolic_branch_is_refused_and_moves_nothing(
    repo, post_json, target
):
    """A symbolic `refs/heads/alias` resolves like a branch everywhere except
    where it matters: `update-ref` would follow it and move its target."""
    _a, b, c = _three_commits(repo)
    run_git(repo, "update-ref", "refs/remotes/origin/protected", c)
    run_git(repo, "symbolic-ref", "refs/heads/alias", target)

    r = post_json(
        "/api/save",
        json={
            "repo": str(repo),
            "branch": "alias",
            "edits": {b: "b new"},
            "expected_old_tip": c,
        },
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    assert "symbolic" in r.get_json()["error"]
    assert run_git(repo, "rev-parse", target) == c
    assert run_git(repo, "rev-parse", "refs/heads/main") == c
    assert run_git(repo, "symbolic-ref", "refs/heads/alias") == target
    assert backup_refs(repo) == []


def test_backup_delete_route_returns_remaining_backups(repo, post_json):
    _a, _b, c = _three_commits(repo)
    first = gitops.rewrite_messages(str(repo), "main", {c: "c v2"}, limit=10)
    second = gitops.rewrite_messages(
        str(repo), "main", {first.new_tip: "c v3"}, limit=10
    )
    assert len(backup_refs(repo)) == 2

    r = post_json(
        "/api/backup-delete", json={"repo": str(repo), "ref": first.backup_ref}
    )

    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["deleted"] == first.backup_ref
    assert [b["ref"] for b in data["backups"]] == [second.backup_ref]
    assert backup_refs(repo) == [second.backup_ref]


def test_detached_head_reports_no_current_branch(repo, post_json):
    commit(repo, "f.txt", "1", "one")
    run_git(repo, "checkout", "-q", "--detach")

    r = post_json("/api/branches", json={"repo": str(repo)})

    assert gitops.list_branches(str(repo))["current"] is None
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["current"] is None
    assert r.get_json()["branches"] == ["main"]


# --------------------------------------------------------------------------- #
# Request guards                                                               #
# --------------------------------------------------------------------------- #


def test_api_post_requires_csrf_token(client):

    r_no_token = client.post(
        "/api/branches", json={"repo": "."}, headers={"Host": "localhost:5050"}
    )
    r_wrong_token = client.post(
        "/api/branches",
        json={"repo": "."},
        headers={"Host": "localhost:5050", "X-CSRF-Token": "wrong-token"},
    )

    assert r_no_token.status_code == 403, r_no_token.status_code
    assert r_wrong_token.status_code == 403, r_wrong_token.status_code


def test_api_post_requires_json_content_type(client):

    r = client.post(
        "/api/branches",
        data="{}",
        headers={
            "Host": "localhost:5050",
            "Content-Type": "text/plain",
            "X-CSRF-Token": appmod.CSRF_TOKEN,
        },
    )

    assert r.status_code == 415, r.status_code


@pytest.mark.parametrize(
    "host", ["localhost:5050", "127.0.0.1:5050", "[::1]:5050", "localhost", "[::1]"]
)
def test_loopback_hosts_are_accepted_by_default(client, host):
    r = client.get("/", headers={"Host": host})

    assert r.status_code == 200, (host, r.status_code)


@pytest.mark.parametrize(
    "method, path", [("GET", "/"), ("POST", "/api/branches")], ids=["index", "api"]
)
@pytest.mark.parametrize(
    "host",
    [
        "evil.example:5050",
        "127.0.0.1.nip.io:5050",
        "localhost.:5050",
        # An unbracketed IPv6 literal is not a valid Host header; Werkzeug
        # blanks it and the guard must refuse the blank, not wave it through.
        "::1",
    ],
)
def test_foreign_host_is_refused_by_default(client, method, path, host):
    """DNS-rebinding defence, judged by *name*: a host that merely resolves
    to loopback is exactly the attack. It covers `/` too, since a page the
    attacker can load leaks the CSRF token."""
    r = client.open(
        path,
        method=method,
        json={"repo": "."},
        headers={"Host": host, "X-CSRF-Token": appmod.CSRF_TOKEN},
    )

    assert r.status_code == 400, (host, path, r.status_code)
    assert r.get_json() == {"error": "Invalid Host header."}


@pytest.mark.parametrize("origin", ["http://evil.example", "null"])
def test_cross_origin_post_refused(post_json, origin):

    r = post_json(
        "/api/branches",
        json={"repo": "."},
        headers={"Host": "localhost:5050", "Origin": origin},
    )

    assert r.status_code == 403, (origin, r.status_code)


def test_api_error_over_the_size_limit_is_json(post_json):
    oversized = {"repo": ".", "pad": "x" * (5 * 1024 * 1024 + 1)}

    r = post_json("/api/branches", json=oversized)

    assert r.status_code == 413
    assert r.content_type == "application/json"
    assert "error" in r.get_json()


def test_unknown_api_route_is_json(post_json):
    r = post_json("/api/no-such-route", json={})

    assert r.status_code == 404
    assert r.content_type == "application/json"
    assert "error" in r.get_json()


def test_security_headers_present(client):

    r = client.get("/", headers={"Host": "localhost:5050"})

    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    # Config reaches the page as a JSON block, not inline script: the policy
    # must therefore be strict enough to actually forbid inline script.
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Cache-Control"] == "no-store"


def test_non_loopback_bind_is_refused(monkeypatch, capsys):
    """Unauthenticated history rewriting must never be reachable off-host."""
    runs = []
    monkeypatch.setattr(appmod.app, "run", lambda **kw: runs.append(kw))
    monkeypatch.setattr(sys, "argv", ["app.py", "--host", "0.0.0.0"])

    with pytest.raises(SystemExit) as exc_info:
        appmod.main()

    assert exc_info.value.code == 2  # argparse usage error
    assert "refusing to bind 0.0.0.0" in capsys.readouterr().err
    assert runs == []


def test_config_defaults_exist_at_import():
    """Reading `config[...]` (not `.get(k, default)`) is only safe because the
    defaults are seeded at import; a stored None must never masquerade as one."""
    assert appmod.app.config["WRAP_WIDTH"] == 72
    assert appmod.app.config["DEFAULT_REPO"] == ""


def test_wrap_width_reaches_the_template(client, monkeypatch):
    """The subject-length hint must be driven by the real --wrap-width, not a
    constant in app.js that silently disagrees."""
    monkeypatch.setitem(appmod.app.config, "WRAP_WIDTH", 96)

    html = client.get("/").get_data(as_text=True)

    match = re.search(
        r'<script id="gce-config" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, html[:400]
    config = json.loads(match.group(1))
    assert config["wrapWidth"] == 96
    assert config["csrfToken"] == appmod.CSRF_TOKEN


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
