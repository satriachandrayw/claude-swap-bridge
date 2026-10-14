"""Tool schemas — exactly what the model reads when deciding to call these tools."""

ACCOUNTS = {
    "name": "claude_swap_accounts",
    "description": (
        "List every Claude account claude-swap manages, with each account's 5-hour and 7-day "
        "limit utilization, reset times, alias, disabled state, and which one is the active "
        "Claude Code login. Use this for any question about Claude usage, remaining limits, rate "
        "limits, weekly quota, or which accounts are available. Read-only: it does not switch or "
        "change anything."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SWITCH = {
    "name": "claude_swap_switch",
    "description": (
        "Switch the active Claude Code login to another claude-swap account. The switch takes "
        "effect on the next Claude request Hermes makes. Use with no arguments to rotate to the "
        "next account, with `target` to select one account by number/email/alias, or with "
        "`strategy` to let claude-swap pick (next-available skips accounts that are at their "
        "limit; best picks the most remaining quota)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Account to activate: slot number, email address, or alias from "
                    "claude_swap_accounts. Mutually exclusive with `strategy`."
                ),
            },
            "strategy": {
                "type": "string",
                "enum": ["next-available", "best"],
                "description": (
                    "Let claude-swap choose instead of naming an account. 'next-available' "
                    "rotates to the next account not at its limit; 'best' picks the one with the "
                    "most quota left. Use this when the current account hit a rate limit."
                ),
            },
        },
        "required": [],
    },
}

AUTO = {
    "name": "claude_swap_auto",
    "description": (
        "Run one claude-swap auto-switch evaluation: it reads every account's usage and switches "
        "off the active account when its 5-hour or 7-day window has reached the configured "
        "threshold. Use `dry_run` to see what it would decide without switching. This is the same "
        "engine as the background cswap auto service, run once on demand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {
                "type": "boolean",
                "description": (
                    "true (default) reports the decision without switching; false performs the "
                    "switch. Pass false only when the user asked you to actually switch."
                ),
            }
        },
        "required": [],
    },
}

MANAGE = {
    "name": "claude_swap_manage",
    "description": (
        "Manage the stored Claude Code logins claude-swap keeps: add, remove, disable, enable, or "
        "alias an account. `add` registers whichever Claude account is currently logged in to the "
        "Claude Code CLI, so the user must first run `claude auth login` in a terminal for the new "
        "account — an interactive browser login this tool cannot perform. Omit `slot` to place the "
        "new account in the next free slot; re-adding an account you are logged into updates it in "
        "place. `remove` deletes an account's stored credentials and requires confirm=true. "
        "`disable` keeps the login but holds the account out of rotation; `enable` returns it. "
        "`alias` sets a short name usable in place of a number or email."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "disable", "enable", "alias"],
                "description": "Operation to perform.",
            },
            "account": {
                "type": "string",
                "description": (
                    "Target account for remove/disable/enable/alias: slot number, email, or "
                    "existing alias."
                ),
            },
            "alias": {
                "type": "string",
                "description": "New short alias when action is 'alias'.",
            },
            "slot": {
                "type": "integer",
                "description": (
                    "Slot number for action 'add'. Omit to use the next free slot. Adding to a "
                    "slot held by a different account replaces it and needs confirm=true."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Must be true to remove an account, or to put a new account into a slot that "
                    "is already occupied by a different one (both destroy a stored login)."
                ),
            },
        },
        "required": ["action"],
    },
}

ALL = (ACCOUNTS, SWITCH, AUTO, MANAGE)
