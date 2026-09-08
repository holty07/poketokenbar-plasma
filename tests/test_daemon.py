import json

from poketokenbar.daemon import Daemon
from poketokenbar.models import DailyUsage, ProviderEnrichment


class FakeProvider:
    id = "fake"
    display_name = "Fake"
    reports_cost = True

    def __init__(self, daily=None, boom=False, pid="fake", history=False):
        self._daily = daily
        self._boom = boom
        self.id = pid
        self._history = history

    def fetch_daily(self, today=None):
        if self._boom:
            raise RuntimeError("boom")
        return self._daily

    def scan_entries(self):
        return ["entry"] if self._history else []

    def fetch_enrichment(self):
        return ProviderEnrichment()


def _daemon(tmp_path, providers):
    return Daemon(
        state_path=tmp_path / "state.json",
        config_path=tmp_path / "config.json",
        cache=None,
        providers=providers,
    )


def test_poll_writes_state_file(tmp_path):
    d = _daemon(tmp_path, [FakeProvider(DailyUsage(date="2026-08-18", total_tokens=42))])
    d.poll_once()
    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert written["today"]["total_tokens"] == 42


def test_a_failing_provider_does_not_abort_the_poll(tmp_path):
    # Per-provider isolation: one bad parser must not zero the panel.
    d = _daemon(
        tmp_path,
        [
            FakeProvider(boom=True, pid="broken"),
            FakeProvider(DailyUsage(date="2026-08-18", total_tokens=7), pid="ok"),
        ],
    )
    payload = d.poll_once()
    assert payload["today"]["total_tokens"] == 7
    assert any("boom" in e for e in payload["errors"])


def test_provider_returning_none_contributes_nothing(tmp_path):
    d = _daemon(tmp_path, [FakeProvider(None)])
    payload = d.poll_once()
    assert payload["today"]["total_tokens"] == 0
    assert payload["errors"] == []


def test_a_quiet_but_used_provider_still_appears_at_zero(tmp_path):
    # No usage today, but it has history (e.g. antigravity used last week) —
    # must not vanish from the panel between sessions, which reads as
    # "broken" rather than "no usage yet".
    d = _daemon(tmp_path, [FakeProvider(None, pid="antigravity", history=True)])
    payload = d.poll_once()
    assert payload["providers"]["antigravity"]["total_tokens"] == 0
    assert payload["providers"]["antigravity"]["total_cost"] == 0


def test_a_never_used_provider_is_left_out_entirely(tmp_path):
    # No usage today AND no history at all (e.g. codex was never installed) —
    # that's "not installed", not "no usage yet", so it stays out of the
    # breakdown rather than cluttering it with permanent zeroes.
    d = _daemon(tmp_path, [FakeProvider(None, pid="codex", history=False)])
    payload = d.poll_once()
    assert "codex" not in payload["providers"]


def test_a_failing_provider_is_not_listed_at_zero(tmp_path):
    # Unlike "no usage", an exception is a real failure — it stays out of
    # the breakdown and shows up in errors[] instead.
    d = _daemon(tmp_path, [FakeProvider(boom=True, pid="broken")])
    payload = d.poll_once()
    assert "broken" not in payload["providers"]


def test_reload_config_command_is_applied(tmp_path):
    from poketokenbar import commands

    spool = tmp_path / "spool"
    d = _daemon(tmp_path, [FakeProvider(DailyUsage(date="2026-08-18", total_tokens=42))])
    d.spool = spool
    (tmp_path / "config.json").write_text('{"show_tokens_in_menu": false}', encoding="utf-8")
    commands.enqueue("reload_config", {}, spool=spool)
    payload = d.poll_once()
    assert payload["panel"]["tokens_text"] == ""
