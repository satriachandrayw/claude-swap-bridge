"""Plugin settings plus the cross-plugin safety checks this bridge depends on.

The one non-obvious hazard: the DirectSDK plugin can be told to run Claude Code
against a *different* config directory (its per-profile ``claude_config_dir``
setting, or ``CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR``). claude-swap swaps the
credential in the config home it resolves from ``CLAUDE_CONFIG_DIR``/``~/.claude``,
which is not that directory — so a switch would rotate an account the spawned
``claude`` never reads. Detecting that is cheaper than debugging it.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_PROVIDER = "claude-subscription-directsdk-experimental"
DIRECTSDK_PLUGIN_ID = "claude-subscription-directsdk-experimental"
CONFIG_DIR_SETTING = "claude_config_dir"
CONFIG_DIR_ENV = "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"
PLAIN_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

DEFAULT_TIMEOUT_S = 25.0
DEFAULT_COOLDOWN_S = 120


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    if isinstance(value, (int, float)):
        return value != 0
    return default


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _as_float(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def settings(ctx: Any = None) -> dict:
    """Resolve this plugin's effective settings, with hostile values coerced away."""
    get = ctx.get_config if ctx is not None else lambda key, default=None: default

    raw_statuses = get("statuses", [429])
    if isinstance(raw_statuses, str):
        # `hermes config set ... statuses '429, 403'` round-trips as a string.
        raw_statuses = [part for part in raw_statuses.replace("[", " ").replace("]", " ").split(",")]
    elif not isinstance(raw_statuses, (list, tuple, set)):
        raw_statuses = [raw_statuses]
    statuses: set[int] = set()
    for item in raw_statuses:
        try:
            statuses.add(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    if not statuses:
        statuses = {429}

    command = get("cswap_command", "")
    return {
        "provider": (str(get("provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER)).strip()
        or DEFAULT_PROVIDER,
        "switch_on_rate_limit": _as_bool(get("switch_on_rate_limit", True), True),
        "statuses": statuses,
        "cooldown_seconds": _as_int(get("cooldown_seconds", DEFAULT_COOLDOWN_S), DEFAULT_COOLDOWN_S),
        "max_switches_per_turn": _as_int(get("max_switches_per_turn", 1), 1),
        "cswap_command": (command or "").strip() if isinstance(command, str) else "",
        # Capped below Hermes' 30s bounded-hook budget so the switch lands before the hook is abandoned.
        "timeout_seconds": min(
            _as_float(get("timeout_seconds", DEFAULT_TIMEOUT_S), DEFAULT_TIMEOUT_S, 1.0), 29.0
        ),
    }


def _directsdk_setting(key: str) -> str:
    """Read ``plugins.entries.<directsdk>.settings.<key>`` from the active profile."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return ""
    node: Any = config
    for segment in ("plugins", "entries", DIRECTSDK_PLUGIN_ID, "settings", key):
        if not isinstance(node, dict) or segment not in node:
            return ""
        node = node[segment]
    return node.strip() if isinstance(node, str) else ""


def config_dir_conflict() -> dict | None:
    """The DirectSDK child's config dir when it is set where claude-swap cannot follow.

    A plain inherited ``CLAUDE_CONFIG_DIR`` is *not* a conflict: claude-swap reads
    the same variable, so both sides move together. The two Hermes-only overrides
    are the problem, and only when they disagree with ``CLAUDE_CONFIG_DIR``
    (matching values still point at one directory).
    """
    plain = os.environ.get(PLAIN_CONFIG_DIR_ENV, "").strip()
    plain_expanded = os.path.expanduser(plain) if plain else ""

    setting = _directsdk_setting(CONFIG_DIR_SETTING)
    if setting:
        if plain_expanded and os.path.expanduser(setting) == plain_expanded:
            return None
        return {
            "source": f"plugins.entries.{DIRECTSDK_PLUGIN_ID}.settings.{CONFIG_DIR_SETTING}",
            "value": setting,
        }

    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        if plain_expanded and os.path.expanduser(override) == plain_expanded:
            return None
        return {"source": CONFIG_DIR_ENV, "value": override}

    return None


def conflict_note(conflict: dict) -> str:
    return (
        "claude-swap would switch the login in CLAUDE_CONFIG_DIR/~/.claude, but the Claude "
        f"Subscription DirectSDK plugin is pinned to a different config directory via "
        f"{conflict['source']} ({conflict['value']}). Switching cannot affect the account Hermes "
        "actually uses until that override is unset."
    )
