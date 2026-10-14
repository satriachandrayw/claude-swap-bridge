"""Tests for the claude-swap bridge.

No Hermes import is needed: ``guard.evaluate`` is a pure function, and the hook is
driven with a fake PluginContext and a stubbed cswap.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfgmod  # noqa: E402
import cswap  # noqa: E402
import guard  # noqa: E402

CFG = {
    "provider": "claude-subscription-directsdk-experimental",
    "switch_on_rate_limit": True,
    "statuses": {429},
    "cooldown_seconds": 120,
    "max_switches_per_turn": 1,
    "cswap_command": "",
    "timeout_seconds": 25.0,
}


def decide(**overrides):
    kwargs = {
        "provider": CFG["provider"],
        "status_code": 429,
        "bucket": "turn-1",
        "now": 1_000.0,
        "cfg": CFG,
        "last_attempt_at": 0.0,
        "turn_switches": {},
        "config_dir_conflict": None,
    }
    kwargs.update(overrides)
    return guard.evaluate(**kwargs)


class TestEvaluate:
    def test_switches_on_configured_status(self):
        assert decide()["switch"] is True

    def test_ignores_other_providers(self):
        verdict = decide(provider="openrouter")
        assert verdict["switch"] is False
        assert verdict["reason"] == "other-provider"

    def test_ignores_status_not_configured(self):
        for status in (500, 529, 401, None, "nonsense"):
            verdict = decide(status_code=status)
            assert verdict["switch"] is False, status
            assert verdict["reason"] == "status-not-configured"

    def test_cooldown_blocks_a_second_switch(self):
        verdict = decide(now=1_060.0, last_attempt_at=1_000.0)
        assert verdict["switch"] is False
        assert verdict["reason"] == "cooldown"

    def test_cooldown_expires(self):
        assert decide(now=1_121.0, last_attempt_at=1_000.0)["switch"] is True

    def test_turn_budget_blocks_repeat_switches_within_one_turn(self):
        verdict = decide(turn_switches={"turn-1": 1})
        assert verdict["switch"] is False
        assert verdict["reason"] == "turn-budget-spent"

    def test_turn_budget_is_per_turn(self):
        assert decide(bucket="turn-2", turn_switches={"turn-1": 5})["switch"] is True

    def test_config_dir_conflict_beats_everything(self):
        verdict = decide(config_dir_conflict={"source": "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"})
        assert verdict["switch"] is False
        assert verdict["reason"] == "config-dir-conflict"

    def test_corrupt_turn_map_does_not_crash(self):
        assert decide(turn_switches={"turn-1": "x"})["switch"] is True


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self, settings=None):
        self.state = FakeState()
        self._settings = settings or {}
        self.hooks = {}

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


@pytest.fixture
def ctx():
    fake = FakeCtx()
    guard.install(fake)
    return fake


@pytest.fixture
def switched(monkeypatch):
    calls = []

    def fake_switch_next(*, command=None, timeout_s=None):
        calls.append({"command": command, "timeout_s": timeout_s})
        return {
            "schemaVersion": 1,
            "switched": True,
            "from": {"number": 1, "email": "a@example.com"},
            "to": {"number": 2, "email": "b@example.com"},
            "strategy": "next-available",
            "reason": "switched",
            "message": "Switched to Account-2 (b@example.com)",
        }

    monkeypatch.setattr(guard_cswap(), "switch_next", fake_switch_next)
    return calls


def guard_cswap():
    """The cswap module object guard.py resolved at call time."""
    import cswap as module

    return module


class TestHook:
    def test_switches_and_records_state(self, ctx, switched):
        ctx.hooks["api_request_error"](
            provider=CFG["provider"], status_code=429, turn_id="t1", reason="rate_limit"
        )
        assert len(switched) == 1
        assert switched[0]["timeout_s"] == 25.0
        recent = ctx.state.get("recent_switches")
        assert recent and recent[-1]["ok"] is True
        assert recent[-1]["result"]["detail"] == "now on b@example.com"
        assert ctx.state.get("last_attempt_at") > 0

    def test_turn_budget_stops_a_second_switch(self, ctx, switched):
        for _ in range(3):
            ctx.hooks["api_request_error"](
                provider=CFG["provider"], status_code=429, turn_id="t1"
            )
        assert len(switched) == 1

    def test_cooldown_blocks_the_next_turn(self, ctx, switched):
        ctx.hooks["api_request_error"](provider=CFG["provider"], status_code=429, turn_id="t1")
        ctx.hooks["api_request_error"](provider=CFG["provider"], status_code=429, turn_id="t2")
        assert len(switched) == 1

    def test_other_status_does_nothing(self, ctx, switched):
        ctx.hooks["api_request_error"](provider=CFG["provider"], status_code=500, turn_id="t1")
        assert switched == []

    def test_other_provider_does_nothing(self, ctx, switched):
        ctx.hooks["api_request_error"](provider="openrouter", status_code=429, turn_id="t1")
        assert switched == []

    def test_disabled_setting_does_nothing(self, switched):
        fake = FakeCtx({"switch_on_rate_limit": False})
        guard.install(fake)
        fake.hooks["api_request_error"](provider=CFG["provider"], status_code=429, turn_id="t1")
        assert switched == []

    def test_cswap_failure_is_recorded_not_raised(self, ctx, monkeypatch):
        def boom(**kwargs):
            raise cswap.CswapError("no accounts are managed yet", exit_code=1)

        monkeypatch.setattr(guard_cswap(), "switch_next", boom)
        ctx.hooks["api_request_error"](provider=CFG["provider"], status_code=429, turn_id="t1")
        assert "no accounts are managed yet" in ctx.state.get("last_error")
        recent = ctx.state.get("recent_switches")
        assert recent[-1]["ok"] is False
        # A failed attempt still starts the cooldown, so cswap is not hammered per retry.
        assert ctx.state.get("last_attempt_at") > 0

    def test_hook_returns_none(self, ctx, switched):
        result = ctx.hooks["api_request_error"](
            provider=CFG["provider"], status_code=429, turn_id="t1"
        )
        assert result is None

    def test_unknown_kwargs_are_tolerated(self, ctx, switched):
        ctx.hooks["api_request_error"](
            provider=CFG["provider"],
            status_code=429,
            turn_id="t1",
            some_future_field={"a": 1},
            model="claude-opus-5-5[1m]",
        )
        assert len(switched) == 1


class TestConfig:
    def test_defaults(self):
        got = cfgmod.settings(FakeCtx())
        assert got["provider"] == cfgmod.DEFAULT_PROVIDER
        assert got["statuses"] == {429}
        assert got["cooldown_seconds"] == 120

    def test_statuses_accept_scalars_and_strings(self):
        assert cfgmod.settings(FakeCtx({"statuses": "429"}))["statuses"] == {429}
        assert cfgmod.settings(FakeCtx({"statuses": [429, "529", "junk"]}))["statuses"] == {429, 529}

    def test_statuses_accept_a_comma_separated_string(self):
        # What `hermes config set ... statuses '429, 403'` stores.
        assert cfgmod.settings(FakeCtx({"statuses": "429, 403"}))["statuses"] == {429, 403}
        assert cfgmod.settings(FakeCtx({"statuses": "[429, 403]"}))["statuses"] == {429, 403}

    def test_empty_statuses_falls_back(self):
        assert cfgmod.settings(FakeCtx({"statuses": []}))["statuses"] == {429}

    def test_timeout_is_capped_under_the_hook_budget(self):
        assert cfgmod.settings(FakeCtx({"timeout_seconds": 300}))["timeout_seconds"] == 29.0

    def test_garbage_values_fall_back(self):
        got = cfgmod.settings(
            FakeCtx({"cooldown_seconds": "soon", "max_switches_per_turn": -3, "timeout_seconds": None})
        )
        assert got["cooldown_seconds"] == cfgmod.DEFAULT_COOLDOWN_S
        assert got["max_switches_per_turn"] == 1
        assert got["timeout_seconds"] == cfgmod.DEFAULT_TIMEOUT_S

    def test_switch_on_rate_limit_parses_strings(self):
        assert cfgmod.settings(FakeCtx({"switch_on_rate_limit": "off"}))["switch_on_rate_limit"] is False
        assert cfgmod.settings(FakeCtx({"switch_on_rate_limit": "yes"}))["switch_on_rate_limit"] is True


class TestConfigDirConflict:
    def test_no_conflict_by_default(self, monkeypatch):
        monkeypatch.delenv(cfgmod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.delenv(cfgmod.PLAIN_CONFIG_DIR_ENV, raising=False)
        monkeypatch.setattr(cfgmod, "_directsdk_setting", lambda key: "")
        assert cfgmod.config_dir_conflict() is None

    def test_plugin_setting_is_a_conflict(self, monkeypatch):
        monkeypatch.delenv(cfgmod.PLAIN_CONFIG_DIR_ENV, raising=False)
        monkeypatch.setattr(cfgmod, "_directsdk_setting", lambda key: "/tmp/other-claude")
        conflict = cfgmod.config_dir_conflict()
        assert conflict is not None
        assert conflict["source"].endswith(cfgmod.CONFIG_DIR_SETTING)

    def test_env_override_is_a_conflict(self, monkeypatch):
        monkeypatch.delenv(cfgmod.PLAIN_CONFIG_DIR_ENV, raising=False)
        monkeypatch.setattr(cfgmod, "_directsdk_setting", lambda key: "")
        monkeypatch.setenv(cfgmod.CONFIG_DIR_ENV, "/tmp/other-claude")
        assert cfgmod.config_dir_conflict()["source"] == cfgmod.CONFIG_DIR_ENV

    def test_plain_claude_config_dir_alone_is_fine(self, monkeypatch):
        # claude-swap reads CLAUDE_CONFIG_DIR too, so both sides move together.
        monkeypatch.setattr(cfgmod, "_directsdk_setting", lambda key: "")
        monkeypatch.delenv(cfgmod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv(cfgmod.PLAIN_CONFIG_DIR_ENV, "/tmp/shared")
        assert cfgmod.config_dir_conflict() is None

    def test_override_matching_the_plain_dir_is_fine(self, monkeypatch):
        monkeypatch.setattr(cfgmod, "_directsdk_setting", lambda key: "/tmp/shared")
        monkeypatch.delenv(cfgmod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv(cfgmod.PLAIN_CONFIG_DIR_ENV, "/tmp/shared")
        assert cfgmod.config_dir_conflict() is None


class TestCswapParsing:
    def test_single_document(self):
        payload, documents = cswap._parse_json('{"a": 1}')
        assert payload == {"a": 1}
        assert documents == [{"a": 1}]

    def test_jsonl(self):
        text = '{"event": "poll"}\n{"event": "no-switch"}\n'
        payload, documents = cswap._parse_json(text)
        assert payload is None
        assert [d["event"] for d in documents] == ["poll", "no-switch"]

    def test_noise_is_not_parsed(self):
        assert cswap._parse_json("Error: something failed") == (None, [])

    def test_empty(self):
        assert cswap._parse_json("") == (None, [])

    def test_error_envelope_message(self):
        result = cswap.CswapResult(
            exit_code=1,
            payload={"schemaVersion": 1, "error": {"type": "ConfigError", "message": "nope"}},
        )
        assert result.error_message == "nope"

    def test_error_message_falls_back_to_stderr(self):
        assert cswap.CswapResult(exit_code=1, stderr="boom\n").error_message == "boom"

    def test_missing_cswap_raises(self, monkeypatch):
        monkeypatch.setattr(cswap.shutil, "which", lambda *a, **k: None)
        monkeypatch.setenv("PATH", "")
        with pytest.raises(cswap.CswapError, match="uv tool install"):
            cswap.call(["list", "--json"])

    def test_auto_once_treats_blocked_as_an_outcome(self, monkeypatch):
        monkeypatch.setattr(
            cswap,
            "call",
            lambda *a, **k: cswap.CswapResult(exit_code=3, stdout='{"event": "blocked"}\n'),
        )
        assert cswap.auto_once().exit_code == 3

    def test_auto_once_raises_on_error(self, monkeypatch):
        monkeypatch.setattr(
            cswap, "call", lambda *a, **k: cswap.CswapResult(exit_code=1, stderr="network trouble")
        )
        with pytest.raises(cswap.CswapError, match="network trouble"):
            cswap.auto_once()

    def test_call_closes_stdin_by_default(self, monkeypatch):
        seen = {}

        class Proc:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return Proc()

        monkeypatch.setattr(cswap.shutil, "which", lambda *a, **k: "/usr/bin/cswap")
        monkeypatch.setattr(cswap.subprocess, "run", fake_run)
        cswap.call(["list", "--json"])
        assert seen["stdin"] is cswap.subprocess.DEVNULL
        assert "input" not in seen

    def test_call_supplies_a_confirmation_when_asked(self, monkeypatch):
        seen = {}

        class Proc:
            returncode, stdout, stderr = 0, "", ""

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return Proc()

        monkeypatch.setattr(cswap.shutil, "which", lambda *a, **k: "/usr/bin/cswap")
        monkeypatch.setattr(cswap.subprocess, "run", fake_run)
        cswap.call(["remove", "2"], stdin_text="y\n")
        assert seen["input"] == "y\n"
        assert "stdin" not in seen


class TestTools:
    @pytest.fixture
    def tool_ctx(self, monkeypatch):
        import tools

        fake = FakeCtx()
        tools.install(fake)
        monkeypatch.setattr(cswap, "resolve_command", lambda explicit=None: "/usr/bin/cswap")
        return tools, fake

    def test_accounts_summarizes_rows(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        monkeypatch.setattr(
            cswap,
            "list_accounts",
            lambda **kwargs: {
                "schemaVersion": 1,
                "activeAccountNumber": 2,
                "accounts": [
                    {
                        "number": 1,
                        "email": "a@example.com",
                        "active": False,
                        "usageStatus": "ok",
                        "usage": {
                            "fiveHour": {"pct": 12.0},
                            "sevenDay": {"pct": 86.4},
                            "scoped": [{"name": "Fable", "pct": 100.0}],
                        },
                    },
                    {
                        "number": 2,
                        "email": "b@example.com",
                        "alias": "work",
                        "active": True,
                        "disabled": True,
                        "usageStatus": "relogin_required",
                    },
                ],
            },
        )
        out = json.loads(tools.claude_swap_accounts({}))
        assert out["ok"] is True
        assert out["accountCount"] == 2
        assert "#1 a@example.com — 5h 12%, 7d 86%, Fable week 100%" in out["summary"][0]
        assert "#2 b@example.com [work] (active, disabled) — usage relogin_required" in out["summary"][1]

    def test_missing_weekly_window_says_so(self, tool_ctx, monkeypatch):
        # Claude Team seats report no account-wide weekly window: the API returns
        # `seven_day: null`, so cswap omits the key entirely.
        tools, _ = tool_ctx
        monkeypatch.setattr(
            cswap,
            "list_accounts",
            lambda **kwargs: {
                "activeAccountNumber": 1,
                "accounts": [
                    {
                        "number": 1,
                        "email": "a@example.com",
                        "active": True,
                        "usageStatus": "ok",
                        "usage": {"fiveHour": {"pct": 100.0}, "scoped": [{"name": "Fable", "pct": 47.0}]},
                    }
                ],
            },
        )
        out = json.loads(tools.claude_swap_accounts({}))
        assert "7d n/a" in out["summary"][0]
        assert "Fable week 47%" in out["summary"][0]

    def test_accounts_reports_install_hint(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        monkeypatch.setattr(cswap, "resolve_command", lambda explicit=None: None)
        out = json.loads(tools.claude_swap_accounts({}))
        assert any("uv tool install claude-swap" in w for w in out["warnings"])

    def test_accounts_errors_cleanly(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx

        def boom(**kwargs):
            raise cswap.CswapError("cswap exploded")

        monkeypatch.setattr(cswap, "list_accounts", boom)
        out = json.loads(tools.claude_swap_accounts({}))
        assert out["ok"] is False
        assert "cswap exploded" in out["error"]

    def test_switch_rejects_target_and_strategy_together(self, tool_ctx):
        tools, _ = tool_ctx
        out = json.loads(tools.claude_swap_switch({"target": "2", "strategy": "best"}))
        assert out["ok"] is False

    def test_switch_rejects_unknown_strategy(self, tool_ctx):
        tools, _ = tool_ctx
        assert json.loads(tools.claude_swap_switch({"strategy": "magic"}))["ok"] is False

    def test_switch_by_strategy_builds_next_available_argv(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        seen = {}

        def fake_call(argv, **kwargs):
            seen["argv"] = argv
            return cswap.CswapResult(
                exit_code=0,
                payload={
                    "switched": True,
                    "reason": "switched",
                    "message": "ok",
                    "from": {"number": 1, "email": "a@example.com"},
                    "to": {"number": 2, "email": "b@example.com"},
                },
            )

        monkeypatch.setattr(cswap, "call", fake_call)
        out = json.loads(tools.claude_swap_switch({"strategy": "next-available"}))
        assert seen["argv"] == ["switch", "--strategy", "next-available", "--json"]
        assert out["switched"] is True
        assert out["to"] == "b@example.com"

    def test_switch_noop_is_reported_honestly(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        monkeypatch.setattr(
            cswap,
            "call",
            lambda argv, **k: cswap.CswapResult(
                exit_code=0,
                payload={"switched": False, "reason": "already-active", "message": "Already on"},
            ),
        )
        out = json.loads(tools.claude_swap_switch({"target": "1"}))
        assert out["ok"] is True
        assert out["switched"] is False
        assert out["reason"] == "already-active"

    def test_remove_requires_confirm(self, tool_ctx):
        tools, _ = tool_ctx
        out = json.loads(tools.claude_swap_manage({"action": "remove", "account": "2"}))
        assert out["ok"] is False
        assert out["needsConfirmation"] is True

    def test_remove_with_confirm_runs(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        seen = {}

        def fake_manage(argv, **kwargs):
            seen["argv"] = argv
            seen["stdin_text"] = kwargs.get("stdin_text")
            return cswap.CswapResult(exit_code=0, stdout="Removed")

        monkeypatch.setattr(cswap, "manage", fake_manage)
        monkeypatch.setattr(cswap, "list_accounts", lambda **k: {"accounts": []})
        out = json.loads(
            tools.claude_swap_manage({"action": "remove", "account": "2", "confirm": True})
        )
        assert seen["argv"] == ["remove", "2"]
        # cswap remove always prompts and has no --assume-yes flag; the answer must be supplied.
        assert seen["stdin_text"] == "y\n"
        assert out["ok"] is True

    def test_add_to_a_free_slot_does_not_answer_a_prompt(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        seen = {}

        def fake_manage(argv, **kwargs):
            seen["stdin_text"] = kwargs.get("stdin_text")
            return cswap.CswapResult(exit_code=0, stdout="Added")

        monkeypatch.setattr(cswap, "manage", fake_manage)
        monkeypatch.setattr(cswap, "list_accounts", lambda **k: {"accounts": [{"number": 1}]})
        out = json.loads(tools.claude_swap_manage({"action": "add", "slot": 3}))
        assert seen["stdin_text"] is None
        assert out["ok"] is True

    def test_add_to_an_occupied_slot_needs_confirmation(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        calls = []
        monkeypatch.setattr(cswap, "manage", lambda argv, **k: calls.append(argv))
        monkeypatch.setattr(
            cswap,
            "list_accounts",
            lambda **k: {"accounts": [{"number": 3, "email": "incumbent@example.com"}]},
        )
        out = json.loads(tools.claude_swap_manage({"action": "add", "slot": 3}))
        assert out["ok"] is False
        assert out["needsConfirmation"] is True
        assert "incumbent@example.com" in out["error"]
        assert calls == []  # nothing was run

    def test_add_to_an_occupied_slot_with_confirm_overwrites(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        seen = {}

        def fake_manage(argv, **kwargs):
            seen["argv"] = argv
            seen["stdin_text"] = kwargs.get("stdin_text")
            return cswap.CswapResult(exit_code=0, stdout="Added")

        monkeypatch.setattr(cswap, "manage", fake_manage)
        monkeypatch.setattr(
            cswap,
            "list_accounts",
            lambda **k: {"accounts": [{"number": 3, "email": "incumbent@example.com"}]},
        )
        out = json.loads(
            tools.claude_swap_manage({"action": "add", "slot": 3, "confirm": True})
        )
        assert seen["argv"] == ["add", "--slot", "3"]
        assert seen["stdin_text"] == "y\n"
        assert out["ok"] is True

    def test_slot_must_be_a_number(self, tool_ctx):
        tools, _ = tool_ctx
        assert json.loads(tools.claude_swap_manage({"action": "add", "slot": "two"}))["ok"] is False

    def test_a_cancelled_prompt_is_not_reported_as_success(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        monkeypatch.setattr(
            cswap, "manage", lambda argv, **k: cswap.CswapResult(exit_code=0, stdout="Cancelled")
        )
        monkeypatch.setattr(cswap, "list_accounts", lambda **k: {"accounts": []})
        out = json.loads(
            tools.claude_swap_manage({"action": "remove", "account": "2", "confirm": True})
        )
        assert out["ok"] is False
        assert "cancelled" in out["error"].lower()

    def test_manage_requires_account_for_disable(self, tool_ctx):
        tools, _ = tool_ctx
        assert json.loads(tools.claude_swap_manage({"action": "disable"}))["ok"] is False

    def test_manage_rejects_unknown_action(self, tool_ctx):
        tools, _ = tool_ctx
        assert json.loads(tools.claude_swap_manage({"action": "purge"}))["ok"] is False

    def test_add_passes_slot_and_alias(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        seen = {}

        def fake_manage(argv, **kwargs):
            seen["argv"] = argv
            return cswap.CswapResult(exit_code=0, stdout="Added")

        monkeypatch.setattr(cswap, "manage", fake_manage)
        monkeypatch.setattr(cswap, "list_accounts", lambda **k: {"accounts": []})
        tools.claude_swap_manage({"action": "add", "slot": 3, "alias": "work"})
        assert seen["argv"] == ["add", "--slot", "3", "--alias", "work"]

    def test_auto_defaults_to_dry_run(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        seen = {}
        monkeypatch.setattr(
            cswap,
            "auto_once",
            lambda **k: seen.update(k) or cswap.CswapResult(exit_code=2, documents=[{"event": "no-switch"}]),
        )
        out = json.loads(tools.claude_swap_auto({}))
        assert seen["dry_run"] is True
        assert out["outcome"] == "nothing_to_do"

    def test_config_dir_conflict_surfaces_in_warnings(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        monkeypatch.setattr(
            cfgmod, "config_dir_conflict", lambda: {"source": cfgmod.CONFIG_DIR_ENV, "value": "/x"}
        )
        monkeypatch.setattr(cswap, "list_accounts", lambda **k: {"accounts": []})
        out = json.loads(tools.claude_swap_accounts({}))
        assert any("different config directory" in w for w in out["warnings"])

    def test_every_handler_returns_json(self, tool_ctx, monkeypatch):
        tools, _ = tool_ctx
        monkeypatch.setattr(cswap, "list_accounts", lambda **k: {"accounts": []})
        monkeypatch.setattr(cswap, "manage", lambda argv, **k: cswap.CswapResult(exit_code=0))
        for payload in (
            tools.claude_swap_accounts({}),
            tools.claude_swap_switch({"target": "1"}),
            tools.claude_swap_auto({}),
            tools.claude_swap_manage({"action": "nope"}),
        ):
            assert isinstance(json.loads(payload), dict)


class TestManifest:
    def test_manifest_matches_registration(self):
        import io
        import re

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.yaml")
        text = open(path, encoding="utf-8").read()
        import schemas

        for schema in schemas.ALL:
            assert f"- {schema['name']}" in text, schema["name"]
        assert "- api_request_error" in text
        assert re.search(r"^name: claude-swap-bridge$", text, re.M)

    def test_schema_names_match_their_payload_name(self):
        import schemas

        for schema in schemas.ALL:
            assert schema["parameters"]["type"] == "object"
            assert "description" in schema
