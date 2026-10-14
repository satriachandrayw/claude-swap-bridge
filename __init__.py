"""claude-swap bridge — rotate Claude accounts on a rate limit, read per-account
plan limits, and manage stored Claude Code logins while running Hermes on the
Claude Subscription DirectSDK provider.

Registration only; the policy lives in ``guard``, the handlers in ``tools``.
"""

from __future__ import annotations

import logging
from typing import Any

try:  # package import under the Hermes loader
    from . import guard, schemas, tools
except ImportError:  # flat import path (tests, `hermes plugins doctor`)
    import guard  # type: ignore
    import schemas  # type: ignore
    import tools  # type: ignore

logger = logging.getLogger(__name__)

TOOLSET = "claude_swap"


def register(ctx: Any) -> None:
    """Wire the four tools and the rate-limit hook. Called once at startup."""
    guard.install(ctx)
    tools.install(ctx)

    ctx.register_tool(
        name=schemas.ACCOUNTS["name"],
        toolset=TOOLSET,
        schema=schemas.ACCOUNTS,
        handler=tools.claude_swap_accounts,
        description="List claude-swap accounts with 5h/7d limits and the active login",
        emoji="🔁",
    )
    ctx.register_tool(
        name=schemas.SWITCH["name"],
        toolset=TOOLSET,
        schema=schemas.SWITCH,
        handler=tools.claude_swap_switch,
        description="Switch the active Claude Code account",
        emoji="🔀",
    )
    ctx.register_tool(
        name=schemas.AUTO["name"],
        toolset=TOOLSET,
        schema=schemas.AUTO,
        handler=tools.claude_swap_auto,
        description="Run one claude-swap auto-switch evaluation",
        emoji="⏱️",
    )
    ctx.register_tool(
        name=schemas.MANAGE["name"],
        toolset=TOOLSET,
        schema=schemas.MANAGE,
        handler=tools.claude_swap_manage,
        description="Add, remove, disable, enable or alias a Claude Code login",
        emoji="👤",
    )

    logger.debug("claude-swap-bridge registered (%d tools + api_request_error hook)", len(schemas.ALL))
