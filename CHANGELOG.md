# Changelog

Each entry links the issue that holds the reasoning. Versions are also
published as [GitHub Releases](https://github.com/nmpowell/git-commit-editor/releases).

## Unreleased

### Fixed

- `Cmd/Ctrl+S` no longer reaches the browser's "Save Page As" dialog when
  there is nothing to save, no repository is loaded, or the confirmation
  dialog is already open. It is swallowed in every state and opens the
  dialog only when the Save button is enabled.
  ([#1](https://github.com/nmpowell/git-commit-editor/issues/1))
- The confirmation dialog's hint now reads `Ctrl+↵` rather than `⌘↵` on
  Windows and Linux, matching the save bar's `Ctrl+S` hint.
  ([#1](https://github.com/nmpowell/git-commit-editor/issues/1))

## 0.1.0 — 2026-09-18

First release. A local web app for rewording the commit messages on a git
branch: edit in the browser, save, and the branch is rewritten with authors,
dates and trees preserved and a backup branch left behind.
