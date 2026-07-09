#!/usr/bin/env bash
# ⚠️ DESTRUCTIVE — rewrites git history. Read before running.
#
# Purpose: scrub the previously-leaked API keys from the ENTIRE git history of
# this repo (they were hardcoded in source and committed). This repo's code
# now reads these keys from environment variables, so this only removes the
# historical copies.
#
# Leaked patterns (substrings matched & replaced):
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
# WHAT THIS DOES:
#   Replaces every occurrence of the leaked substrings with "***REMOVED***"
#   across all commits, then removes the now-empty refs/original backup.
#
# RUN ONLY AFTER the above. It will NOT run automatically.

set -euo pipefail

cd "$(cd "$(dirname "$0")" && pwd)/.."

echo "=== Pre-flight backup ==="
git bundle create /tmp/content-hub-backup.bundle --all
echo "Backup written to /tmp/content-hub-backup.bundle"

echo "=== Rewriting history (replace leaked keys) ==="
git filter-repo --replace-text <(printf '%s\n' \
  'IupPtrTI2BG3nKYHvuSBDpZmL4bX48FvCY3HxV4BiN1BvgtQhRJM1to3==>***REMOVED***' \
  '46539967-a28550dda0098c01b6a752b00==>***REMOVED***' \
  'as_sk_688a742d1ce960add6350d87f2adf1ac==>***REMOVED***' \
)

echo "=== Done. Verify no leaks remain in history: ==="
echo "  git log -p | grep -c 'IupPtr\|46539967\|as_sk_688a'   # expect 0"
echo ""
echo "=== If verified, force-push (requires coordination): ==="
echo "  git push --force --all origin"
echo "  git push --force --tags origin"
