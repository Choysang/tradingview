#!/usr/bin/env bash
# Publish the current `main` tree to the public GitHub repository as a sanitized snapshot history.
# - Never pushes the local history (old commits carry server details): each publish is one new commit whose
#   parent is the previous publish (refs/heads/publish), built with `git commit-tree` from main's tree.
# - Refuses on a secret-scan hit or a file over 5 MB; never force-pushes (a rejected push means the remote moved).
# - Commits carry the repository owner's GitHub no-reply identity, not the local git e-mail.
# Usage: scripts/publish_github.sh ["commit message"]
set -euo pipefail
cd "$(dirname "$0")/.."
REMOTE=${PULSE_PUBLIC_REMOTE:-git@github.com:Choysang/tradingview.git}
REF=refs/heads/publish
MSG=${1:-"sync: $(git log -1 --format=%s main)"}
TREE=$(git rev-parse 'main^{tree}')

PATTERN='sk-(proj-)?[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE|43\.160\.[0-9]+\.[0-9]+|Administrator@[0-9]'
if git grep -nIE "$PATTERN" main -- . >/tmp/pulse_publish_scan.txt 2>/dev/null; then
  echo "secret scan hit, refusing to publish:"; cat /tmp/pulse_publish_scan.txt; exit 1
fi
BIG=$(git ls-tree -r -l main | awk '$4 != "-" && $4 > 5*1024*1024 {print $4, $5}')
if [ -n "$BIG" ]; then echo "files over 5 MB, refusing to publish:"; echo "$BIG"; exit 1; fi

PARENT=$(git rev-parse -q --verify "$REF" || true)
if [ -n "$PARENT" ] && [ "$(git rev-parse "$PARENT^{tree}")" = "$TREE" ]; then echo "nothing new to publish"; exit 0; fi
export GIT_AUTHOR_NAME=Choysang GIT_AUTHOR_EMAIL=65757025+Choysang@users.noreply.github.com
export GIT_COMMITTER_NAME=Choysang GIT_COMMITTER_EMAIL=65757025+Choysang@users.noreply.github.com
COMMIT=$(git commit-tree "$TREE" ${PARENT:+-p "$PARENT"} -m "$MSG")
GIT_SSH_COMMAND="ssh -o UserKnownHostsFile=$HOME/.ssh/known_hosts" git push "$REMOTE" "$COMMIT:refs/heads/main"
git update-ref "$REF" "$COMMIT"
echo "published $COMMIT ($MSG)"
