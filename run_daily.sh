#!/bin/zsh
# Run by launchd every morning (see ~/Library/LaunchAgents/local.rent-comparables.daily.plist).
cd "$(dirname "$0")" || exit 1
echo "=== $(date '+%Y-%m-%d %H:%M') ==="
./.venv/bin/python -u tribunal_decisions.py || echo "(tribunal refresh failed, continuing)"
./.venv/bin/python -u openrent_comps.py daily
# Public showcase: rebuild the anonymised page, privacy-check it, upload to Cloudflare Pages if configured.
./.venv/bin/python -u public_site.py --deploy || echo "(public site step failed, continuing)"
