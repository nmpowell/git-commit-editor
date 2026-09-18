/**
 * Git Commit Message Editor — frontend.
 *
 * Load a repo, pick a branch, edit commit-message textareas, and save. Saving
 * POSTs the changed messages to the backend, which rewrites the affected
 * commits (and replays descendants). Edited cards are highlighted; the tip SHA
 * loaded with the commits is sent back as a guard against concurrent changes.
 */

const $ = (id) => document.getElementById(id);

// Minimum editor height: two lines. Anything larger is decided by the content,
// via autoGrow().
const MIN_TA_H = 66;
// Server-supplied: the real --wrap-width (so the ruler cannot drift from what
// saving does), the CSRF token, and the commit-count default and cap.
const CONFIG = JSON.parse($("gce-config").textContent);
const WRAP_WIDTH = CONFIG.wrapWidth;
// Subject-length thresholds are a separate convention from the body-wrap
// column above, and are NOT derived from it: they are what git/GitHub
// themselves do with the subject line, regardless of how the body wraps.
const SUBJECT_SOFT = 50; // convention: git/GitHub show ~50 comfortably
const SUBJECT_HARD = 72; // past this, GitHub truncates
// Garnish: a column-ruler hairline inside the editor at the real wrap width
// (style.css draws it as a background gradient off this custom property). If
// wrapping is disabled, --wrap-col is never set and the gradient collapses.
if (WRAP_WIDTH > 0) document.documentElement.style.setProperty("--wrap-col", `${WRAP_WIDTH}ch`);

const el = {
    repoPath: $("repo-path"),
    loadBranchesBtn: $("load-branches-btn"),
    branchControls: $("branch-controls"),
    branchSelect: $("branch-select"),
    baseRef: $("base-ref"),
    limit: $("limit"),
    loadCommitsBtn: $("load-commits-btn"),
    repoMeta: $("repo-meta"),
    banner: $("banner"),
    stateWarn: $("state-warn"),
    listInfo: $("list-info"),
    placeholder: $("placeholder"),
    commitList: $("commit-list"),
    saveBar: $("save-bar"),
    editCount: $("edit-count"),
    resetAllBtn: $("reset-all-btn"),
    saveBtn: $("save-btn"),
    kbdHint: document.querySelector(".kbd-hint"),
    modalModKey: $("modal-mod-key"),
    modalOverlay: $("modal-overlay"),
    modalBranch: $("modal-branch"),
    modalSummary: $("modal-summary"),
    modalImpact: $("modal-impact"),
    modalWarnings: $("modal-warnings"),
    modalCancel: $("modal-cancel"),
    modalConfirm: $("modal-confirm"),
    toast: $("toast"),
    saveLog: $("save-log"),
    saveLogList: $("save-log-list"),
    saveLogClear: $("save-log-clear"),
    draftStatus: $("draft-status"),
    orphans: $("orphan-drafts"),
    filterBar: $("filter-bar"),
    filter: $("filter"),
    filterEdited: $("filter-edited"),
    filterCount: $("filter-count"),
    noMatches: $("no-matches"),
    backups: $("backups"),
    backupsList: $("backups-list"),
    backupsRefresh: $("backups-refresh"),
    themeToggle: $("theme-toggle"),
    themeIcon: $("theme-icon"),
    expandAll: $("expand-all"),
    timeline: $("timeline"),
    tlOldestLabel: $("tl-oldest-label"),
    livePolite: $("live-polite"),
    liveAssertive: $("live-assertive"),
};

// ---- inline icons -----------------------------------------------------------
// Lucide 1.27.0 (https://lucide.dev, ISC licence — see the notice at the end of
// this block), pasted as raw SVG. Inline rather than a webfont because the CSP
// forbids remote assets and because `currentColor` then follows the theme for
// free. Decorative throughout — every control that uses one also carries a
// text label.
const ICON = {
    chevron: '<svg class="icon chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
    pencil: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/></svg>',
    sun: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>',
    moon: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/></svg>',
    monitor: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/></svg>',
    ok: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>',
    warn: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    close: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
    reset: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>',
    merge: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/></svg>',
};
// Lucide icon notice (ISC): Copyright (c) 2026 Lucide Icons and Contributors.
// Permission to use, copy, modify, and/or distribute this software for any
// purpose with or without fee is hereby granted, provided that the above
// copyright notice and this permission notice appear in all copies. THE
// SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
// REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
// AND FITNESS. Lucide icons are derived in part from Feather (MIT, Copyright
// (c) 2013-2023 Cole Bemis).

/** Announce something to assistive tech that has no visible focus change. */
let politeTimer = null;
function announce(message, assertive = false) {
    if (assertive) {
        el.liveAssertive.textContent = message;
        return;
    }
    // Debounced: announcing on every keystroke of a filter is unusable.
    clearTimeout(politeTimer);
    politeTimer = setTimeout(() => {
        el.livePolite.textContent = message;
    }, 300);
}

// ---- draft persistence ------------------------------------------------------
// Typed-but-unsaved messages survive a reload. One localStorage key per draft:
// localStorage has no locking, so a tab that rewrote a whole-branch record
// would clobber another tab's newer text, whereas independent keys cannot
// touch each other. The RAW textarea text is stored so a restored draft comes
// back exactly as typed.

const DRAFT_PREFIX = "git-commit-editor:draft:v2";

const drafts = {
    map: new Map(), // sha -> raw text, for the loaded repo + branch
    prefix: null, // key prefix of the loaded repo + branch
    // SHAs whose last write to storage failed: their text exists in this tab
    // only. Tracked per draft, so one over-quota message does not make every
    // other draft look unsaved — and a later successful write clears only its
    // own SHA.
    failed: new Set(),
    orphans: [], // drafts whose SHA is not in the loaded range

    /** True when every draft in `map` is also in storage; the save bar and the
     *  beforeunload guard key off this. */
    get persistent() {
        return this.failed.size === 0;
    },

    load(repoId, branch) {
        const prefix = repoId && branch ? `${DRAFT_PREFIX}:${repoId}:${branch}:` : null;
        // Storage is the source of truth — except for the drafts that never
        // reached it. Re-entering the same scope keeps this tab's copy of those
        // (it is the only copy); leaving the scope drops them, which is what
        // confirmLeavingUnsavedDrafts() asked about.
        const held = new Map();
        if (prefix === this.prefix) {
            this.failed.forEach((sha) => {
                if (this.map.has(sha)) held.set(sha, this.map.get(sha));
            });
        } else {
            this.failed.clear();
        }
        this.prefix = prefix;
        this.map = held;
        this.orphans = [];
        if (!prefix) return;
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const sha = key.startsWith(prefix) ? key.slice(prefix.length) : null;
                if (sha && !this.map.has(sha)) this.map.set(sha, localStorage.getItem(key));
            }
        } catch {
            // Storage unreadable: nothing to restore. If it is unwritable too,
            // the first set() below marks its draft as failed, which is the
            // per-draft fact the UI reports; there is no draft to blame yet.
        }
    },

    set(sha, text) {
        this.map.set(sha, text);
        try {
            localStorage.setItem(this.prefix + sha, text);
            this.failed.delete(sha);
        } catch {
            this.failed.add(sha);
        }
        updateDraftStatus();
    },

    clear(sha) {
        this.map.delete(sha);
        // With no text left there is nothing that exists "in this tab only",
        // whether or not the removal below succeeds.
        this.failed.delete(sha);
        try {
            localStorage.removeItem(this.prefix + sha);
        } catch {
            /* a stale copy left in storage is reconciled on the next load */
        }
        updateDraftStatus();
    },

    /** Drop the drafts a save submitted (`edits` is the exact {sha: text}
     *  object that was POSTed) — unless another tab has since written a newer
     *  draft for the same commit, which is its business, not ours. */
    clearSubmitted(edits) {
        Object.entries(edits).forEach(([sha, text]) => {
            let stored = null;
            try {
                stored = localStorage.getItem(this.prefix + sha);
            } catch {
                /* nothing stored means nothing newer to keep */
            }
            if (stored === null || normalize(stored) === text) this.clear(sha);
        });
    },
};

// Session state
const state = {
    repoPath: "",
    repoId: "",
    branch: "",
    base: null,
    limit: null,
    defaultBranch: null, // auto-detected main/integration branch
    suggestedBase: "", // the Base last filled in automatically, so a typed one is left alone
    tip: null, // tip SHA at load time -> expected_old_tip on save
    statePayload: null, // last repo state (dirty/worktrees/operation)
    original: new Map(), // sha -> original normalized message
    current: new Map(), // sha -> current textarea value (normalized)
    raw: new Map(), // sha -> EXACT textarea text (trailing blank lines etc.)
    order: [], // shas newest-first
    meta: new Map(), // sha -> commit dict
};

// ---- helpers ----------------------------------------------------------------

async function postJSON(url, body) {
    const res = await fetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            // A cross-site page cannot read this token, and cannot set a custom
            // header without a CORS preflight this app never approves.
            "X-CSRF-Token": CONFIG.csrfToken,
        },
        body: JSON.stringify(body),
        credentials: "same-origin",
    });
    let data;
    try {
        data = await res.json();
    } catch {
        throw new Error(`Server error (${res.status})`);
    }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
}

const normalize = (s) => s.replace(/\r\n/g, "\n").replace(/\r/g, "\n").replace(/\n+$/, "");

// Escape a value for safe interpolation into HTML text content or into a
// double- or single-quoted HTML attribute (e.g. cmd()'s data-copy="...").
function escapeHTML(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Quote a value for safe interpolation into a POSIX sh command line.
function shellQuote(s) {
    if (/^[A-Za-z0-9_\/.\-@+:=,]+$/.test(s)) return s;
    return `'${s.replace(/'/g, "'\\''")}'`;
}

// A command with a real Copy button beside it: selectable text alone is
// unreachable by keyboard, and a button can wait to find out whether the
// clipboard write actually happened.
function cmd(text) {
    return (
        `<span class="copy-row"><code>${escapeHTML(text)}</code>` +
        `<button type="button" class="link copy-btn" data-copy="${escapeHTML(text)}">Copy</button></span>`
    );
}

/** Delegated: copy buttons are rendered into toasts, logs and backups. */
document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        btn.textContent = "Copied";
        announce("Command copied to the clipboard.");
        setTimeout(() => (btn.textContent = "Copy"), 2000);
    } catch {
        btn.textContent = "Press Ctrl+C";
        announce("Could not copy automatically. Select the command and copy it.", true);
    }
});

function showBanner(message, kind = "error") {
    el.banner.textContent = message;
    el.banner.className = `banner ${kind}`;
    el.banner.hidden = false;
}
function clearBanner() {
    el.banner.hidden = true;
}

// Toast lifetimes (ms). A successful save stays up long enough to read the
// old→new tip and undo command; 0 means "stay until dismissed" (errors).
// Everything a toast says is also kept in the save log, so nothing is lost
// when it disappears.
const TOAST_MS = 7000;
const TOAST_SUCCESS_MS = 20000;

let toastTimer = null;
function showToast(title, bodyHTML, kind, durationMs) {
    el.toast.className = `toast ${kind}`;
    // The glyph carries the kind, the title carries the words.
    const glyph = kind === "error" ? ICON.warn : ICON.ok;
    el.toast.innerHTML =
        `<span class="toast-icon" aria-hidden="true">${glyph}</span>` +
        `<div class="toast-main"><div class="toast-title">${escapeHTML(title)}</div>` +
        `<div class="toast-body">${bodyHTML}<br><a href="#save-log">Kept in the save log</a></div></div>` +
        `<button class="toast-close" aria-label="Dismiss">${ICON.close}</button>`;
    el.toast.hidden = false;
    el.toast.querySelector(".toast-close").onclick = () => (el.toast.hidden = true);
    clearTimeout(toastTimer);
    if (durationMs > 0) toastTimer = setTimeout(() => (el.toast.hidden = true), durationMs);
}

// Record a save result in the save log at the bottom of the page (newest
// first). Entries live for the page session; the backup refs in the repo are
// the durable record. bodyHTML is trusted markup (same as the toast body), so
// click-to-copy undo commands keep working via the [data-copy] handler.
function logEvent(title, bodyHTML, kind, repo) {
    const li = document.createElement("li");
    li.className = `save-log-entry ${kind}`;
    const when = new Date().toLocaleTimeString([], { hour12: false });
    const repoMeta = repo ? ` · <code>${escapeHTML(repo)}</code>` : "";
    li.innerHTML =
        `<div class="save-log-head">` +
        `<span class="save-log-title">${escapeHTML(title)}</span>` +
        `<span class="save-log-meta">${escapeHTML(when)}${repoMeta}</span>` +
        `</div>` +
        `<div class="save-log-body">${bodyHTML}</div>`;
    el.saveLogList.prepend(li);
    el.saveLog.hidden = false;
}

// Show a toast, keep the same message in the save log, and announce it. The
// toast is not itself a live region: content written into an element that is
// un-hidden in the same tick is not reliably read out.
function notify(title, bodyHTML, kind = "ok", durationMs = TOAST_MS, repo = state.repoPath) {
    showToast(title, bodyHTML, kind, durationMs);
    logEvent(title, bodyHTML, kind, repo);
    if (kind === "error") {
        const text = document.createElement("div");
        text.innerHTML = bodyHTML;
        announce(`${title}: ${text.textContent}`, true);
    } else {
        announce(title);
    }
}

function relativeDate(iso) {
    const then = new Date(iso);
    if (isNaN(then)) return iso;
    const secs = (Date.now() - then.getTime()) / 1000;
    const units = [
        ["year", 31536000], ["month", 2592000], ["week", 604800],
        ["day", 86400], ["hour", 3600], ["minute", 60],
    ];
    for (const [name, size] of units) {
        const n = Math.floor(secs / size);
        if (n >= 1) return `${n} ${name}${n > 1 ? "s" : ""} ago`;
    }
    return "just now";
}

function absoluteDate(iso) {
    const then = new Date(iso);
    if (isNaN(then)) return iso;
    return then.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

// ---- loading ----------------------------------------------------------------

// Monotonic generation counter: a slow response from a previous repo/branch
// must never be applied over a newer one, or cards end up associated with the
// wrong repository.
let loadGeneration = 0;

/** Drop every loaded commit and its edits. Used whenever the identity of what
 *  is on screen changes or becomes unknown — an empty editor is always safer
 *  than one whose cards belong to a different repository. */
function clearLoadedCommits() {
    loadGeneration += 1; // any /api/commits response still in flight is for a view that no longer exists
    el.loadCommitsBtn.disabled = false;
    el.commitList.classList.remove("loading");
    state.order = [];
    state.original.clear();
    state.current.clear();
    state.raw.clear();
    state.meta.clear();
    state.tip = null;
    state.branch = "";
    collapsedEditors.clear();
    openFiles.clear();
    openDiffs.clear();
    drafts.load(null, null);
    invalidatePreview();
    renderCommits();
}

/** Drafts that never reached storage exist only in this view; leaving it loses them. */
function confirmLeavingUnsavedDrafts() {
    if (drafts.persistent) return true;
    const n = drafts.failed.size;
    return confirm(
        `${n} edited message${n > 1 ? "s" : ""} could not be saved in this browser and will be lost. Continue?`
    );
}

let loadingBranches = false;
async function loadBranches() {
    if (loadingBranches) return; // guard against rapid re-entry (repeated Enter)
    loadingBranches = true;
    clearBanner();
    const repo = el.repoPath.value.trim();
    if (!repo) {
        loadingBranches = false;
        return showBanner("Enter a repository path.");
    }
    el.loadBranchesBtn.disabled = true;
    el.loadBranchesBtn.textContent = "Loading…";
    try {
        const data = await postJSON("/api/branches", { repo });
        if (data.repo_path !== state.repoPath) {
            if (!confirmLeavingUnsavedDrafts()) {
                el.repoPath.value = state.repoPath;
                return;
            }
            // A different repository invalidates every loaded commit. Clear them
            // in the same step as adopting the new identity, or a failed commit
            // load below would leave repo A's edits under repo B's path — and
            // two clones can share SHAs, so the server could not catch it.
            clearLoadedCommits();
            el.baseRef.value = "";
        }
        state.repoPath = data.repo_path;
        state.repoId = data.repo_id;
        state.defaultBranch = data.default_branch || null;
        el.repoPath.value = data.repo_path;
        el.branchSelect.innerHTML = "";
        data.branches.forEach((b) => {
            const opt = document.createElement("option");
            opt.value = b;
            opt.textContent = b;
            if (b === data.current) opt.selected = true;
            el.branchSelect.appendChild(opt);
        });
        el.branchControls.hidden = false;
        renderRepoMeta(data.state, data.branches.length);
        applySuggestedBase(); // default base = branch start, so only its commits show
        await loadCommits();
        loadBackups();
    } catch (err) {
        showBanner(err.message);
        el.branchControls.hidden = true;
        el.repoMeta.innerHTML = "";
        state.repoPath = ""; // don't leave edits orphaned against a hidden control set
        state.repoId = "";
        clearLoadedCommits();
        announce(err.message, true);
    } finally {
        loadingBranches = false;
        el.loadBranchesBtn.disabled = false;
        el.loadBranchesBtn.textContent = "Load repo";
    }
}

function renderRepoMeta(repoState, branchCount) {
    const pills = [`<span class="pill">${branchCount} branch${branchCount === 1 ? "" : "es"}</span>`];
    if (repoState.current_branch)
        pills.push(`<span class="pill">checked out: <code>${escapeHTML(repoState.current_branch)}</code></span>`);
    if (repoState.dirty) pills.push(`<span class="pill warn">uncommitted changes</span>`);
    else pills.push(`<span class="pill ok">working tree clean</span>`);
    el.repoMeta.innerHTML = pills.join("");
}

// Suggest the detected main branch as the base, so only the selected branch's
// own commits are shown — unless it *is* the main branch (then show recent N).
// A Base the user typed themselves is left alone.
function applySuggestedBase() {
    const branch = el.branchSelect.value;
    const suggestion = state.defaultBranch && branch && branch !== state.defaultBranch ? state.defaultBranch : "";
    const typed = el.baseRef.value.trim();
    if (typed === "" || typed === state.suggestedBase) el.baseRef.value = suggestion;
    state.suggestedBase = suggestion;
}

function renderListInfo(data) {
    if (data.count === 0) {
        el.listInfo.hidden = true;
        return;
    }
    const n = data.count;
    const branch = `<code>${escapeHTML(data.branch)}</code>`;
    if (data.base) {
        el.listInfo.innerHTML =
            `Showing the <strong>${n}</strong> commit${n > 1 ? "s" : ""} on ${branch} ` +
            `not in <code>${escapeHTML(data.base)}</code> (its base). ` +
            `Commits shared with <code>${escapeHTML(data.base)}</code> are hidden and can't be edited.`;
    } else {
        el.listInfo.innerHTML =
            `Showing the latest <strong>${n}</strong> commit${n > 1 ? "s" : ""} on ${branch} ` +
            `(no base set).`;
    }
    el.listInfo.hidden = false;
}

function renderStateWarnings(st) {
    const w = [];
    if (st.operation_in_progress)
        w.push(
            `A git ${escapeHTML(st.operation_in_progress)} is in progress — saving is blocked ` +
            `until it is finished or aborted.`
        );
    if (st.other_worktrees && st.other_worktrees.length)
        w.push(
            `This branch is also checked out in another worktree ` +
            `(${st.other_worktrees.map(escapeHTML).join(", ")}). Saving moves its HEAD; ` +
            `<code>git status</code> there may then show changes.`
        );
    el.stateWarn.innerHTML = w.map((x) => `<div>⚠ ${x}</div>`).join("");
    el.stateWarn.hidden = w.length === 0;
}

async function loadCommits() {
    clearBanner();
    el.listInfo.hidden = true;
    const branch = el.branchSelect.value;
    if (!branch) return;
    const base = el.baseRef.value.trim();
    const limit = parseInt(el.limit.value, 10) || CONFIG.defaultLimit;
    const generation = ++loadGeneration;
    const repo = state.repoPath;
    // True once the page has moved on: a newer load started, or the repository
    // changed underneath this one.
    const superseded = () => generation !== loadGeneration || state.repoPath !== repo;
    el.loadCommitsBtn.disabled = true;
    el.commitList.classList.add("loading");
    try {
        const data = await postJSON("/api/commits", { repo, branch, base: base || null, limit });
        if (superseded()) return;
        state.branch = data.branch;
        state.base = data.base;
        state.limit = data.limit;
        state.tip = data.tip;
        state.statePayload = data.state;
        // A dialog opened against the previous tip/branch/range no longer
        // describes what is on screen.
        invalidatePreview();
        state.order = [];
        state.original.clear();
        state.current.clear();
        state.raw.clear();
        state.meta.clear();

        // Load this branch's drafts BEFORE seeding current values, so a draft
        // wins over the committed message for its SHA.
        drafts.load(state.repoId, data.branch);
        let restored = 0;
        data.commits.forEach((c) => {
            const msg = normalize(c.message);
            state.order.push(c.sha);
            state.original.set(c.sha, msg);
            state.meta.set(c.sha, c);
            const draft = drafts.map.get(c.sha);
            if (draft !== undefined && normalize(draft) !== msg) {
                // current is the normalised form used for change detection and
                // submission; raw is exactly what was typed and is what the
                // textarea shows, so trailing blank lines survive a reload.
                state.current.set(c.sha, normalize(draft));
                state.raw.set(c.sha, draft);
                restored += 1;
                return;
            }
            if (draft !== undefined) drafts.clear(c.sha); // draft now matches committed text
            state.current.set(c.sha, msg);
            state.raw.set(c.sha, msg);
        });
        // Anything left over belongs to a commit outside the loaded range.
        const visible = new Set(state.order);
        drafts.orphans = [...drafts.map.entries()]
            .filter(([sha]) => !visible.has(sha))
            .map(([sha, text]) => ({ sha, text }));

        renderRepoMeta(data.state, el.branchSelect.options.length);
        renderStateWarnings(data.state);
        renderListInfo(data);
        renderCommits();
        renderOrphans();
        if (restored)
            notify(
                "Drafts restored",
                `Restored <strong>${restored}</strong> unsaved message${restored > 1 ? "s" : ""} ` +
                    `for <code>${escapeHTML(data.branch)}</code>.`
            );
        if (data.count === 0)
            showBanner(
                base
                    ? `${branch} has no commits of its own beyond ${base}. ` +
                      `Clear the Base field and reload to edit its recent commits instead.`
                    : `Branch ${branch} has no commits.`,
                "warn"
            );
    } catch (err) {
        if (superseded()) return;
        // Cards we can no longer vouch for must not stay saveable.
        clearLoadedCommits();
        showBanner(err.message);
        announce(err.message, true);
    } finally {
        if (!superseded()) {
            el.loadCommitsBtn.disabled = false;
            el.commitList.classList.remove("loading");
        }
    }
}

/** Drafts whose commit is not in the current view — recoverable, never dropped. */
function renderOrphans() {
    const box = el.orphans;
    if (!drafts.orphans.length) {
        box.hidden = true;
        box.innerHTML = "";
        return;
    }
    const n = drafts.orphans.length;
    box.hidden = false;
    box.innerHTML =
        `<details><summary>${n} draft${n > 1 ? "s" : ""} outside this view</summary>` +
        `<p class="muted">These messages were typed against commits that are not in the ` +
        `current range. Raising <strong>Max commits</strong> or clearing <strong>Base</strong> ` +
        `may bring them back. They are kept until you apply or discard them.</p>` +
        drafts.orphans
            .map(
                (d) =>
                    `<div class="orphan"><div class="orphan-head"><code>${escapeHTML(d.sha.slice(0, 9))}</code>` +
                    `<button class="ghost orphan-discard" data-sha="${escapeHTML(d.sha)}">discard</button></div>` +
                    `<pre>${escapeHTML(d.text)}</pre></div>`
            )
            .join("") +
        `</details>`;
    box.querySelectorAll(".orphan-discard").forEach((b) =>
        b.addEventListener("click", () => {
            drafts.clear(b.dataset.sha);
            drafts.orphans = drafts.orphans.filter((o) => o.sha !== b.dataset.sha);
            renderOrphans();
        })
    );
}

// ---- rendering --------------------------------------------------------------

/** Which commits the user has explicitly COLLAPSED. Editors are open by
 *  default — the task is nearly always "fix these messages", so the message
 *  should be there to fix without a click first. Tracking the *collapsed* set
 *  rather than the open one is what makes "open" the default for a commit we
 *  have not seen before: a fresh load, or a commit appended since.
 *
 *  Deliberately separate from "edited": resetting a message must not make the
 *  card collapse under the user. */
const collapsedEditors = new Set();
/** Files and Diff panels the user opened. renderCommits() rebuilds every card,
 *  so these are re-opened (and re-fetched) afterwards rather than forgotten. */
const openFiles = new Set();
const openDiffs = new Set();

function renderCommits() {
    el.placeholder.hidden = state.order.length > 0;
    el.commitList.innerHTML = "";
    // Un-hide the ledger before sizing: a textarea inside display:none reports
    // scrollHeight 0 and would be pinned to MIN_TA_H.
    el.filterBar.hidden = state.order.length === 0;
    el.timeline.hidden = state.order.length === 0;
    state.order.forEach((sha, i) => {
        const c = state.meta.get(sha);
        const card = document.createElement("li");
        card.className = "commit";
        card.dataset.sha = sha;
        // 0 at the newest commit, 1 at the oldest; the node's opacity reads off
        // this so the rail fades into the past. Pure reinforcement — the two
        // captions carry the meaning in words.
        const n = state.order.length;
        card.style.setProperty("--age", n > 1 ? (i / (n - 1)).toFixed(3) : "0");
        // A group with a name gives screen-reader users something to jump to
        // per commit; without it the SHA, author, badges, editor and buttons
        // are one undifferentiated stream.
        card.setAttribute("role", "group");
        card.setAttribute("aria-label", `Commit ${c.short} by ${c.author_name}`);

        const tags = [];
        if (c.is_merge) tags.push(`<span class="commit-tag merge">${ICON.merge}merge</span>`);
        if (c.is_root) tags.push('<span class="commit-tag">root</span>');
        if (c.signed)
            tags.push('<span class="commit-tag signed" title="Signed; the signature is lost on rewrite">signed</span>');
        // "pushed" and "on a remote somewhere" are different facts and only the
        // first actually implies a force-push to this branch's upstream.
        if (c.pushed)
            tags.push('<span class="commit-tag pushed" title="Reachable from this branch&#39;s upstream; rewriting needs a force-push">pushed</span>');
        else if (c.on_remote)
            tags.push('<span class="commit-tag on-remote" title="Reachable from some other remote-tracking ref — shared, but not on this branch&#39;s upstream">on a remote</span>');

        // Unique ids so the label/description/panel wiring works with 30 cards.
        const ed = `ed-${c.short}`, hint = `hint-${c.short}`, guide = `guide-${c.short}`;
        const files = `files-${c.short}`, diff = `diff-${c.short}`;
        const subject = state.current.get(sha).split("\n")[0] || "(no subject)";
        const when = escapeHTML(absoluteDate(c.author_date));

        card.innerHTML = `
            <p class="commit-subject">${escapeHTML(subject)}</p>
            <div class="commit-head">
                <span class="commit-sha" title="${escapeHTML(c.sha)}">${escapeHTML(c.short)}</span>
                <span class="commit-author">${escapeHTML(c.author_name)}</span>
                <time class="commit-date" datetime="${escapeHTML(c.author_date)}" title="${when}"
                      aria-label="${when}">${escapeHTML(relativeDate(c.author_date))}</time>
                ${tags.join("")}
                <span class="commit-tag edited" hidden>edited</span>
            </div>
            <div class="commit-actions">
                <button class="disclosure commit-edit-toggle" type="button"
                        aria-expanded="false" aria-controls="${ed}-wrap">
                    ${ICON.pencil}<span class="label">Edit message</span>
                </button>
                <button class="disclosure commit-preview-toggle" type="button"
                        aria-expanded="false" aria-controls="${files}">
                    ${ICON.chevron}Files
                </button>
                <button class="disclosure commit-diff-toggle" type="button"
                        aria-expanded="false" aria-controls="${diff}">
                    ${ICON.chevron}Diff
                </button>
                <button class="commit-reset link" type="button" hidden>${ICON.reset}Reset message</button>
            </div>
            <div class="editor-wrap" id="${ed}-wrap" hidden>
                <div class="editor-label">
                    <label class="name" for="${ed}">Message<span class="sr-only"> for commit ${escapeHTML(c.short)}</span></label>
                    <span id="${guide}">first line is the subject<span class="subject-count"></span></span>
                </div>
                <textarea id="${ed}" spellcheck="true" rows="2"
                          aria-describedby="${guide} ${hint}"></textarea>
                <div class="commit-hint" id="${hint}"></div>
            </div>
            <div class="commit-preview" id="${files}" hidden></div>
            <div class="commit-diff" id="${diff}" hidden></div>`;

        const ta = card.querySelector("textarea");
        // Render the RAW value, not the original and not the normalised form:
        // a restored draft must survive a re-render (renderCommits() runs after
        // every load) and must come back exactly as it was typed.
        ta.value = state.raw.get(sha) ?? state.current.get(sha);
        ta.addEventListener("input", () => onEdit(sha, card, ta));
        card.querySelector(".commit-reset").addEventListener("click", () => {
            ta.value = state.original.get(sha);
            onEdit(sha, card, ta); // clears the draft and resyncs raw
            ta.focus();
        });
        card.querySelector(".commit-edit-toggle").addEventListener("click", () =>
            toggleEditor(sha, card, true)
        );
        card.querySelector(".commit-preview-toggle").addEventListener("click", () =>
            togglePreview(sha, card)
        );
        card.querySelector(".commit-diff-toggle").addEventListener("click", () =>
            toggleDiff(sha, card)
        );
        el.commitList.appendChild(card);
        // Open unless this commit was explicitly collapsed. An edited commit
        // opens regardless: its text is what you came back for.
        const edited = state.current.get(sha) !== state.original.get(sha);
        setEditorOpen(sha, card, edited || !collapsedEditors.has(sha));
        refreshCard(sha, card, ta);
        if (openFiles.has(sha)) togglePreview(sha, card);
        if (openDiffs.has(sha)) toggleDiff(sha, card);
    });
    // Say what the bottom of the list actually is. With a base it is the point
    // the branch diverged; without one it is just the end of the window we
    // loaded, and claiming "branch start" there would be a lie.
    el.tlOldestLabel.innerHTML = state.base
        ? `<span class="tl-word">oldest</span> · branch start (${escapeHTML(state.base)})`
        : `<span class="tl-word">oldest</span> of the ${state.order.length} loaded`;
    syncExpandAll();
    applyFilter();
    updateSaveBar();
}

/** Set one commit's editor open or closed. Never driven by focus: tab
 *  navigation must not move the page underneath the user. */
function setEditorOpen(sha, card, open, moveFocus = false) {
    const wrap = card.querySelector(".editor-wrap");
    const btn = card.querySelector(".commit-edit-toggle");
    const ta = card.querySelector("textarea");
    wrap.hidden = !open;
    card.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
    btn.querySelector(".label").textContent = open ? "Hide message" : "Edit message";
    if (open) {
        collapsedEditors.delete(sha);
        autoGrow(ta);
        if (moveFocus) ta.focus();
    } else {
        collapsedEditors.add(sha);
    }
    syncExpandAll();
}

/** Click handler for the per-card disclosure. Focus follows only when opening,
 *  so collapsing does not yank the caret into a box that is disappearing. */
function toggleEditor(sha, card) {
    const opening = card.querySelector(".editor-wrap").hidden;
    setEditorOpen(sha, card, opening, opening);
}

function allEditorsOpen() {
    return state.order.every((sha) => !collapsedEditors.has(sha));
}

function syncExpandAll() {
    if (!state.order.length) return;
    el.expandAll.textContent = allEditorsOpen() ? "Collapse all" : "Expand all";
}

function autoGrow(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.max(MIN_TA_H, ta.scrollHeight + 2) + "px";
}

async function togglePreview(sha, card) {
    const box = card.querySelector(".commit-preview");
    const btn = card.querySelector(".commit-preview-toggle");
    const opening = box.hidden;
    box.hidden = !opening;
    btn.setAttribute("aria-expanded", String(opening));
    if (opening) openFiles.add(sha);
    else openFiles.delete(sha);
    if (opening && !box.dataset.loaded) {
        box.innerHTML = '<span class="muted">Loading…</span>';
        try {
            const stat = await postJSON("/api/commit-stat", { repo: state.repoPath, sha });
            box.dataset.loaded = "1";
            box.innerHTML = renderStat(stat);
            // Announce that it arrived, not the whole file list.
            announce(`Files for ${sha.slice(0, 9)}: ${stat.file_count} changed.`);
        } catch (err) {
            box.innerHTML = `<span class="err-text">${escapeHTML(err.message)}</span>`;
            announce(`Could not load files for ${sha.slice(0, 9)}.`, true);
        }
    }
}

/** The full patch, fetched on demand — never eagerly for the whole range. */
async function toggleDiff(sha, card) {
    const box = card.querySelector(".commit-diff");
    const btn = card.querySelector(".commit-diff-toggle");
    const opening = box.hidden;
    box.hidden = !opening;
    btn.setAttribute("aria-expanded", String(opening));
    if (opening) openDiffs.add(sha);
    else openDiffs.delete(sha);
    if (opening && !box.dataset.loaded) {
        box.innerHTML = '<span class="muted">Loading diff…</span>';
        try {
            const d = await postJSON("/api/commit-diff", { repo: state.repoPath, sha });
            box.dataset.loaded = "1";
            box.innerHTML = renderDiff(d);
            // The patch itself is keyboard-scrollable and labelled, rather than
            // being read out in full by a live region.
            const pre = box.querySelector("pre.diff");
            if (pre) {
                pre.tabIndex = 0;
                pre.setAttribute("role", "region");
                pre.setAttribute("aria-label", `Patch for commit ${sha.slice(0, 9)}`);
            }
            announce(`Diff for ${sha.slice(0, 9)} loaded.`);
        } catch (err) {
            box.innerHTML = `<span class="err-text">${escapeHTML(err.message)}</span>`;
            announce(`Could not load the diff for ${sha.slice(0, 9)}.`, true);
        }
    }
}

// Only a real file header, not a removed line that happens to start with "--"
// (an SQL comment, a Markdown rule) or an added one starting with "++".
const DIFF_FILE_HEADER = /^(---|\+\+\+) (a\/|b\/|\/dev\/null)/;

function renderDiff(d) {
    if (!d.diff.trim())
        return '<span class="muted">No textual changes.</span>';
    // Colourise by line prefix. git is asked for --no-color so we own this and
    // the output stays plain text we can escape safely.
    const body = d.diff
        .split("\n")
        .map((line) => {
            let cls = "";
            if (DIFF_FILE_HEADER.test(line)) cls = "d-file";
            else if (line.startsWith("@@")) cls = "d-hunk";
            else if (line.startsWith("+")) cls = "d-add";
            else if (line.startsWith("-")) cls = "d-del";
            else if (line.startsWith("diff ") || line.startsWith("index ")) cls = "d-meta";
            // A line that is empty renders as a zero-height block, so keep a
            // zero-width space to preserve blank context lines in the patch.
            return `<span class="${cls}">${escapeHTML(line) || "​"}</span>`;
        })
        // Joined with nothing, NOT "\n": the spans are display:block, and a
        // newline between them is literal text inside a <pre>, which rendered
        // every diff at double spacing.
        .join("");
    const scope = d.vs_first_parent
        ? '<div class="stat-scope">vs first parent — what this merge brought in</div>'
        : "";
    const cut = d.truncated
        ? '<div class="stat-scope">Diff truncated — open the commit in git to see it all.</div>'
        : "";
    return scope + `<pre class="diff">${body}</pre>` + cut;
}

function renderStat(stat) {
    // A merge has no single "what changed"; we show the first-parent diff (what
    // the merge brought onto this branch) and say so rather than implying it is
    // the whole story.
    const scope = stat.vs_first_parent
        ? '<div class="stat-scope">vs first parent — what this merge brought in</div>'
        : "";
    if (stat.file_count === 0)
        // A merge with no net change against its first parent is NOT an empty
        // commit — it can still be a meaningful merge.
        return (
            scope +
            `<span class="muted">${
                stat.vs_first_parent
                    ? "No changes against the first parent."
                    : "No file changes (empty commit)."
            }</span>`
        );
    const head =
        `<div class="stat-head">${stat.file_count} file${stat.file_count > 1 ? "s" : ""} changed, ` +
        `<span class="add">+${stat.additions}</span> <span class="del">−${stat.deletions}</span></div>`;
    const rows = stat.files
        .map((f) => {
            // Renames/copies carry a similarity score (R080); show the letter as
            // the badge and the score as a tooltip, with the old path inline.
            const letter = f.status[0];
            const score = f.status.length > 1 ? `${letter} ${f.status.slice(1)}%` : letter;
            const path = f.old_path
                ? `<span class="stat-old">${escapeHTML(f.old_path)}</span> → ${escapeHTML(f.path)}`
                : escapeHTML(f.path);
            const nums = f.binary
                ? '<span class="muted">binary</span>'
                : `<span class="add">+${escapeHTML(f.added)}</span> <span class="del">−${escapeHTML(f.deleted)}</span>`;
            return (
                `<div class="stat-row"><span class="stat-status s-${escapeHTML(letter)}" title="${escapeHTML(score)}">${escapeHTML(letter)}</span>` +
                `<span class="stat-path">${path}</span>` +
                `<span class="stat-nums">${nums}</span></div>`
            );
        })
        .join("");
    return scope + head + rows;
}

/** Update one card's edited markers and validation hint from current state. */
function refreshCard(sha, card, ta) {
    autoGrow(ta);
    const value = state.current.get(sha) ?? "";
    const isEdited = value !== state.original.get(sha);
    card.classList.toggle("edited", isEdited);
    card.querySelector(".commit-tag.edited").hidden = !isEdited;
    card.querySelector(".commit-reset").hidden = !isEdited;

    // The collapsed summary shows the *current* subject, so a card you have
    // edited and closed still reads as the message you gave it.
    const subject = value.split("\n")[0] || "(no subject)";
    card.querySelector(".commit-subject").textContent = subject;

    const count = card.querySelector(".subject-count");
    count.textContent = ` · subject ${subject.length} characters`;
    count.classList.toggle("over", subject.length > SUBJECT_SOFT);
    count.classList.toggle("err", subject.length > SUBJECT_HARD);

    const hint = card.querySelector(".commit-hint");
    const empty = isEdited && value.trim() === "";
    // aria-invalid is for an actual error, not a style guideline: an over-long
    // subject or a missing blank line is advice, and marking it invalid would
    // cry wolf.
    ta.setAttribute("aria-invalid", String(empty));

    // Issues are listed in this order; the hint's class follows the worst of
    // them. An empty message has nothing left to measure, so it suppresses
    // every other issue.
    const issues = [];
    if (empty) {
        issues.push({ severity: "error", text: "A commit message cannot be empty." });
    } else {
        if (subject.length > SUBJECT_HARD)
            issues.push({
                severity: "error",
                text: `Subject is ${subject.length} characters; GitHub truncates past ${SUBJECT_HARD}.`,
            });
        const lines = value.split("\n");
        if (lines.length >= 2 && lines[1].trim() !== "")
            issues.push({
                severity: "warn",
                text:
                    "Line 2 should be blank — git folds the body into the subject. " +
                    "A blank line will be inserted on save.",
            });
        if (subject.length > SUBJECT_SOFT && subject.length <= SUBJECT_HARD)
            issues.push({
                severity: "warn",
                text: `Subject is ${subject.length} characters; ${SUBJECT_SOFT} is the convention.`,
            });
    }
    const worstSeverity = issues.some((i) => i.severity === "error")
        ? "error"
        : issues.length
        ? "warn"
        : null;
    hint.textContent = issues.map((i) => i.text).join(" ");
    hint.className =
        worstSeverity === "error" ? "commit-hint err-text" : worstSeverity === "warn" ? "commit-hint over" : "commit-hint";
}

function onEdit(sha, card, ta) {
    const raw = ta.value;
    const value = normalize(raw);
    state.current.set(sha, value);
    state.raw.set(sha, raw);
    if (value !== state.original.get(sha)) {
        drafts.set(sha, raw);
    } else {
        drafts.clear(sha); // back to the committed message — no draft to keep
    }
    refreshCard(sha, card, ta);
    updateSaveBar();
    // "Edited only" is a live view of what you have changed, so re-apply it.
    if (el.filterEdited.checked) applyFilter();
}

function editedShas() {
    return state.order.filter((sha) => state.current.get(sha) !== state.original.get(sha));
}
function hasEdits() {
    return editedShas().length > 0;
}
function editsObject() {
    const o = {};
    editedShas().forEach((sha) => (o[sha] = state.current.get(sha)));
    return o;
}
function sameEdits(a, b) {
    const shas = Object.keys(a);
    return shas.length === Object.keys(b).length && shas.every((sha) => b[sha] === a[sha]);
}

function updateSaveBar() {
    const edited = editedShas();
    el.saveBar.hidden = state.order.length === 0;
    const n = edited.length;
    el.editCount.textContent = n === 0 ? "No edits" : `${n} commit${n > 1 ? "s" : ""} edited`;
    el.editCount.classList.toggle("has-edits", n > 0);
    const emptyCount = edited.filter((sha) => state.current.get(sha).trim() === "").length;
    if (emptyCount) {
        el.saveBtn.textContent = `Can't save — ${emptyCount} empty`;
        el.saveBtn.disabled = true;
    } else {
        el.saveBtn.textContent = "Save changes";
        el.saveBtn.disabled = n === 0;
    }
    el.resetAllBtn.disabled = n === 0;
    // The open dialog describes one exact set of edits; if a reload changes
    // them underneath it, the consent it asks for no longer applies.
    if (pendingSubmission && !sameEdits(pendingSubmission.edits, editsObject())) {
        el.modalConfirm.disabled = true;
        el.modalConfirm.textContent = "Edits changed";
    }
    updateDraftStatus();
}

function resetAll() {
    if (!hasEdits()) return;
    const n = editedShas().length;
    if (!confirm(`Discard ${n} unsaved message${n > 1 ? "s" : ""} in this view? This cannot be undone.`))
        return;
    // Scoped to what is visible: drafts outside this view are not touched.
    editedShas().forEach((sha) => {
        state.current.set(sha, state.original.get(sha));
        state.raw.set(sha, state.original.get(sha));
        drafts.clear(sha);
    });
    renderCommits();
}

/** List the repo's backup branches, with a copyable restore command each. */
async function loadBackups() {
    if (!state.repoPath) return;
    try {
        const data = await postJSON("/api/backups", { repo: state.repoPath });
        const list = data.backups || [];
        el.backups.hidden = list.length === 0;
        el.backupsList.innerHTML = list
            .map((b) => {
                const when = b.created
                    ? new Date(b.created * 1000).toLocaleString()
                    : "unknown date";
                // A copyable command rather than a one-click restore: moving a
                // branch back is itself history surgery and deserves a
                // deliberate paste, not a stray click next to "delete".
                const restore = cmd(
                    `git -C ${shellQuote(state.repoPath)} update-ref ` +
                        `${shellQuote("refs/heads/" + b.branch)} ${shellQuote(b.ref)}`
                );
                return (
                    `<li><div class="backup-row">` +
                    `<code class="backup-name">${escapeHTML(b.name)}</code>` +
                    `<span class="muted">${escapeHTML(b.short)} · ${escapeHTML(when)}</span>` +
                    `<button class="ghost backup-delete" data-ref="${escapeHTML(b.ref)}">delete</button>` +
                    `</div><div class="backup-restore">Restore: ${restore}</div></li>`
                );
            })
            .join("");
        el.backupsList.querySelectorAll(".backup-delete").forEach((btn) =>
            btn.addEventListener("click", async () => {
                if (!confirm(`Delete backup ${btn.dataset.ref}?\n\nThis removes a recovery point.`))
                    return;
                try {
                    await postJSON("/api/backup-delete", {
                        repo: state.repoPath, ref: btn.dataset.ref,
                    });
                    await loadBackups();
                } catch (err) {
                    showBanner(err.message);
                }
            })
        );
    } catch (err) {
        // Not worth a hard error, but a silently missing panel reads as "no
        // backups", which is a different claim from "could not check".
        el.backups.hidden = true;
        showBanner(`Could not list backups: ${err.message}`, "warn");
        announce(`Could not list backups: ${err.message}`, true);
    }
}

/** Client-side filter over the loaded commits. Hides cards; edits nothing. */
function applyFilter() {
    const q = (el.filter.value || "").trim().toLowerCase();
    const editedOnly = el.filterEdited.checked;
    const edited = new Set(editedShas());
    let shown = 0;
    el.commitList.querySelectorAll(".commit").forEach((card) => {
        const sha = card.dataset.sha;
        const c = state.meta.get(sha);
        // Search the CURRENT text so a commit is findable by what you just
        // typed, plus the full sha (substring matches short and long forms)
        // and both author and committer names.
        const hay = [
            sha,
            c.author_name,
            c.author_email,
            c.committer_name,
            state.current.get(sha),
        ]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
        const hit = (!q || hay.includes(q)) && (!editedOnly || edited.has(sha));
        card.hidden = !hit;
        if (hit) shown += 1;
    });
    const total = state.order.length;
    const hiddenEdits = [...edited].filter((sha) => {
        const card = el.commitList.querySelector(`[data-sha="${sha}"]`);
        return card && card.hidden;
    }).length;
    let text = shown === total ? `${total} commit${total === 1 ? "" : "s"}` : `${shown} of ${total}`;
    // Saving applies every edit in the loaded range, not just the visible ones.
    // Filtering is a view, and the count has to say so or it silently implies a
    // smaller save than the one that will happen.
    if (hiddenEdits)
        text += ` · ${hiddenEdits} edited commit${hiddenEdits === 1 ? "" : "s"} hidden by this filter`;
    el.filterCount.textContent = text;
    el.filterCount.classList.toggle("warn", hiddenEdits > 0);
    el.noMatches.hidden = shown > 0 || total === 0;
    if (q || editedOnly) announce(text);
}

/** Reflect draft count and storage health in the save bar. */
function updateDraftStatus() {
    const n = drafts.map.size;
    if (!n) {
        el.draftStatus.hidden = true;
        return;
    }
    el.draftStatus.hidden = false;
    const lost = drafts.failed.size;
    const count = `${n} draft${n > 1 ? "s" : ""}`;
    el.draftStatus.textContent = drafts.persistent
        ? `${count} · saved in this browser`
        : `${count} · ${lost === n ? "NOT" : `${lost} NOT`} saved — this tab only`;
    el.draftStatus.classList.toggle("warn", !drafts.persistent);
}

// ---- saving -----------------------------------------------------------------

function modalIsOpen() {
    return !el.modalOverlay.hidden;
}

// The request the open dialog describes, snapshotted when it opened: the
// complete /api/save payload, not just the edits. save() submits exactly this
// object — the blast radius the user consented to — so a reload that changes
// the tip, branch or range underneath the dialog cannot leak into the save;
// a preview response for any earlier snapshot is ignored.
let pendingSubmission = null;
let previewGeneration = 0;
let focusBeforeModal = null;

/** The loaded commits changed (or went away) underneath the dialog, so the
 *  impact it shows — or is still computing — no longer describes what would
 *  be rewritten. Any preview in flight is dropped and Confirm stays off until
 *  the dialog is closed and opened again, which takes a fresh snapshot. A
 *  save already in flight is left alone: its request is with the server,
 *  whose expected_old_tip check decides it, and "nothing has been changed"
 *  would be untrue. */
function invalidatePreview() {
    previewGeneration += 1;
    if (!pendingSubmission || saving) return;
    pendingSubmission = null;
    if (!modalIsOpen()) return;
    el.modalImpact.innerHTML =
        '<span class="muted">The loaded commits changed while this dialog was open, ' +
        "so the impact is no longer known.</span>";
    el.modalWarnings.innerHTML =
        '<div class="modal-warn">Nothing has been changed. Close this dialog and ' +
        "choose Save changes again.</div>";
    el.modalConfirm.disabled = true;
    el.modalConfirm.textContent = "Commits changed";
}

async function openModal() {
    if (!state.repoPath || !state.tip) return;
    const edits = editsObject();
    const shas = Object.keys(edits);
    if (shas.length === 0) return;
    const generation = ++previewGeneration;
    const submission = {
        repo: state.repoPath,
        branch: state.branch,
        base: state.base,
        limit: state.limit,
        // Same CAS guard the save uses: the blast radius shown here is what
        // the user consents to, so it must describe the history they loaded.
        expected_old_tip: state.tip,
        edits,
    };
    pendingSubmission = submission;
    el.modalBranch.textContent = state.branch;
    el.modalSummary.innerHTML =
        `Editing <strong>${shas.length}</strong> message${shas.length > 1 ? "s" : ""}:<ul>` +
        shas
            .map((sha) => {
                const subj = edits[sha].split("\n")[0] || "(empty)";
                return `<li><code>${escapeHTML(state.meta.get(sha).short)}</code> → ${escapeHTML(subj)}</li>`;
            })
            .join("") +
        "</ul>";
    focusBeforeModal = document.activeElement;
    el.modalImpact.innerHTML = '<span class="muted">Calculating impact…</span>';
    el.modalWarnings.innerHTML = "";
    el.modalOverlay.hidden = false;
    el.modalConfirm.disabled = true;
    el.modalConfirm.textContent = "Calculating…";
    el.modalCancel.focus();

    try {
        // Previewed and saved from the same object, so what the dialog shows
        // is computed from exactly what Confirm would send.
        const prev = await postJSON("/api/preview", submission);
        if (generation !== previewGeneration) return;
        el.modalImpact.innerHTML = renderImpact(prev, shas.length);
        el.modalWarnings.innerHTML = renderModalWarnings(prev);
        el.modalConfirm.disabled = false;
        el.modalConfirm.textContent = "Rewrite messages";
    } catch (err) {
        if (generation !== previewGeneration) return;
        // A failed preview means the blast radius is unknown — and the blast
        // radius is precisely what this dialog asks the user to consent to.
        // Leave the confirm button disabled rather than letting them agree to
        // something neither of us can describe.
        el.modalImpact.innerHTML =
            `<span class="err-text">Could not compute impact: ${escapeHTML(err.message)}</span>`;
        el.modalWarnings.innerHTML =
            `<div class="modal-warn">Nothing has been changed. Close this dialog, reload ` +
            `commits and try again.</div>`;
        el.modalConfirm.textContent = "Cannot rewrite";
    }
}

function renderImpact(prev, editedCount) {
    const total = prev.rewritten_count;
    const changed = prev.message_change_count;
    const descendants = total - changed;
    let s = `You edited <strong>${editedCount}</strong> message${editedCount > 1 ? "s" : ""}. ` +
        `This rewrites <strong>${total}</strong> commit${total > 1 ? "s" : ""} in total`;
    if (descendants > 0)
        s += `, including <strong>${descendants}</strong> descendant${descendants > 1 ? "s" : ""} ` +
            `whose message is unchanged (their hash changes because a parent changed).`;
    else s += ".";
    return s;
}

function renderModalWarnings(prev) {
    const allShas = prev.rewritten.map((r) => r.old);
    const warns = [];
    const signed = allShas.filter((s) => state.meta.get(s)?.signed).length;
    if (signed)
        warns.push(`${signed} of these commits ${signed > 1 ? "are" : "is"} signed — the signature${signed > 1 ? "s" : ""} will be lost.`);
    const pushed = allShas.filter((s) => state.meta.get(s)?.pushed).length;
    if (pushed)
        warns.push(
            `${pushed} of these commits ${pushed > 1 ? "are" : "is"} already on a remote — ` +
            `saving will require a force-push (${cmd("git push --force-with-lease")}).`
        );
    const st = state.statePayload || {};
    if (st.other_worktrees && st.other_worktrees.length)
        warns.push(
            `Branch is checked out in another worktree ` +
            `(${st.other_worktrees.map(escapeHTML).join(", ")}); its HEAD will move.`
        );
    return warns.length
        ? '<ul class="warn-list">' + warns.map((w) => `<li>${w}</li>`).join("") + "</ul>"
        : "";
}

let saving = false; // true between POST /api/save and its response

/** Close the dialog and put focus back where it was — or on `focus` when the
 *  caller knows better — so a keyboard user is not dumped at the top of the
 *  document. User gestures (Escape, the overlay, Cancel) must not dismiss a
 *  rewrite already in flight; save() passes force when the request settles. */
function closeModal({ force = false, focus = null } = {}) {
    if (saving && !force) return;
    previewGeneration += 1; // a preview still in flight is for a dialog that no longer exists
    pendingSubmission = null;
    el.modalOverlay.hidden = true;
    const prev = focusBeforeModal;
    focusBeforeModal = null;
    const target = focus || (prev && document.contains(prev) && !prev.disabled ? prev : el.commitList);
    target.focus();
}

async function save() {
    const submission = pendingSubmission;
    if (!submission) return;
    el.modalConfirm.disabled = true;
    el.modalCancel.disabled = true; // no cancelling mid-flight
    saving = true;
    const scrollY = window.scrollY;
    // Everything below reads from the snapshot, never from `state`: the undo
    // command and log entry describe THIS save even if the page moves on
    // before the response arrives.
    const { repo, edits } = submission;
    try {
        const data = await postJSON("/api/save", submission);
        // The trigger is about to be re-rendered, so focus goes to the list
        // itself rather than to an element that will not exist.
        closeModal({ force: true, focus: el.commitList });
        if (!data.changed) {
            notify("Nothing to do", "No effective changes were detected.", "ok", TOAST_MS, repo);
        } else {
            const undo = cmd(
                `git -C ${shellQuote(repo)} update-ref ${shellQuote("refs/heads/" + data.branch)} ${data.old_tip}`
            );
            const backup = data.backup_ref
                ? `<br>Backup ref: <code>${escapeHTML(data.backup_ref)}</code>`
                : "";
            const n = data.rewritten_count;
            notify(
                "Messages rewritten ✓",
                `Rewrote <code>${n}</code> commit${n === 1 ? "" : "s"} on ` +
                    `<code>${escapeHTML(data.branch)}</code>.<br>` +
                    `Tip <code>${escapeHTML(data.old_tip_short)}</code> → ` +
                    `<code>${escapeHTML(data.new_tip_short)}</code>.${backup}<br>Undo: ${undo}`,
                "ok",
                TOAST_SUCCESS_MS,
                repo
            );
        }
        // Clear edits before reload so a failed reload can't trip beforeunload
        // against now-stale SHAs. Drop exactly the drafts that were submitted
        // and acknowledged — including on a `changed: false` response, since
        // server-side wrapping can make a visibly-different message a no-op.
        state.order.forEach((sha) => {
            state.current.set(sha, state.original.get(sha));
            state.raw.set(sha, state.original.get(sha));
        });
        drafts.clearSubmitted(edits);
        await loadCommits();
        loadBackups(); // a save just created one
        window.scrollTo(0, scrollY);
    } catch (err) {
        // Keep the drafts: the rewrite may in fact have succeeded and only the
        // response been lost, and either way the typed text must not vanish.
        closeModal({ force: true });
        notify("Save failed", escapeHTML(err.message), "error", 0, repo);
    } finally {
        saving = false;
        el.modalCancel.disabled = false;
    }
}


// ---- theme ------------------------------------------------------------------
// Dark / Light / System. "System" leaves the attribute off so the CSS falls
// through to `color-scheme` and the media query; an explicit choice stamps
// data-theme on <html>, which is what the light token block keys on.
const THEMES = ["system", "dark", "light"];
let themeIndex = 0;

function applyTheme(name, announceIt = false) {
    if (name === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", name);
    const icon = name === "light" ? ICON.sun : name === "dark" ? ICON.moon : ICON.monitor;
    el.themeIcon.innerHTML = icon;
    el.themeToggle.setAttribute("aria-label", `Colour theme: ${name}. Activate to change.`);
    el.themeToggle.title = `Colour theme: ${name}`;
    try {
        localStorage.setItem("git-commit-editor:theme", name);
    } catch {
        /* a theme preference is not worth an error */
    }
    if (announceIt) announce(`Colour theme: ${name}`);
}

function initTheme() {
    let saved = "system";
    try {
        saved = localStorage.getItem("git-commit-editor:theme") || "system";
    } catch {
        /* ignore */
    }
    themeIndex = Math.max(0, THEMES.indexOf(saved));
    applyTheme(THEMES[themeIndex]);
}

// ---- wiring -----------------------------------------------------------------

el.loadBranchesBtn.addEventListener("click", loadBranches);
el.loadCommitsBtn.addEventListener("click", () => loadCommits());
el.branchSelect.addEventListener("change", () => {
    if (!confirmLeavingUnsavedDrafts()) {
        el.branchSelect.value = state.branch;
        return;
    }
    applySuggestedBase(); // switching branch re-detects its base, then reloads
    loadCommits();
});
el.resetAllBtn.addEventListener("click", resetAll);
el.backupsRefresh.addEventListener("click", loadBackups);
el.themeToggle.addEventListener("click", () => {
    themeIndex = (themeIndex + 1) % THEMES.length;
    applyTheme(THEMES[themeIndex], true);
});
el.expandAll.addEventListener("click", () => {
    const openAll = !allEditorsOpen();
    el.commitList.querySelectorAll(".commit").forEach((card) =>
        setEditorOpen(card.dataset.sha, card, openAll)
    );
    announce(openAll ? "All editors expanded." : "All editors collapsed.");
});
el.filter.addEventListener("input", applyFilter);
el.filterEdited.addEventListener("change", applyFilter);
el.saveLogClear.addEventListener("click", () => {
    el.saveLogList.innerHTML = "";
    el.saveLog.hidden = true;
});
el.saveBtn.addEventListener("click", openModal);
el.modalCancel.addEventListener("click", () => closeModal());
el.modalConfirm.addEventListener("click", save);
el.modalOverlay.addEventListener("click", (e) => {
    if (e.target === el.modalOverlay) closeModal();
});
el.repoPath.addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadBranches();
});
[el.baseRef, el.limit].forEach((input) =>
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") loadCommits();
    })
);

// Keyboard: Cmd/Ctrl+S opens the save modal; Esc closes it; Cmd/Ctrl+Enter
// confirms; Tab is trapped within the modal.
window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        // Always swallowed, whatever the app's state: the browser's fallback
        // is "Save Page As", which is never what Cmd+S means in an editor.
        // With nothing to save it does what the disabled button does — nothing.
        e.preventDefault();
        if (!modalIsOpen() && !el.saveBtn.disabled) openModal();
        return;
    }
    if (!modalIsOpen()) return;
    if (e.key === "Escape") {
        e.preventDefault();
        closeModal();
    } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        if (!el.modalConfirm.disabled) {
            e.preventDefault();
            save();
        }
    } else if (e.key === "Tab") {
        const f = el.modalOverlay.querySelectorAll("button:not([disabled])");
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }
});

// Textareas are sized to their content, and that content re-wraps when the
// viewport does.
let resizeTimer = null;
window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
        el.commitList
            .querySelectorAll(".commit:not([hidden]) .editor-wrap:not([hidden]) textarea")
            .forEach(autoGrow);
    }, 100);
});

// Only nag when the text would actually be lost: once drafts are in
// localStorage a reload is harmless, and a dialog on every refresh trains
// people to dismiss it.
window.addEventListener("beforeunload", (e) => {
    if (!drafts.persistent) {
        e.preventDefault();
        e.returnValue = "";
    }
});

// The shortcut hints: ⌘ is right on macOS/iOS, wrong everywhere else.
const platform = navigator.userAgentData?.platform ?? navigator.platform;
const isApple = /mac|iphone|ipad/i.test(platform);
el.kbdHint.textContent = isApple ? "⌘S" : "Ctrl+S";
el.modalModKey.textContent = isApple ? "⌘" : "Ctrl";

el.limit.value = CONFIG.defaultLimit;
el.limit.max = CONFIG.maxCommits;
initTheme();
// Auto-load if a default repo was prefilled by the server.
if (el.repoPath.value.trim()) loadBranches();
