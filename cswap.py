"""Non-interactive wrapper around the ``cswap`` CLI.

Every claude-swap subcommand used here is non-interactive (its only TTY-gated
path is the bare-`cswap` TUI), and stdin is closed so a future prompt surfaces
as a fast failure instead of a hung hook.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

DEFAULT_COMMAND = "cswap"
DEFAULT_TIMEOUT_S = 25.0
# `cswap auto --once` exit codes, from `cswap auto --help`.
AUTO_SWITCHED = 0
AUTO_ERROR = 1
AUTO_NOTHING_TO_DO = 2
AUTO_BLOCKED = 3


class CswapError(RuntimeError):
    """A cswap invocation that did not produce a usable result."""

    def __init__(self, message: str, *, exit_code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload


@dataclass
class CswapResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    payload: Any = None
    documents: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def error_message(self) -> str:
        """The message cswap put in its JSON error envelope, else its stderr/stdout."""
        payload = self.payload if isinstance(self.payload, dict) else None
        if payload is not None:
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
            if isinstance(error, str) and error:
                return error
        return (self.stderr or self.stdout or f"cswap exited {self.exit_code}").strip()


def resolve_command(explicit: str | None = None) -> str | None:
    """Absolute path to cswap, or ``None`` when it is not installed."""
    candidate = (
        (explicit or "").strip()
        or os.environ.get("CLAUDE_SWAP_COMMAND", "").strip()
        or DEFAULT_COMMAND
    )
    found = shutil.which(candidate)
    if found:
        return found
    if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def _parse_json(text: str) -> tuple[Any, list]:
    """Parse a JSON document, else JSONL. Returns ``(payload, documents)``.

    ``list``/``switch`` emit one document; ``auto --json`` emits one object per
    line, so everything parsed is kept in ``documents`` either way.
    """
    stripped = text.strip()
    if not stripped:
        return None, []
    try:
        return json.loads(stripped), [json.loads(stripped)]
    except ValueError:
        pass
    documents = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            documents.append(json.loads(line))
        except ValueError:
            return None, []
    if not documents:
        return None, []
    # A single JSONL object is also the payload, so callers can treat it uniformly.
    return (documents[0] if len(documents) == 1 else None), documents


def call(
    args: list[str],
    *,
    command: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    stdin_text: str | None = None,
) -> CswapResult:
    """Run ``cswap <args>`` and return its result. Never raises for a non-zero exit.

    ``stdin_text`` answers a cswap confirmation prompt. Two management operations
    (``remove``, and ``add --slot`` onto a different account) call ``input()`` and
    have no ``--assume-yes`` CLI flag — the parameter exists only internally and the
    CLI never passes it. With stdin closed the prompt raises an unhandled EOFError
    (remove) or silently cancels (add), so these operations need the answer supplied.
    Callers must gate on their own explicit confirmation first.
    """
    exe = resolve_command(command)
    if exe is None:
        raise CswapError(
            "cswap is not installed — install claude-swap with `uv tool install claude-swap`"
        )
    try:
        # `input=` and `stdin=` are mutually exclusive, so pick one.
        stream = (
            {"stdin": subprocess.DEVNULL}
            if stdin_text is None
            else {"input": stdin_text}
        )
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=dict(os.environ),
            check=False,
            **stream,
        )
    except subprocess.TimeoutExpired as exc:
        raise CswapError(f"`cswap {' '.join(args)}` timed out after {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise CswapError(f"could not run cswap ({exe}): {exc}") from exc

    payload, documents = _parse_json(proc.stdout)
    return CswapResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        payload=payload,
        documents=documents,
    )


def _require_ok(result: CswapResult, what: str) -> CswapResult:
    if not result.ok:
        raise CswapError(result.error_message, exit_code=result.exit_code, payload=result.payload)
    return result


def list_accounts(*, command: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """``cswap list --json`` as a dict. Raises when cswap refuses."""
    result = _require_ok(
        call(["list", "--json"], command=command, timeout_s=timeout_s), "list"
    )
    if not isinstance(result.payload, dict):
        raise CswapError("`cswap list --json` returned no JSON account document")
    return result.payload


def switch_next(*, command: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Rotate to the next account that is not at its limit. Raises when cswap refuses."""
    result = _require_ok(
        call(
            ["switch", "--strategy", "next-available", "--json"],
            command=command,
            timeout_s=timeout_s,
        ),
        "switch",
    )
    return result.payload if isinstance(result.payload, dict) else {}


def auto_once(
    *, dry_run: bool = False, command: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
) -> CswapResult:
    """One ``cswap auto --once`` tick (exit code is the outcome, not an error signal)."""
    args = ["auto", "--once", "--json"]
    if dry_run:
        args.append("--dry-run")
    result = call(args, command=command, timeout_s=timeout_s)
    # 3 (blocked) and 2 (nothing to do) are outcomes; only a real error raises.
    if result.exit_code not in (AUTO_SWITCHED, AUTO_NOTHING_TO_DO, AUTO_BLOCKED):
        raise CswapError(result.error_message, exit_code=result.exit_code, payload=result.payload)
    return result


def manage(
    args: list[str],
    *,
    command: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    stdin_text: str | None = None,
) -> CswapResult:
    """Run an account-management subcommand (add/remove/disable/enable/alias)."""
    return _require_ok(
        call(args, command=command, timeout_s=timeout_s, stdin_text=stdin_text),
        args[0] if args else "manage",
    )
