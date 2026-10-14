"""The rate-limit reaction: one pure decision function plus the hook that applies it.

``evaluate`` has no imports beyond the standard library and no side effects, so the
whole switching policy is testable without Hermes or a live cswap.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_STATE_ATTEMPT = "last_attempt_at"
_STATE_SWITCH = "last_switch_at"
_STATE_TURNS = "turn_switches"
_STATE_RECENT = "recent_switches"
_STATE_ERROR = "last_error"
_MAX_TRACKED_TURNS = 25
_MAX_RECENT = 20

_CTX: Any = None


def install(ctx: Any) -> None:
    """Bind the PluginContext and subscribe the hook. Called from ``register(ctx)``."""
    global _CTX
    _CTX = ctx
    ctx.register_hook("api_request_error", on_api_request_error)


def _no(reason: str, detail: str = "") -> dict:
    return {"switch": False, "reason": reason, "detail": detail}


def _count(value: Any) -> int:
    """A stored counter that may have been corrupted by hand; non-numeric reads as 0."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def evaluate(
    *,
    provider: str,
    status_code: Any,
    bucket: str,
    now: float,
    cfg: dict,
    last_attempt_at: float,
    turn_switches: dict,
    config_dir_conflict: dict | None = None,
) -> dict:
    """Decide whether this failed request should rotate the account.

    Returns ``{"switch": bool, "reason": str, "detail": str}``. The reason is the
    skipping rule when ``switch`` is false, so every non-switch is explainable in
    the debug log instead of silent.
    """
    if provider != cfg["provider"]:
        return _no("other-provider", f"provider={provider or '(none)'}")
    try:
        code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code not in cfg["statuses"]:
        return _no("status-not-configured", f"status={status_code!r}")

    if config_dir_conflict is not None:
        return _no("config-dir-conflict", str(config_dir_conflict.get("source", "")))

    if _count(turn_switches.get(bucket)) >= cfg["max_switches_per_turn"]:
        return _no("turn-budget-spent", f"bucket={bucket or '(empty)'}")

    cooldown = cfg["cooldown_seconds"]
    elapsed = now - float(last_attempt_at or 0.0)
    if last_attempt_at and elapsed < cooldown:
        return _no("cooldown", f"{elapsed:.0f}s of {cooldown}s elapsed")

    return {"switch": True, "reason": "rate-limit", "detail": f"HTTP {code} from {provider}"}


def _prune(mapping: dict, limit: int) -> dict:
    """Keep the newest ``limit`` entries of an insertion-ordered mapping."""
    if len(mapping) <= limit:
        return mapping
    return dict(list(mapping.items())[-limit:])


def on_api_request_error(
    provider: str = "",
    status_code: Any = None,
    turn_id: str = "",
    api_request_id: str = "",
    reason: str = "",
    retry_count: Any = None,
    max_retries: Any = None,
    model: str = "",
    **kwargs: Any,
) -> None:
    """Observer hook: rotate the Claude Code login before Hermes retries a rate limit.

    Returns ``None`` always — Hermes' own retry/fallback shape is left intact, and the
    retry that follows re-spawns ``claude``, which reads the credential just swapped in.
    """
    ctx = _CTX
    if ctx is None:
        return
    try:
        from . import cswap
        from . import config as cfgmod
    except ImportError:  # flat import path (tests)
        import cswap  # type: ignore
        import config as cfgmod  # type: ignore

    try:
        cfg = cfgmod.settings(ctx)
        if not cfg["switch_on_rate_limit"]:
            return None

        conflict = cfgmod.config_dir_conflict()
        state = ctx.state
        turn_switches = state.get(_STATE_TURNS, {}) or {}
        if not isinstance(turn_switches, dict):
            turn_switches = {}
        bucket = turn_id or api_request_id or "-"

        verdict = evaluate(
            provider=provider,
            status_code=status_code,
            bucket=bucket,
            now=time.time(),
            cfg=cfg,
            last_attempt_at=state.get(_STATE_ATTEMPT, 0.0) or 0.0,
            turn_switches=turn_switches,
            config_dir_conflict=conflict,
        )
        if not verdict["switch"]:
            logger.debug(
                "claude-swap-bridge: not switching (%s: %s)", verdict["reason"], verdict["detail"]
            )
            return None

        if conflict is not None:
            # Unreachable while evaluate() rejects the conflict, kept as a belt-and-braces guard.
            return None

        now = time.time()
        record: dict = {
            "at": now,
            "status": status_code,
            "reason": reason or "rate_limit",
            "model": model,
            "turn_id": bucket,
        }
        try:
            switch_payload = cswap.switch_next(
                command=cfg["cswap_command"], timeout_s=cfg["timeout_seconds"]
            )
            record["ok"] = True
            record["result"] = _summarize_switch(switch_payload)
            state.set(_STATE_SWITCH, now)
            logger.warning(
                "claude-swap-bridge: rate limit (HTTP %s) — switched Claude account: %s",
                status_code,
                record["result"].get("detail") or record["result"].get("to") or "rotated",
            )
        except Exception as exc:  # CswapError and anything unexpected
            record["ok"] = False
            record["error"] = str(exc)
            state.set(_STATE_ERROR, str(exc))
            logger.warning("claude-swap-bridge: could not switch accounts: %s", exc)
        finally:
            # The cooldown counts attempts, not successes, so a failing cswap is not hammered
            # once per retry attempt.
            state.set(_STATE_ATTEMPT, now)
            turn_switches[bucket] = _count(turn_switches.get(bucket)) + 1
            state.set(_STATE_TURNS, _prune(turn_switches, _MAX_TRACKED_TURNS))
            recent = state.get(_STATE_RECENT, []) or []
            if not isinstance(recent, list):
                recent = []
            state.set(_STATE_RECENT, (recent + [record])[-_MAX_RECENT:])
        return None
    except Exception:
        # An observer hook must never break the turn.
        logger.warning("claude-swap-bridge: hook failed", exc_info=True)
        return None


def _ref_label(value: Any) -> Any:
    """A switch payload's ``from``/``to`` is ``{"number": int, "email": str}``."""
    if isinstance(value, dict):
        email = value.get("email")
        number = value.get("number")
        if isinstance(email, str) and email:
            return f"Account-{number} ({email})" if number is not None else email
        return number if number is not None else value
    return value


def _summarize_switch(payload: dict) -> dict:
    """Flatten the `cswap switch --json` document to what is worth showing."""
    if not isinstance(payload, dict):
        return {}
    summary: dict = {}
    for key in ("switched", "reason", "strategy", "message", "from", "to"):
        if key not in payload:
            continue
        value = _ref_label(payload[key]) if key in ("from", "to") else payload[key]
        if isinstance(value, (str, int, float, bool)):
            summary[key] = value
    target = payload.get("to")
    if isinstance(target, dict) and isinstance(target.get("email"), str):
        summary["detail"] = f"now on {target['email']}"
    return summary
