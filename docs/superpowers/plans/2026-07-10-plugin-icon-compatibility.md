# Plugin Icon Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the TMDB Auto Subscribe icon render in a default MoviePilot installation without environment-specific allowlist changes.

**Architecture:** Keep the icon as a repository asset, but expose it through MoviePilot's already allowed `raw.githubusercontent.com` host. Keep market and installed-plugin metadata identical, and guard that contract in the existing preflight script.

**Tech Stack:** Python, JSON, MoviePilot v2 plugin metadata, pytest/preflight scripts, Docker Compose.

---

### Task 1: Add the failing icon compatibility check

**Files:**
- Modify: `tests/v2/tmdbautosubscribe/preflight.py`

- [ ] **Step 1: Require an allowed image host**

Add a check that parses `plugin_icon`, accepts `raw.githubusercontent.com` or
`github.com`, and fails for the current `cdn.jsdelivr.net` URL.

- [ ] **Step 2: Verify the check fails**

Run: `python tests/v2/tmdbautosubscribe/preflight.py`

Expected: non-zero exit with `plugin_icon_moviepilot_compatible` in `failed`.

### Task 2: Update plugin metadata

**Files:**
- Modify: `plugins.v2/tmdbautosubscribe/__init__.py`
- Modify: `package.v2.json`

- [ ] **Step 1: Use the immutable raw GitHub icon URL**

Set both icon fields to:

```text
https://raw.githubusercontent.com/01dmt/MoviePilot-Plugins/9b5ba8d3d0fe32ae34fb23a9b72b47d67ce2569d/icons/tmdbautosubscribe-256.png?v=1.0.5
```

- [ ] **Step 2: Bump and document the release**

Set both versions to `1.0.5` and add a `v1.0.5` history entry explaining the
MoviePilot image allowlist compatibility fix.

- [ ] **Step 3: Verify the focused check passes**

Run: `python tests/v2/tmdbautosubscribe/preflight.py`

Expected: exit 0 and `"ok": true`.

### Task 3: Run release verification and publish

**Files:**
- Synchronize the two modified release files from the development repository to
  this market repository.

- [ ] **Step 1: Run local verification**

Run:

```powershell
python -m compileall plugins.v2/tmdbautosubscribe tests/v2/tmdbautosubscribe
python tests/v2/tmdbautosubscribe/verify_stub.py
python tests/v2/tmdbautosubscribe/preflight.py
python tests/v2/tmdbautosubscribe/acceptance_all.py
```

Expected: all commands exit 0.

- [ ] **Step 2: Commit and push**

Commit the icon metadata and release history, then push `main` to
`https://github.com/01dmt/MoviePilot-Plugins`.

### Task 4: Deploy and verify the test environment

**Files:**
- Deploy: `/opt/mp-tmdb-test/plugins/tmdbautosubscribe/__init__.py`
- Deploy: `/opt/mp-tmdb-test/plugins/tmdbautosubscribe/README.md`

- [ ] **Step 1: Synchronize and restart**

Copy the release files to the test VM, remove the plugin `__pycache__`, compile
the plugin, and restart `moviepilot-tmdb-test`.

- [ ] **Step 2: Verify runtime state**

Confirm the container is healthy, the loaded source declares version `1.0.5`,
MoviePilot's URL safety check accepts the new URL, and fetching it returns a
non-empty PNG.
