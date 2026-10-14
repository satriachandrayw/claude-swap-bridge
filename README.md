# claude-swap-bridge (Hermes plugin)

Rotates the Claude Code login with [claude-swap](https://github.com/realiti4/claude-swap)
while Hermes runs the **Claude Subscription DirectSDK (Experimental)** provider, reads
per-account plan limits, and manages stored logins.

## Disclosure

What this plugin does that is worth knowing before you install it:

- **It shells out to a third-party CLI you install yourself.** Every operation runs
  `cswap` (claude-swap) as a subprocess. This plugin contributes no Python dependency
  and makes **no network calls of its own**.
- **That CLI manages Claude Code OAuth credentials, and this plugin triggers it.**
  claude-swap reads and writes Claude Code's credential store (`~/.claude` or
  `CLAUDE_CONFIG_DIR`, and the macOS Keychain) and rotates between the accounts you
  registered with it. The plugin itself never reads, holds, refreshes, prints or
  parses a token: it passes an argv and reads stdout. But it does *cause* credential
  rotation, so understand that this is what you are installing. It never presents
  itself as another vendor's client and never touches Hermes' own credentials
  (`auth.json`, a provider sign-in, a `.env` file).
- **Network activity happens in `cswap`, not here.** claude-swap calls Anthropic's
  usage endpoint (`api.anthropic.com/api/oauth/usage`) with each account's token to
  read plan limits, and its token endpoint when refreshing an inactive account's
  expired access token. Switching accounts is local file state.
- **It runs commands on your machine.** `claude auth login`, `cswap add`, `cswap remove`,
  `cswap switch`, `cswap list`, `cswap auto`, `cswap alias`, `cswap disable`. cswap's
  `remove` permanently deletes a stored login, so that action sits behind an explicit
  `confirm=true` in the tool schema.
- **One long-running background process, if you opt in.** The bundled
  `cswap-autoswitch.service` is a systemd user unit for `cswap auto`, which polls
  plan limits and switches accounts on its own. Nothing starts it for you; the README
  shows how, and how to stop it.
- **An interactive browser login, if you use the bundled helper.** `add-account.sh`
  runs `claude auth login`, which opens a browser. It refuses to run without a TTY, so
  it cannot hang an unattended run.
- **It reads one setting that belongs to another plugin.** `claude_config_dir` from the
  Claude Subscription DirectSDK plugin's config entry, via `hermes_cli.config`. It reads
  that one string to detect a config-directory mismatch that would silently make
  switching useless, and touches nothing else. No credential is involved. This is the
  plugin's only import of a Hermes module.
- **It makes a live network request only when you ask it to.** The tools are all
  explicit calls. The one automatic action is the `api_request_error` hook, which runs
  on a rate-limit failure from the configured provider and nothing else.

## Install

From the Hermes plugin catalog (once listed):

```bash
hermes plugins install claude-swap-bridge
```

From this repository:

```bash
uv tool install claude-swap        # the cswap CLI this plugin drives
hermes plugins install satriachandrayw/claude-swap-bridge
hermes plugins enable claude-swap-bridge
```

`cswap` is an external dependency, not a Python one: the plugin declares it under
`external_dependencies` and reports the install hint instead of failing obscurely.

## Why this exists

Hermes' DirectSDK plugin spawns the `claude` CLI once per request and has no account
concept of its own:

- On a 429, core classifies `rate_limit` with `should_rotate_credential=True`, but
  `recover_with_credential_pool()` returns immediately when `agent._credential_pool is
  None` — which it always is for an `external_process` provider. So Hermes *wants* to
  rotate and structurally cannot.
- claude-swap already owns the credential store and knows every account's 5h/7d usage.

This plugin is the seam between them. Because the DirectSDK plugin re-spawns `claude`
per request (and per retry), swapping the stored credential is enough: the retry Hermes
already performs lands on the new account.

## What it registers

**Tools** (toolset `claude_swap`):

| Tool | Purpose |
|---|---|
| `claude_swap_accounts` | Every account with 5h/7d (+per-model) utilization, reset times, alias, disabled state, and which one is active. Read-only. |
| `claude_swap_switch` | Switch login: no args = rotate, `target` = a specific account, `strategy` = `next-available` / `best`. |
| `claude_swap_auto` | One `cswap auto --once` evaluation. `dry_run` defaults to **true**. |
| `claude_swap_manage` | `add` / `remove` / `disable` / `enable` / `alias`. `remove` needs `confirm: true`. |

**Hook:** `api_request_error` (observer, bounded at 30s by Hermes).

On a configured status (default `429`) from the configured provider, it runs
`cswap switch --strategy next-available` and returns `None`, leaving Hermes' own
retry-then-fallback behaviour untouched. The next retry re-spawns `claude`, which reads
the credential that was just swapped in.

Guards, in order:

| Guard | Default | Why |
|---|---|---|
| provider match | `claude-subscription-directsdk-experimental` | Never react to another provider's 429. |
| status match | `{429}` | 429 is the DirectSDK plugin's "plan or session limit". 529 (overloaded) is provider-wide — switching accounts does not help. |
| config-dir conflict | — | See below. |
| per-turn budget | 1 | Prevents several switches during one turn's retries. |
| cooldown | 120s | `switch --strategy` bypasses claude-swap's own autoswitch cooldown, so this one is ours. Counts *attempts*, so a failing cswap is not hammered once per retry. |

## Configuration

```bash
hermes config set plugins.entries.claude-swap-bridge.settings.cooldown_seconds 300
hermes config set plugins.entries.claude-swap-bridge.settings.statuses '[429, 403]'
hermes config set plugins.entries.claude-swap-bridge.settings.switch_on_rate_limit false
```

Full key list is in `plugin.yaml` under `config_schema` (`provider`, `statuses`,
`cooldown_seconds`, `max_switches_per_turn`, `cswap_command`, `timeout_seconds`).

## The config-directory trap

The DirectSDK plugin can be pointed at a different Claude config directory via its
per-profile `claude_config_dir` setting or `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR`.
claude-swap swaps the credential in `CLAUDE_CONFIG_DIR`/`~/.claude`, **not** that
directory — so a switch would rotate an account the spawned `claude` never reads.
The bridge detects this and refuses to switch, reporting the conflict in every tool
result. A plain inherited `CLAUDE_CONFIG_DIR` is not a conflict: claude-swap reads the
same variable, so both sides move together.

## Adding accounts

Run once per account, in a real terminal:

```bash
~/.hermes/plugins/claude-swap-bridge/add-account.sh you@example.com [alias]
```

It logs in, confirms the login landed, snapshots it into claude-swap, and prints the
result. A script rather than raw commands because `claude auth login` overwrites
`~/.claude/.credentials.json` — if the `cswap add` that follows is skipped, the account
you just logged into is silently lost.

Two things that bite in practice:

- **A browser already signed in will approve the wrong account.** `--email` only
  pre-populates the login page; the claude.ai session cookie decides. Use a private
  window or a separate browser profile per account.
- **Never run `/logout`.** It revokes the refresh token claude-swap has stored for the
  account you are leaving. To add another account, just log in as it.

## Proactive switching (recommended, and the two work together)

Reactive switching only fires *after* a 429. Run claude-swap's own auto-switcher to
rotate before the limit is reached; the plugin then handles whatever slips through.

```bash
mkdir -p ~/.config/systemd/user
cp cswap-autoswitch.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cswap-autoswitch
journalctl --user -u cswap-autoswitch -f      # JSONL decision log
```

Tune it in claude-swap, not here: `cswap config set autoswitch.threshold 80`.

## Tool Search and these four tools

Hermes defers plugin/MCP tools behind `tool_search`/`tool_describe`/`tool_call` once the
deferrable schema surface exceeds a share of the context window. That is Hermes' normal
progressive-disclosure behaviour, and these tools stay reachable through it. To keep
them directly listed instead:

```bash
hermes config set tools.tool_search.enabled off
```

## Verify

```bash
# The plugin's own suite: pure stdlib + pytest, no Hermes checkout needed.
uv run --no-project --with pytest pytest -q tests

# Hermes' plugin check, and the catalog admission gate (security scan, capability
# declaration, no-core-override): the same command catalog CI runs.
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

Developed and verified against Hermes Agent **0.21.7** (`0.21.7+144.g3f2abce`) on Linux,
with claude-swap **0.26.0**. `requires_hermes` is `>=0.21.4`: the hook and provider
surfaces it uses landed in that release.

## Known limitations

- **`/usage` still shows one account.** The DirectSDK plugin implements
  `fetch_account_usage` itself, and built-in usage fetchers take precedence over any
  plugin hook — so this plugin cannot make Hermes' `/usage` multi-account. Ask the
  agent to call `claude_swap_accounts` instead (same data as `cswap list --json`).
- **Interactive logins are interactive.** `add` registers the account already logged
  into the CLI; a new account needs `claude auth login` in a terminal first.
- **Accounts and turns can race.** cswap takes Claude Code's credential locks. With
  parallel Hermes requests, a switch may wait on a lock; the 30s hook budget and the
  `timeout_seconds` setting bound that.
- **A real Anthropic 429 has not been exercised.** Both switching paths are tested
  against the real `cswap` CLI — including a real switch, and the reactive hook fed a
  synthetic 429 with the status code and provider string the DirectSDK plugin produces —
  but a rate limit produced by Anthropic itself is not something a test can request.
- **Team seats report no account-wide weekly window.** The usage API returns
  `seven_day: null` for them, so the only weekly limit that can gate a switch is a
  per-model one (e.g. Fable). Set `autoswitch.model` to cover it — see
  [Configuration](#configuration).
