"""Tool handlers. Each returns a JSON string, and never raises into the tool loop."""

from __future__ import annotations

import json
from typing import Any

try:  # package import under the Hermes loader
    from . import cswap
    from . import config as cfgmod
except ImportError:  # flat import path (tests, `hermes plugins doctor`)
    import cswap  # type: ignore
    import config as cfgmod  # type: ignore

_CTX: Any = None
_MAX_TEXT = 4000

_OUTCOMES = {
    cswap.AUTO_SWITCHED: "switched",
    cswap.AUTO_NOTHING_TO_DO: "nothing_to_do",
    cswap.AUTO_BLOCKED: "blocked_no_viable_account",
}


def install(ctx: Any) -> None:
    """Bind the PluginContext. Tool registration happens in ``register(ctx)``."""
    global _CTX
    _CTX = ctx


def _settings() -> dict:
    return cfgmod.settings(_CTX)


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "… (truncated)"


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    if isinstance(value, (int, float)):
        return value != 0
    return default


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _warnings(cfg: dict) -> list[str]:
    """Things the user must know before trusting anything this plugin reports."""
    warnings: list[str] = []
    if cswap.resolve_command(cfg["cswap_command"]) is None:
        warnings.append(
            "cswap is not installed — run `uv tool install claude-swap`, then `cswap add` "
            "once per Claude account."
        )
    conflict = cfgmod.config_dir_conflict()
    if conflict is not None:
        warnings.append(cfgmod.conflict_note(conflict))
    return warnings


def _error(action: str, exc: Exception, warnings: list[str] | None = None) -> str:
    payload: dict = {"ok": False, "action": action, "error": str(exc)}
    if warnings:
        payload["warnings"] = warnings
    return _dumps(payload)


def _pct(window: dict | None) -> str:
    if not isinstance(window, dict):
        return "?"
    value = window.get("pct")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return f"{value:.0f}%"


def _summary_line(row: dict) -> str:
    number = row.get("number")
    label = f"#{number} {row.get('email') or '?'}"
    if row.get("alias"):
        label += f" [{row['alias']}]"
    markers = []
    if row.get("active"):
        markers.append("active")
    if row.get("disabled"):
        markers.append("disabled")
    if markers:
        label += f" ({', '.join(markers)})"

    status = row.get("usageStatus")
    if status != "ok":
        return f"{label} — usage {status or 'unknown'}"

    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    parts = [f"5h {_pct(usage.get('fiveHour'))}"]
    if "sevenDay" in usage:
        parts.append(f"7d {_pct(usage.get('sevenDay'))}")
    else:
        # Absent, not unparsed: Claude Team seats report no account-wide weekly window
        # (the usage API returns `seven_day: null`), so there is simply nothing to show.
        # Saying so is more useful to the model than a bare "?".
        parts.append("7d n/a (no weekly window on this plan)")
    scoped = usage.get("scoped")
    if isinstance(scoped, list):
        for window in scoped:
            if isinstance(window, dict) and window.get("name"):
                parts.append(f"{window['name']} week {_pct(window)}")
    if isinstance(usage.get("spend"), dict):
        parts.append(f"credits {_pct(usage['spend'])}")
    line = f"{label} — " + ", ".join(parts)
    age = row.get("usageAgeSeconds")
    if isinstance(age, (int, float)) and age >= 90:
        line += f" (measured {age / 60:.0f}m ago)"
    if row.get("loginExpiresAt"):
        line += f" [login expires {row['loginExpiresAt']}]"
    return line


def claude_swap_accounts(args: dict | None = None, **kwargs: Any) -> str:
    """Read-only: every managed account with its 5h/7d limits and which one is active."""
    cfg = _settings()
    warnings = _warnings(cfg)
    try:
        payload = cswap.list_accounts(
            command=cfg["cswap_command"], timeout_s=cfg["timeout_seconds"]
        )
    except Exception as exc:
        return _error("list", exc, warnings)

    accounts = payload.get("accounts")
    accounts = accounts if isinstance(accounts, list) else []
    out: dict = {
        "ok": True,
        "activeAccountNumber": payload.get("activeAccountNumber"),
        "accountCount": len(accounts),
        "summary": [_summary_line(row) for row in accounts if isinstance(row, dict)],
        "accounts": accounts,
    }
    if payload.get("unclaimedCredentials"):
        out["unclaimedCredentials"] = payload["unclaimedCredentials"]
    if warnings:
        out["warnings"] = warnings
    if not accounts:
        out["note"] = (
            "No accounts are managed by claude-swap yet. The user must log into Claude Code "
            "with an account (`claude auth login`) and run `cswap add` once per account."
        )
    return _dumps(out)


def claude_swap_switch(args: dict | None = None, **kwargs: Any) -> str:
    """Switch the active Claude Code login (by target, by strategy, or plain rotation)."""
    args = args or {}
    cfg = _settings()
    target = _as_text(args.get("target"))
    strategy = _as_text(args.get("strategy")).lower()
    if target and strategy:
        return _error(
            "switch",
            ValueError("Pass either `target` or `strategy`, not both — cswap allows "
                       "--strategy only on a bare switch."),
        )
    if strategy and strategy not in ("next-available", "best"):
        return _error("switch", ValueError(f"Unknown strategy {strategy!r}"))

    argv = ["switch"]
    if target:
        argv.append(target)
    elif strategy:
        argv += ["--strategy", strategy]
    argv.append("--json")

    try:
        result = cswap.call(argv, command=cfg["cswap_command"], timeout_s=cfg["timeout_seconds"])
    except Exception as exc:
        return _error("switch", exc, _warnings(cfg))

    payload = result.payload if isinstance(result.payload, dict) else {}
    if not result.ok:
        return _dumps(
            {
                "ok": False,
                "action": "switch",
                "error": result.error_message,
                "exitCode": result.exit_code,
            }
        )
    out = {
        "ok": True,
        "switched": bool(payload.get("switched")),
        "reason": payload.get("reason"),
        "message": payload.get("message"),
        "from": (payload.get("from") or {}).get("email") if isinstance(payload.get("from"), dict) else None,
        "to": (payload.get("to") or {}).get("email") if isinstance(payload.get("to"), dict) else None,
    }
    if payload.get("warnings"):
        out["warnings"] = payload["warnings"]
    out["note"] = (
        "The new login is picked up by the next Claude request Hermes makes. If the target is "
        "'already-active', pick another account or use strategy 'next-available'."
    )
    return _dumps(out)


def claude_swap_auto(args: dict | None = None, **kwargs: Any) -> str:
    """One claude-swap auto-switch evaluation (threshold-based, cooldown-aware)."""
    args = args or {}
    cfg = _settings()
    dry_run = _as_bool(args.get("dry_run"), True)
    try:
        result = cswap.auto_once(
            dry_run=dry_run, command=cfg["cswap_command"], timeout_s=cfg["timeout_seconds"]
        )
    except Exception as exc:
        return _error("auto", exc, _warnings(cfg))
    return _dumps(
        {
            "ok": True,
            "dryRun": dry_run,
            "outcome": _OUTCOMES.get(result.exit_code, f"exit_{result.exit_code}"),
            "events": result.documents,
        }
    )


def _slot_occupant(cfg: dict, slot: int) -> str | None:
    """The email occupying ``slot``, or None when the slot is free / cannot be read.

    Read fresh rather than cached: the answer decides whether cswap is about to
    displace a stored login.
    """
    try:
        payload = cswap.list_accounts(
            command=cfg["cswap_command"], timeout_s=cfg["timeout_seconds"]
        )
    except Exception:
        # Unreadable store: do not claim the slot is free, but do not block the add
        # either — cswap's own identity check still protects the common refresh case.
        return None
    for row in payload.get("accounts") or []:
        if isinstance(row, dict) and row.get("number") == slot:
            email = row.get("email")
            return email if isinstance(email, str) and email else f"Account-{slot}"
    return None


def claude_swap_manage(args: dict | None = None, **kwargs: Any) -> str:
    """Add / remove / disable / enable / alias a stored Claude Code login."""
    args = args or {}
    cfg = _settings()
    action = _as_text(args.get("action")).lower()
    account = _as_text(args.get("account"))
    alias = _as_text(args.get("alias"))
    warnings = _warnings(cfg)

    argv: list[str]
    stdin_text: str | None = None
    if action == "add":
        argv = ["add"]
        slot = args.get("slot")
        slot_number: int | None = None
        if isinstance(slot, (int, float, str)) and not isinstance(slot, bool) and str(slot).strip():
            try:
                slot_number = int(slot)
            except (TypeError, ValueError):
                return _error("add", ValueError(f"`slot` must be a number, got {slot!r}"))
            argv += ["--slot", str(slot_number)]
        if alias:
            argv += ["--alias", alias]
        if slot_number is not None:
            # `cswap add --slot N` prompts before displacing a *different* account, and
            # with stdin closed it silently cancels. Decide here instead of guessing at
            # an answer, so the overwrite is an explicit, informed choice.
            occupied = _slot_occupant(cfg, slot_number)
            if occupied is not None and not _as_bool(args.get("confirm"), False):
                return _dumps(
                    {
                        "ok": False,
                        "action": "add",
                        "needsConfirmation": True,
                        "slot": slot_number,
                        "error": (
                            f"Slot {slot_number} is already occupied by {occupied}. Adding to an "
                            "occupied slot permanently replaces that account's stored login. "
                            "Omit `slot` to give the new account the next free slot (re-adding an "
                            "account you are logged into updates it in place), or pass "
                            "confirm=true to replace it."
                        ),
                    }
                )
            if occupied is not None:
                stdin_text = "y\n"
    elif action == "remove":
        if not account:
            return _error("manage", ValueError("`account` is required for remove"))
        if not _as_bool(args.get("confirm"), False):
            return _dumps(
                {
                    "ok": False,
                    "action": "remove",
                    "needsConfirmation": True,
                    "error": (
                        f"Refusing to remove '{account}' without confirm=true — this deletes the "
                        "stored login for that account. The user can re-add it later with "
                        "`claude auth login` + `cswap add`."
                    ),
                }
            )
        argv = ["remove", account]
        # `cswap remove` always prompts (its assume_yes has no CLI flag), and with stdin
        # closed that EOFError is unhandled. The user already confirmed above.
        stdin_text = "y\n"
    elif action in ("disable", "enable"):
        if not account:
            return _error("manage", ValueError(f"`account` is required for {action}"))
        argv = [action, account]
    elif action == "alias":
        if not account or not alias:
            return _error("manage", ValueError("`account` and `alias` are both required for alias"))
        argv = ["alias", account, alias]
    else:
        return _error(
            "manage",
            ValueError("action must be one of add, remove, disable, enable, alias"),
        )

    try:
        result = cswap.manage(
            argv,
            command=cfg["cswap_command"],
            timeout_s=cfg["timeout_seconds"],
            stdin_text=stdin_text,
        )
    except Exception as exc:
        return _error(action, exc, warnings)

    output = _clip(result.stdout or result.stderr)
    if action in ("remove", "add") and "Cancelled" in output:
        # cswap answered a prompt it did not expect an answer to — report the no-op as a failure
        # rather than a silent success.
        return _dumps(
            {
                "ok": False,
                "action": action,
                "command": "cswap " + " ".join(argv),
                "error": "cswap cancelled the operation without changing anything.",
                "output": output,
            }
        )

    out: dict = {
        "ok": True,
        "action": action,
        "command": "cswap " + " ".join(argv),
        "output": output,
    }
    if action == "add" and not out["output"]:
        out["note"] = (
            "`cswap add` registers the Claude account currently logged in to the Claude Code "
            "CLI. To add a *different* account, the user must first run `claude auth login` in a "
            "terminal (interactive browser flow) and then run this tool again."
        )
    if warnings:
        out["warnings"] = warnings
    try:
        fresh = cswap.list_accounts(command=cfg["cswap_command"], timeout_s=cfg["timeout_seconds"])
        rows = fresh.get("accounts")
        if isinstance(rows, list):
            out["summary"] = [_summary_line(row) for row in rows if isinstance(row, dict)]
            out["activeAccountNumber"] = fresh.get("activeAccountNumber")
    except Exception:
        pass
    return _dumps(out)
