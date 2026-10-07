"""Per-provider extra scan folders (upstream #177)."""

import json

from poketokenbar import commands, config, ctl
from poketokenbar.daemon import Daemon
from poketokenbar.providers.base import with_extra_roots
from poketokenbar.providers.claude import ClaudeProvider


def _assistant_line(msg_id):
    return json.dumps({
        "type": "assistant", "timestamp": "2026-10-06T10:00:00Z", "requestId": msg_id,
        "message": {"id": msg_id, "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 1, "output_tokens": 10}},
    })


def test_extras_are_added_never_replace_and_missing_ones_are_skipped(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "nested").mkdir()
    roots = with_extra_roots([a], [b, a / "nested", tmp_path / "missing", str(b)])
    assert roots == [a, b]


def test_claude_reads_logs_from_an_extra_folder(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "p").mkdir(parents=True)
    (home / ".claude" / "projects" / "p" / "s.jsonl").write_text(_assistant_line("m1") + "\n")
    elsewhere = tmp_path / "synced-machine" / "projects" / "q"
    elsewhere.mkdir(parents=True)
    (elsewhere / "s.jsonl").write_text(_assistant_line("m2") + "\n")
    provider = ClaudeProvider(home=home)
    assert len(provider.scan_entries()) == 1
    provider.extra_roots = (tmp_path / "synced-machine",)
    assert len(provider.scan_entries()) == 2


def test_ctl_folder_edits_config_and_daemon_applies_it(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(config, "default_path", lambda: cfg)
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path / "spool")
    folder = tmp_path / "logs"
    assert ctl.main(["folder", "add", "claude_code", str(folder)]) == 0
    assert ctl.main(["folder", "add", "claude_code", str(folder)]) == 0  # no duplicate
    assert ctl.main(["folder", "add", "opencode", str(folder)]) != 0  # single-database provider
    assert config.load(cfg)["extra_scan_folders"] == {"claude_code": [str(folder)]}

    provider = ClaudeProvider(home=tmp_path)
    d = Daemon(state_path=tmp_path / "state.json", config_path=cfg, cache=None,
               providers=[provider])
    assert provider.extra_roots == (folder,)

    assert ctl.main(["folder", "remove", "claude_code", str(folder)]) == 0
    assert config.load(cfg)["extra_scan_folders"] == {}
    d.spool = tmp_path / "spool"
    d.poll_once()
    assert provider.extra_roots == ()


def test_scan_folders_cannot_be_set_as_a_scalar(tmp_path):
    import pytest

    with pytest.raises(KeyError):
        config.set_value(tmp_path / "c.json", "extra_scan_folders", "x")


def test_a_malformed_folder_map_degrades_to_empty(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"extra_scan_folders": {"claude_code": "not-a-list", "codex": [1, "/x"]}}))
    assert config.load(cfg)["extra_scan_folders"] == {"codex": ["/x"]}
