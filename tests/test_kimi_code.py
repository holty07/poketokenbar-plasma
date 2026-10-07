"""Mirrors upstream KimiCodeUsageTests.swift (PokeTokenBar #403)."""

import time
from datetime import UTC, datetime

import pytest

from poketokenbar.cache import ScanCache
from poketokenbar.providers import _local
from poketokenbar.providers.kimi_code import (
    KimiCodeProvider,
    parse_file,
    session_roots,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("KIMI_CODE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)


def _root(home):
    return home / ".kimi-code" / "sessions"


def _write_wire(home, text, session="ses_1", agent="main"):
    directory = _root(home) / "wd_proj_abc" / session / "agents" / agent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "wire.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def _record(model="k3-agent", scope="turn", time_ms=0, input_=100, output=20, read=500, write=30):
    scope_field = f',"usageScope":"{scope}"' if scope is not None else ""
    return (
        f'{{"type":"usage.record","model":"{model}","usage":{{"inputOther":{input_},'
        f'"output":{output},"inputCacheRead":{read},"inputCacheCreation":{write}}}'
        f'{scope_field},"time":{time_ms}}}'
    )


T = 1_779_256_800_000


def test_parses_usage_record_mapping(tmp_path):
    t = 1_779_256_800_302
    path = _write_wire(
        tmp_path,
        f'{{"type":"turn.begin","turnId":"t1","time":{t - 10}}}\n'
        '{"type":"usage.record","model":"kimi-k2","usage":{"inputOther":10,"output":5,'
        f'"inputCacheRead":7,"inputCacheCreation":3}},"usageScope":"turn","time":{t}}}\n',
    )
    entries = parse_file(path)
    assert len(entries) == 1
    e = entries[0]
    assert e.model == "kimi-k2"
    assert (e.input, e.output, e.cache_read, e.cache_write) == (10, 5, 7, 3)
    assert e.total == 25
    assert e.date == datetime.fromtimestamp(t / 1000, tz=UTC)
    assert e.local_day == _local.local_day(e.date)


def test_counts_turn_session_and_missing_scope_skips_unknown(tmp_path):
    path = _write_wire(
        tmp_path,
        "\n".join(
            [
                _record(scope="turn", time_ms=T),
                _record(scope="session", time_ms=T + 1),
                _record(scope=None, time_ms=T + 2),
                _record(scope="cumulative", time_ms=T + 3),
            ]
        ),
    )
    assert len(parse_file(path)) == 3


def test_skips_malformed_lines_and_defaults_model(tmp_path):
    path = _write_wire(
        tmp_path,
        "\n".join(
            [
                f'{{"type":"message","text":"what is \\"usage.record\\"?","time":{T}}}',
                '{"type":"usage.record","usage":{"inputOther":1,"output":1},"usageScope":"turn"}',
                (
                    '{"type":"usage.record","usage":{"inputOther":1,"output":1},'
                    '"usageScope":"turn","time":0}'
                ),
                f'{{"type":"usage.record","model":"k3-agent","usageScope":"turn","time":{T}}}',
                '{"type":"usage.record","model":"k3-agent","usage":{"inputOther":1,',
                (
                    '{"type":"usage.record","model":"","usage":{"inputOther":4,"output":2},'
                    f'"usageScope":"turn","time":{T}}}'
                ),
            ]
        ),
    )
    entries = parse_file(path)
    assert len(entries) == 1
    assert entries[0].model == "kimi-code"
    assert entries[0].total == 6


def test_partial_usage_defaults_missing_buckets(tmp_path):
    path = _write_wire(
        tmp_path,
        '{"type":"usage.record","model":"k3-agent","usage":{"inputCacheRead":9},'
        f'"usageScope":"turn","time":{T}}}',
    )
    e = parse_file(path)[0]
    assert (e.input, e.output, e.cache_read, e.cache_write) == (0, 0, 9, 0)


def test_absurd_token_counts_are_clamped(tmp_path):
    path = _write_wire(
        tmp_path,
        '{"type":"usage.record","model":"k3-agent","usage":{"inputOther":1e30,'
        f'"output":-5,"inputCacheRead":7}},"usageScope":"turn","time":{T}}}',
    )
    e = parse_file(path)[0]
    assert (e.input, e.output, e.cache_read) == (0, 0, 7)


def test_unreadable_file_yields_none_and_is_skipped(tmp_path):
    path = _write_wire(tmp_path, "")
    path.write_bytes(b"\xff\xfe\x41\x42")
    assert parse_file(path) is None
    assert KimiCodeProvider(home=tmp_path).scan_entries() == []


def test_fork_copy_dedups_and_subagents_count(tmp_path):
    original = [_record(time_ms=T), _record(time_ms=T + 1000, output=40)]
    _write_wire(tmp_path, "\n".join(original), session="ses_orig")
    _write_wire(
        tmp_path, "\n".join([*original, _record(time_ms=T + 5000, output=7)]), session="ses_fork"
    )
    _write_wire(tmp_path, _record(model="k2d6-agent", time_ms=T + 2000),
                session="ses_orig", agent="sub_1")

    entries = KimiCodeProvider(home=tmp_path).scan_entries()
    assert len(entries) == 4  # 2 original + 1 fork-only + 1 subagent
    assert {e.model for e in entries} == {"k3-agent", "k2d6-agent"}


def test_only_wire_files_are_scanned(tmp_path):
    _write_wire(tmp_path, _record(time_ms=T))
    (_root(tmp_path) / "wd_proj_abc" / "ses_1" / "context.jsonl").write_text(
        _record(time_ms=T + 1), encoding="utf-8"
    )
    assert len(KimiCodeProvider(home=tmp_path).scan_entries()) == 1


def test_cache_round_trip_and_picks_up_appended_record(tmp_path):
    path = _write_wire(tmp_path, _record(time_ms=T))
    cache = ScanCache(tmp_path / "scan.db")
    assert len(KimiCodeProvider(cache=cache, home=tmp_path).scan_entries()) == 1
    assert len(KimiCodeProvider(cache=cache, home=tmp_path).scan_entries()) == 1

    path.write_text(
        "\n".join([_record(time_ms=T), _record(time_ms=T + 1000, output=1)]), encoding="utf-8"
    )
    assert len(KimiCodeProvider(cache=cache, home=tmp_path).scan_entries()) == 2
    cache.close()


def test_session_roots(tmp_path, monkeypatch):
    defaults = [
        tmp_path / ".kimi-code" / "sessions",
        tmp_path / ".config" / "kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions",
    ]
    assert session_roots(home=tmp_path) == defaults
    monkeypatch.setenv("KIMI_CODE_HOME", "  ")
    assert session_roots(home=tmp_path) == defaults
    monkeypatch.setenv("KIMI_CODE_HOME", "/opt/kimi")
    assert [str(p) for p in session_roots(home=tmp_path)][-1] == "/opt/kimi/sessions"
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / ".kimi-code"))
    assert session_roots(home=tmp_path) == defaults  # no double scan


def test_desktop_root_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert session_roots(home=tmp_path)[1] == (
        tmp_path / "cfg" / "kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions"
    )


def test_provider_fetch_daily_and_periods(tmp_path):
    now = int(time.time() * 1000)
    _write_wire(
        tmp_path,
        "\n".join(
            [
                _record(model="k3-agent", time_ms=now - 2000, input_=100, output=200, read=0, write=0),
                _record(model="k2d6-agent", scope="session", time_ms=now - 1000,
                        input_=10, output=20, read=0, write=0),
            ]
        ),
    )
    provider = KimiCodeProvider(home=tmp_path)
    assert provider.id == "kimi_code"
    daily = provider.fetch_daily()
    assert daily is not None
    assert daily.total_tokens == 330
    assert daily.total_cost == 0.0
    assert provider.fetch_periods()["month"]["tokens"] == 330


def test_provider_without_data_returns_none(tmp_path):
    assert KimiCodeProvider(home=tmp_path).fetch_daily() is None
