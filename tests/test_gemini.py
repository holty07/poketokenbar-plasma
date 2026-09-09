import json
from datetime import datetime, timezone

from poketokenbar.cache import ScanCache
from poketokenbar.providers.gemini import GeminiProvider, iter_records, parse_log


def _serialize(record: dict) -> str:
    # Matches gemini-cli's FileExporter.serialize(): pretty-printed, newline
    # terminated, records concatenated with no separator between them.
    return json.dumps(record, indent=2) + "\n"


def _response_record(
    model="gemini-2.5-flash",
    input_tokens=100,
    output_tokens=50,
    thoughts_tokens=0,
    tool_tokens=0,
    cached_tokens=0,
    timestamp="2026-09-09T01:12:29.104Z",
    prompt_id="prompt-1",
):
    return {
        "attributes": {
            "event.name": "gemini_cli.api_response",
            "event.timestamp": timestamp,
            "model": model,
            "input_token_count": input_tokens,
            "output_token_count": output_tokens,
            "cached_content_token_count": cached_tokens,
            "thoughts_token_count": thoughts_tokens,
            "tool_token_count": tool_tokens,
            "total_token_count": input_tokens + output_tokens + thoughts_tokens + cached_tokens,
            "prompt_id": prompt_id,
            "status_code": 200,
        }
    }


def _request_record(model="gemini-3.1-flash-lite"):
    # A real request-details record carries no usage attributes at all —
    # this must never be mistaken for a response.
    return {
        "attributes": {
            "event.name": "gen_ai.client.inference.operation.details",
            "event.timestamp": "2026-09-09T01:12:29.104Z",
            "gen_ai.request.model": model,
        }
    }


def test_iter_records_splits_concatenated_pretty_json():
    text = _serialize({"a": 1}) + _serialize({"b": 2}) + _serialize({"c": [1, 2, 3]})
    records = list(iter_records(text))
    assert records == [{"a": 1}, {"b": 2}, {"c": [1, 2, 3]}]


def test_iter_records_drops_a_trailing_partial_object():
    # The exporter is mid-write when the daemon polls; the tail must not
    # crash the parse, and must not be cached as a valid (empty) result.
    text = _serialize({"a": 1}) + '{\n  "b": '
    assert list(iter_records(text)) == [{"a": 1}]


def test_only_api_response_events_become_entries(tmp_path):
    log = tmp_path / "telemetry.log"
    log.write_text(_serialize(_request_record()) + _serialize(_response_record()))
    entries = parse_log(log)
    assert len(entries) == 1
    assert entries[0].model == "gemini-2.5-flash"


def test_thoughts_tokens_fold_into_output(tmp_path):
    log = tmp_path / "telemetry.log"
    log.write_text(_serialize(_response_record(output_tokens=50, thoughts_tokens=30)))
    entries = parse_log(log)
    assert entries[0].output == 80


def test_tool_tokens_fold_into_input(tmp_path):
    log = tmp_path / "telemetry.log"
    log.write_text(_serialize(_response_record(input_tokens=100, tool_tokens=25)))
    entries = parse_log(log)
    assert entries[0].input == 125


def test_cached_tokens_become_cache_read(tmp_path):
    log = tmp_path / "telemetry.log"
    log.write_text(_serialize(_response_record(cached_tokens=40)))
    entries = parse_log(log)
    assert entries[0].cache_read == 40
    assert entries[0].cache_write == 0


def test_zero_usage_record_is_skipped(tmp_path):
    log = tmp_path / "telemetry.log"
    log.write_text(_serialize(_response_record(input_tokens=0, output_tokens=0)))
    assert parse_log(log) == []


def test_local_day_derives_from_event_timestamp(tmp_path):
    log = tmp_path / "telemetry.log"
    log.write_text(_serialize(_response_record(timestamp="2026-09-03T12:00:00.000Z")))
    entries = parse_log(log)
    expected = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")
    assert entries[0].local_day == expected


def test_provider_reads_the_default_log_location(tmp_path):
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    (gemini_dir / "telemetry.log").write_text(_serialize(_response_record()))
    provider = GeminiProvider(home=tmp_path)
    entries = provider.scan_entries()
    assert len(entries) == 1


def test_a_priced_model_costs_correctly(tmp_path):
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    now = datetime.now(tz=timezone.utc)
    (gemini_dir / "telemetry.log").write_text(
        _serialize(
            _response_record(
                model="gemini-2.5-flash",
                input_tokens=1_000_000,
                output_tokens=0,
                timestamp=now.isoformat().replace("+00:00", "Z"),
            )
        )
    )
    provider = GeminiProvider(home=tmp_path)
    today = now.astimezone().strftime("%Y-%m-%d")
    daily = provider.fetch_daily(today=today)
    assert daily is not None
    assert daily.total_cost == 0.30  # per_million(0.30, ...) input rate


def test_no_log_file_means_no_entries(tmp_path):
    provider = GeminiProvider(home=tmp_path)
    assert provider.scan_entries() == []
    assert provider.fetch_daily() is None


def test_cache_roundtrips_scan_entries(tmp_path):
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    (gemini_dir / "telemetry.log").write_text(_serialize(_response_record()))

    cache = ScanCache(tmp_path / "scan.db")
    try:
        provider = GeminiProvider(cache=cache, home=tmp_path)
        first = provider.scan_entries()
        second = provider.scan_entries()
        assert len(first) == 1
        assert [e.id for e in first] == [e.id for e in second]
    finally:
        cache.close()
