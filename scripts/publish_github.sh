#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
command -v gh >/dev/null 2>&1 || { printf 'Install GitHub CLI from https://cli.github.com and run gh auth login.\n'; exit 1; }
account=$(gh api user --jq .login | tr "[:upper:]" "[:lower:]")
[ "$account" = babak-ghadirzadeh ] || { printf 'Sign in to the intended GitHub account babak-ghadirzadeh first.\n'; exit 1; }
[ -z "$(git status --porcelain)" ] || { printf 'Commit the reviewed source changes before publication.\n'; exit 1; }
branch=$(git branch --show-current)
[ "$branch" = main ] || { printf 'Expected the reviewed main branch.\n'; exit 1; }
expected=https://github.com/babak-ghadirzadeh/ReviewGuard.git
if git remote get-url origin >/dev/null 2>&1; then
  current=$(git remote get-url origin)
  [ "$current" = "$expected" ] || { printf 'Unexpected origin. Review it before publishing.\n'; exit 1; }
else
  if ! gh repo view babak-ghadirzadeh/ReviewGuard >/dev/null 2>&1; then
    gh repo create babak-ghadirzadeh/ReviewGuard --public --description 'Open-source journal retraction surveillance with local evidence analysis and editorial assessment. Research alpha.'
  fi
  git remote add origin "$expected"
fi
gh auth setup-git
git push --set-upstream origin main
git push origin v0.3.0a1
gh repo view babak-ghadirzadeh/ReviewGuard --json url --jq .url
