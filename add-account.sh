#!/usr/bin/env bash
# add-account.sh — add one Claude account to claude-swap, safely.
#
#   add-account.sh <email> [alias]
#
# Logs into Claude Code as <email>, snapshots that login into claude-swap, and shows
# the result. Run it once per account, in a real terminal.
#
# Why a script: `claude auth login` overwrites ~/.claude/.credentials.json with the new
# account, so the *snapshot* step must follow it every time. Forgetting `cswap add`
# silently loses the account you just logged in with.
set -euo pipefail

die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

email="${1:-}"
alias_name="${2:-}"

[ -n "$email" ] || die "usage: add-account.sh <email> [alias]"
case "$email" in *@*.*) ;; *) die "'$email' does not look like an email address" ;; esac

# The browser flow and the paste-the-code step both need a TTY.
[ -t 0 ] && [ -t 1 ] || die "run this in a real terminal (needs a TTY for the login flow)"

command -v claude >/dev/null 2>&1 || die "'claude' not found on PATH"
command -v cswap  >/dev/null 2>&1 || die "'cswap' not found — install with: uv tool install claude-swap"

count_accounts() {
  cswap list --json 2>/dev/null \
    | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("accounts") or []))' 2>/dev/null \
    || echo "?"
}

before="$(count_accounts)"
printf 'Managed accounts before: %s\n\n' "$before"

cat <<EOF
>>> Step 1/3: logging in as $email
    A browser window (or URL) will open. Complete it as $email.

    NOTE: do not run '/logout' inside Claude Code at any point — it revokes the
    refresh token that claude-swap has stored for the account you are leaving.
EOF
printf '\n'

if ! claude auth login --email "$email"; then
  die "login failed or was cancelled — nothing was changed"
fi

printf '\n>>> Step 2/3: confirming the login landed\n'
if status="$(claude auth status 2>&1)"; then
  printf '%s\n' "$status" | sed 's/^/    /'
else
  printf '    (claude auth status failed; continuing)\n'
fi

printf '\n>>> Step 3/3: snapshotting this login into claude-swap\n'
# stdin is closed: `cswap add` only prompts when a slot would be displaced, which
# cannot happen here (no --slot), so a prompt would mean something unexpected.
if [ -n "$alias_name" ]; then
  cswap add --alias "$alias_name" </dev/null
else
  cswap add </dev/null
fi

after="$(count_accounts)"
printf '\nManaged accounts after:  %s\n\n' "$after"

if [ "$before" != "?" ] && [ "$after" != "?" ] && [ "$after" -le "$before" ]; then
  printf 'NOTE: the account count did not grow.\n'
  printf 'If you re-logged into an account claude-swap already had, that is expected\n'
  printf '(its stored credentials were refreshed in place).\n\n'
fi

cswap list </dev/null

cat <<EOF

Done. Next account: run this script again with a different email.
When all accounts are added, switch off the exhausted one:
    cswap switch --strategy best
EOF
