#!/usr/bin/env bash
# ⚠️ DESTRUCTIVE — rewrites git history. Read before running.
#
# Purpose: scrub the previously-leaked API keys from the ENTIRE git history of
# this repo (they were hardcoded in source and committed). This repo's code
# now reads these keys from environment variables, so this only removes the
# historical copies.
#
# IMPORTANT: The patterns in LEAK_PATTERNS below are HISTORICAL-CLEANUP
# match strings ONLY (git-filter-repo --replace-text). They are NOT runtime
# secrets — running code reads keys from .env / environment variables. They
# live here solely so the script can locate & scrub old committed copies.
#
# Leaked patterns being scrubbed (substrings matched & replaced):
#   - Pexels key  : IupPtrTI2BG3nKYHvuSBDpZmL4bX48FvCY3HxV4BiN1BvgtQhRJM1to3
#   - Pixabay key : 46539967-a28550dda0098c01b6a752b00
#   - AnySearch   : as_sk_688a742d1ce960add6350d87f2adf1ac
#
# PRECONDITIONS (do all of these first):
#   1. Make sure the NEW keys are already set via .env / environment and the
#      running pipeline works WITHOUT the old hardcoded keys.
#   2. git-filter-repo must be installed:  brew install git-filter-repo
#   3. Coordinate with any collaborators — this rewrites history; everyone must
#      re-clone afterwards. Force-push will be required.
#   4. Take a full backup:  git bundle create /tmp/content-hub-backup.bundle --all
#
# SAFETY GATES (R5 hardening):
#   - Default mode is --dry-run: prints what WOULD be scrubbed, does nothing.
#   - Pass --yes to actually rewrite history (after you have verified a backup
#     bundle exists). There is NO --force-push — that is left to you, manually,
#     after you verify the result.
#
# RUN ONLY AFTER the above. It will NOT auto-push.

set -euo pipefail

cd "$(dirname "$0")"

# ── Historical-cleanup patterns ONLY (NOT runtime secrets) ──
LEAK_PATTERNS=(
  'IupPtrTI2BG3nKYHvuSBDpZmL4bX48FvCY3HxV4BiN1BvgtQhRJM1to3==>***REMOVED***'
  '46539967-a28550dda0098c01b6a752b00==>***REMOVED***'
  'as_sk_688a742d1ce960add6350d87f2adf1ac==>***REMOVED***'
)

DRY_RUN=1
if [ "${1:-}" = "--yes" ]; then
  DRY_RUN=0
elif [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
else
  echo "⚠️ This script rewrites git history (destructive)."
  echo "   Default is --dry-run (preview only). Use --yes to actually run."
  echo "   Usage: $0 [--dry-run|--yes]"
  exit 1
fi

BUNDLE=/tmp/content-hub-backup.bundle

# DRY-RUN: pure preview, does not require git-filter-repo to be installed.
if [ "$DRY_RUN" -eq 1 ]; then
  echo "=== DRY-RUN (no history rewritten) ==="
  echo "Would scrub these leaked-key substrings across ALL commits:"
  printf '  - %s\n' "${LEAK_PATTERNS[@]%%==>*}"
  echo ""
  echo "Pre-flight would create backup bundle at: $BUNDLE"
  echo "Verify current leaks in history with:"
  echo "  git log -p | grep -c 'IupPtr\|46539967\|as_sk_688a'   # expect 0 after run"
  echo ""
  echo "Re-run with --yes to actually rewrite history (after coordinating w/ collaborators)."
  exit 0
fi

# ── Actual (--yes) run ──
# Detect git-filter-repo; fail loudly if missing (no silent breakage).
if ! command -v git-filter-repo >/dev/null 2>&1; then
  echo "❌ git-filter-repo is not installed. Install it first:"
  echo "     brew install git-filter-repo"
  exit 1
fi

echo "=== Pre-flight backup ==="
git bundle create "$BUNDLE" --all
echo "Backup written to $BUNDLE  (KEEP THIS until you verify the rewrite)"

echo "=== Rewriting history (replace leaked keys) ==="
git filter-repo --replace-text <(printf '%s\n' "${LEAK_PATTERNS[@]}")

echo "=== Done. Verify no leaks remain in history: ==="
echo "  git log -p | grep -c 'IupPtr\|46539967\|as_sk_688a'   # expect 0"
echo ""
echo "=== Backups & next steps ==="
echo "  Bundle : $BUNDLE"
echo "  Force-push is NOT done automatically. Only after you verify AND coordinate:"
echo "    git push --force --all origin"
echo "    git push --force --tags origin"
