#!/usr/bin/env bash
# publish.sh — automate the flow-doctor PyPI release sequence.
#
# Usage:
#   bash scripts/publish.sh            # full release (prompts before upload)
#   bash scripts/publish.sh --dry-run  # build + twine check, skip upload
#   bash scripts/publish.sh --yes      # full release, no confirm prompt
#
# What it does:
#   1. Verify working tree is clean and on main
#   2. Verify main is up to date with origin
#   3. Read VERSION from pyproject.toml
#   4. Verify flow_doctor/__init__.py matches pyproject.toml
#   5. Verify v$VERSION tag exists locally and on origin (create if missing)
#   6. Run the test suite (fail loud)
#   7. Clean stale build artifacts (dist/, build/, *.egg-info)
#   8. Run python -m build
#   9. Run python -m twine check dist/*
#  10. Prompt once for confirmation (unless --yes)
#  11. Run python -m twine upload dist/*
#  12. Verify 0.X.Y is live on PyPI via pip index
#  13. Create GitHub release (if tag push was new)
#
# Prerequisites:
#   - main branch up to date with origin
#   - v$VERSION tag already pushed, OR not yet created (script will create)
#   - CHANGELOG.md updated for the release
#   - ~/.pypirc configured OR twine will prompt interactively
#   - build + twine installed in .venv
#
# Exit codes:
#   0: success
#   1: precondition failure (dirty tree, wrong branch, version mismatch, etc.)
#   2: test failure
#   3: build failure
#   4: twine check failure
#   5: upload failure
#   6: verification failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY=".venv/bin/python"
DRY_RUN=false
SKIP_CONFIRM=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --yes|-y)  SKIP_CONFIRM=true ;;
    -h|--help)
      sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# ── Helpers ────────────────────────────────────────────────────────────────

step() { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "    \033[1;32m✓\033[0m %s\n" "$*"; }
fail() { printf "    \033[1;31m✗\033[0m %s\n" "$*" >&2; exit "${2:-1}"; }
info() { printf "    %s\n" "$*"; }

# ── 1. Working tree clean + on main ────────────────────────────────────────

step "Checking working tree state"
if [ -n "$(git status --porcelain)" ]; then
  git status --short
  fail "Working tree has uncommitted changes. Commit or stash before publishing." 1
fi
ok "Working tree clean"

CURRENT_BRANCH="$(git branch --show-current)"
if [ "$CURRENT_BRANCH" != "main" ]; then
  fail "Must be on main branch (current: $CURRENT_BRANCH)" 1
fi
ok "On main branch"

# ── 2. Up to date with origin ──────────────────────────────────────────────

step "Fetching origin/main"
git fetch origin main --quiet
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
  fail "Local main is not in sync with origin/main. Pull first. (local=$LOCAL remote=$REMOTE)" 1
fi
ok "main is up to date with origin"

# ── 3-4. Version sourced from pyproject.toml, verified against __init__.py ─

step "Reading version from pyproject.toml"
VERSION=$(grep -m1 '^version' pyproject.toml | sed -E 's/^version *= *"([^"]+)".*/\1/')
if [ -z "$VERSION" ]; then
  fail "Could not parse version from pyproject.toml" 1
fi
info "pyproject.toml version: $VERSION"

INIT_VERSION=$(grep -m1 '^__version__' flow_doctor/__init__.py | sed -E 's/^__version__ *= *"([^"]+)".*/\1/')
if [ "$INIT_VERSION" != "$VERSION" ]; then
  fail "Version mismatch: pyproject.toml=$VERSION but __init__.py=$INIT_VERSION. Fix one or the other." 1
fi
ok "Version $VERSION consistent across pyproject.toml and __init__.py"

TAG="v$VERSION"

# ── 5. Tag exists locally + on origin (create if missing) ──────────────────

step "Verifying tag $TAG"
if git rev-parse "$TAG" >/dev/null 2>&1; then
  TAG_COMMIT=$(git rev-list -n 1 "$TAG")
  if [ "$TAG_COMMIT" != "$LOCAL" ]; then
    fail "Tag $TAG points at $TAG_COMMIT but main HEAD is $LOCAL. Delete and re-tag, or rebase." 1
  fi
  ok "Tag $TAG exists locally at HEAD"
else
  info "Tag $TAG does not exist yet — creating"
  git tag -a "$TAG" -m "Release $TAG"
  ok "Created tag $TAG locally"
fi

if git ls-remote --tags origin | grep -q "refs/tags/$TAG$"; then
  ok "Tag $TAG exists on origin"
else
  info "Pushing tag $TAG to origin"
  git push origin "$TAG"
  ok "Pushed tag $TAG to origin"
fi

# ── 6. Run tests ────────────────────────────────────────────────────────────

step "Running test suite"
if ! $VENV_PY -m pytest tests/ -q 2>&1 | tail -20; then
  fail "Tests failed. Refusing to publish a broken release." 2
fi
ok "Tests passed"

# ── 7. Clean build artifacts ───────────────────────────────────────────────

step "Cleaning stale build artifacts"
rm -rf dist/ build/ *.egg-info flow_doctor.egg-info
ok "Cleaned dist/, build/, *.egg-info"

# ── 8. Build ────────────────────────────────────────────────────────────────

step "Building sdist and wheel"
if ! $VENV_PY -m build 2>&1 | tail -10; then
  fail "python -m build failed. Is 'build' installed in .venv? (.venv/bin/pip install --upgrade build)" 3
fi
ok "Build succeeded"
info "Artifacts:"
ls -la dist/ | tail -n +2 | awk '{print "      " $NF "  " $5 " bytes"}'

# Sanity: artifacts should contain the version in their filenames
if ! ls dist/ | grep -q "$VERSION"; then
  fail "dist/ does not contain files matching version $VERSION. Build produced unexpected output." 3
fi
ok "Artifact filenames contain version $VERSION"

# ── 9. twine check ─────────────────────────────────────────────────────────

step "Running twine check"
if ! $VENV_PY -m twine check dist/* 2>&1 | tail -10; then
  fail "twine check failed. Inspect README metadata and classifiers." 4
fi
ok "twine check passed"

if [ "$DRY_RUN" = true ]; then
  step "Dry run — stopping before upload"
  info "Artifacts built and validated at dist/"
  info "Run without --dry-run to upload to PyPI."
  exit 0
fi

# ── 10. Confirm upload ─────────────────────────────────────────────────────

if [ "$SKIP_CONFIRM" != true ]; then
  step "Ready to upload flow-doctor $VERSION to PyPI"
  printf "    Proceed? [y/N] "
  read -r REPLY
  if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
    info "Aborted by user. Artifacts remain in dist/ — re-run to upload later."
    exit 0
  fi
fi

# ── 11. Upload ─────────────────────────────────────────────────────────────

step "Uploading to PyPI"
if ! $VENV_PY -m twine upload dist/*; then
  fail "twine upload failed. Check ~/.pypirc credentials or interactive prompt." 5
fi
ok "Uploaded flow-doctor $VERSION to PyPI"

# ── 12. Verify on PyPI ─────────────────────────────────────────────────────

step "Verifying $VERSION is live on PyPI"
# PyPI index can take up to ~30s to reflect a new release; poll briefly.
for attempt in 1 2 3 4 5 6; do
  if $VENV_PY -m pip index versions flow-doctor 2>/dev/null | grep -q "$VERSION"; then
    ok "flow-doctor $VERSION visible via pip index"
    break
  fi
  if [ "$attempt" -eq 6 ]; then
    info "Warning: $VERSION not visible via pip index after 30s. PyPI may still be propagating — check https://pypi.org/project/flow-doctor/ manually."
  else
    sleep 5
  fi
done

# ── 13. GitHub release ─────────────────────────────────────────────────────

step "Creating GitHub release"
if command -v gh >/dev/null 2>&1; then
  if gh release view "$TAG" >/dev/null 2>&1; then
    info "GitHub release for $TAG already exists — skipping"
  else
    if gh release create "$TAG" --title "$TAG" --notes-from-tag; then
      ok "Created GitHub release for $TAG"
    else
      info "Warning: gh release create failed. Create manually at https://github.com/cipher813/flow-doctor/releases/new?tag=$TAG"
    fi
  fi
else
  info "gh CLI not found — skipping GitHub release step"
fi

printf "\n\033[1;32mRelease complete: flow-doctor %s\033[0m\n\n" "$VERSION"
printf "Next steps:\n"
printf "  1. Update consumers to pin the new version (alpha-engine, alpha-engine-backtester)\n"
printf "  2. Verify the release at https://pypi.org/project/flow-doctor/%s/\n" "$VERSION"
