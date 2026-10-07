"""Kiro CLI usage — ports upstream's Kiro reader (PokeTokenBar #178, #236,
#383, plus the #307 clamp).

Two on-disk generations coexist, and both are scanned:

* Pre-2.20 kiro-cli keeps a SQLite database, `data.sqlite3`, with one row per
  conversation whose JSON `value` holds the *entire* history, rewritten in
  place every turn. Two schemas: `conversations_v2(conversation_id, value)`
  (< 2.0.1) and `conversations(key, value)` (2.0.1+, id inside the JSON).
* 2.20+ / `--v3` append JSONL under `~/.kiro/sessions`: `cli/<id>.jsonl`
  (`{kind: Prompt|AssistantMessage|ToolResults|Clear, data}` with a
  companion `<id>.json`), or `<workspace>/<session>/messages.jsonl` next to a
  `session.json` (structured `{payload: {type}}` or flat `{role, content}`).

Neither store persists a real token count. Every turn resends the whole
conversation, so a turn's input is a bytes/4 estimate of the accumulated
history plus its own prompt (and tool results); output is bytes/4 of the
streamed `response_size` (SQLite) or the assistant text, tool-call input and
thinking text (JSONL, #383). Image blobs, ids and signatures are not counted.
`usage_summary` credits are not dollars, so cost is $0 ("unavailable"
upstream).

Kiro *deletes* turns on `/clear` and compaction, so — like upstream — each
scan is merged keep-max with the entries already seen this process (cleared at
a month change); a cleared turn stays counted rather than dropping out of
today's total. Unchanged files are not re-parsed (#178): the ScanCache keys
SQLite on `(mtime, size)` across the database and its `-wal`, never `-shm`.

Paths. Kiro CLI derives from Amazon Q Developer CLI, which stores
`data.sqlite3` under the platform data dir — `~/Library/Application Support`
on macOS, `$XDG_DATA_HOME` (default `~/.local/share`) on Linux — so the
legacy database is `~/.local/share/kiro-cli/data.sqlite3` here.
`$KIRO_CLI_HOME` overrides it (comma-separated, as upstream). The JSONL
sessions live in the `~/.kiro` home dot-dir on every OS (`$KIRO_HOME`
overrides).
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from ..cache import ScanCache
from ..models import DailyUsage, Entry, ProviderEnrichment
from . import _local

PARSER_VERSION = 1

BYTES_PER_TOKEN = 4
_INT64_MAX = 2**63 - 1


# --- roots -----------------------------------------------------------------


def roots(home: Path | None = None) -> list[Path]:
    sqlite_homes = _local.env_paths("KIRO_CLI_HOME") or [
        _local.xdg_data_home(home) / "kiro-cli"
    ]
    kiro_homes = _local.env_paths("KIRO_HOME") or [_local.home_dir(home) / ".kiro"]
    return [*sqlite_homes, *(h / "sessions" for h in kiro_homes)]


def database_path(root: Path) -> Path:
    return root if root.suffix == ".sqlite3" else root / "data.sqlite3"


def is_jsonl_session_file(path: Path) -> bool:
    """Layout-shaped, not "every jsonl under the root"."""
    if path.name == "messages.jsonl":
        return True
    return path.suffix == ".jsonl" and path.parent.name == "cli"


def source_files(scan_roots: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for root in scan_roots:
        for path in [database_path(root), *_local.walk_files(root, is_jsonl_session_file)]:
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


# --- byte estimators -------------------------------------------------------


def _number_text(value: float) -> str:
    """NSNumber.stringValue: integral floats print without a fraction."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return str(value)


def json_value_bytes(value) -> int:
    """UTF-8 bytes of every string/number leaf (keys excluded)."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return len(_number_text(value))
    if isinstance(value, list):
        return sum(json_value_bytes(v) for v in value)
    if isinstance(value, dict):
        return sum(json_value_bytes(v) for v in value.values())
    return 0


def field_bytes(value) -> int:
    """A SQLite turn's `user`/`assistant` field, minus its `images` blob."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return sum(json_value_bytes(v) for k, v in value.items() if k != "images")
    return 0


def text_bytes(value) -> int:
    """Textual content of a JSONL content value / block list (#383): text,
    tool-use input, tool-result content, thinking text, json payloads. Ids,
    signatures, redacted data and images do not count."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, list):
        return sum(text_bytes(v) for v in value)
    if not isinstance(value, dict):
        return 0
    kind = value.get("kind")
    if isinstance(kind, str):
        data = value.get("data")
        if kind == "text":
            return text_bytes(data)
        if kind == "toolUse":
            return json_value_bytes(data.get("input")) if isinstance(data, dict) else 0
        if kind == "toolResult":
            return text_bytes(data.get("content")) if isinstance(data, dict) else 0
        if kind == "thinking":
            thought = data.get("text") if isinstance(data, dict) else None
            return len(thought.encode("utf-8")) if isinstance(thought, str) else 0
        if kind == "json":
            return json_value_bytes(data)
        return 0
    for key in ("content", "text", "data"):
        if key in value:
            return text_bytes(value[key])
    return 0


def _flexible_number(value) -> float | None:
    raw = _local.number(value)
    if raw is None and isinstance(value, str):
        try:
            raw = float(value.strip())
        except ValueError:
            return None
    return raw


def _int64_millis(value: float) -> int:
    if math.isnan(value) or value <= 0:
        return 0
    return _INT64_MAX if value >= 9.223372036854776e18 else int(value)


def _timestamp_millis(raw, moment) -> int:
    value = _flexible_number(raw)
    if value is not None and value > 0:
        return _int64_millis(value * 1000 if value < 1_000_000_000_000 else value)
    return _int64_millis(moment.timestamp() * 1000)


def _estimate(entry_id: str, moment, model: str, input_bytes: int, output_bytes: int):
    return _local.make_entry(
        entry_id,
        moment,
        model,
        input_=input_bytes // BYTES_PER_TOKEN,
        output=output_bytes // BYTES_PER_TOKEN,
        explicit_cost=0.0,
    )


# --- SQLite (pre-2.20) -----------------------------------------------------


def turn_entries(conversation_id: str, conversation: dict) -> list[Entry]:
    history = conversation.get("history")
    if not isinstance(history, list):
        return []
    entries = []
    # `latest_summary` stands in for turns compaction deleted from history —
    # it is still resent on every later request.
    cumulative = json_value_bytes(conversation.get("latest_summary"))
    for turn in history:
        if not isinstance(turn, dict):
            continue
        user_bytes = field_bytes(turn.get("user"))
        prompt_bytes = cumulative + user_bytes
        # Every turn's text joins the history, even one skipped below.
        cumulative += user_bytes + field_bytes(turn.get("assistant"))
        meta = turn.get("request_metadata")
        if not isinstance(meta, dict):
            continue
        raw = _flexible_number(meta.get("request_start_timestamp_ms"))
        moment = _local.epoch_date(raw)
        if raw is None or moment is None:
            continue
        entry = _estimate(
            f"kiro|{conversation_id}|{_int64_millis(raw)}",
            moment,
            _local.text(meta.get("model_id")) or "unknown",
            prompt_bytes,
            _local.tokens(meta.get("response_size"), allow_str=True),
        )
        if entry is not None:
            entries.append(entry)
    return entries


def _query(conn: sqlite3.Connection, sql: str) -> list[tuple] | None:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error:
        return None


def parse_database(db_path: Path) -> list[Entry] | None:
    """Both schema generations; None when neither could be read this poll
    (BUSY, damaged, not a Kiro store) so the next poll retries."""
    conn = _local.open_readonly(db_path)
    if conn is None:
        return None
    try:
        v2_rows = _query(conn, "SELECT conversation_id, value FROM conversations_v2")
        v1_rows = _query(conn, "SELECT value FROM conversations")
    finally:
        conn.close()
    if v2_rows is None and v1_rows is None:
        return None
    entries: list[Entry] = []
    for conversation_id, value in v2_rows or []:
        conversation = _local.loads(value) if isinstance(value, (str, bytes)) else None
        if not isinstance(conversation, dict):
            continue
        cid = (
            _local.text(conversation_id)
            or _local.text(conversation.get("conversation_id"))
            or str(db_path)
        )
        entries.extend(turn_entries(cid, conversation))
    for (value,) in v1_rows or []:
        conversation = _local.loads(value) if isinstance(value, (str, bytes)) else None
        if not isinstance(conversation, dict):
            continue
        cid = _local.text(conversation.get("conversation_id"))
        if cid is not None:
            entries.extend(turn_entries(cid, conversation))
    return _local.dedup_keep_max(entries)


# --- JSONL (2.20+ / v3) ----------------------------------------------------


def _json_file(path: Path) -> dict | None:
    content = _local.read_text(path)
    obj = _local.loads(content) if content is not None else None
    return obj if isinstance(obj, dict) else None


def _dig(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def parse_cli_jsonl(path: Path) -> list[Entry] | None:
    """`sessions/cli/<id>.jsonl`; model and session id from `<id>.json`."""
    content = _local.read_text(path)
    if content is None:
        return None
    companion = _json_file(path.with_suffix(".json"))
    session_id = _local.text(_dig(companion, "session_id")) or path.stem
    model = _local.text(
        _dig(companion, "session_state", "rts_model_state", "model_info", "model_id")
    ) or "unknown"

    entries: list[Entry] = []
    state = {"history": 0, "prompt": 0, "assistant": 0, "tool": 0,
             "raw": None, "date": None, "started": False}

    def flush() -> None:
        moment = state["date"]
        if state["started"] and moment is not None:
            entry = _estimate(
                f"kiro|cli|{session_id}|{_timestamp_millis(state['raw'], moment)}",
                moment,
                model,
                state["history"] + state["prompt"] + state["tool"],
                state["assistant"],
            )
            if entry is not None:
                entries.append(entry)
        state["history"] += state["prompt"] + state["assistant"] + state["tool"]
        state.update(prompt=0, assistant=0, tool=0, raw=None, date=None)

    for line in content.splitlines():
        event = _local.loads(line)
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "Prompt":
            if state["started"]:
                flush()
            state["started"] = True
            state["prompt"] = text_bytes(data.get("content"))
            state["raw"] = _dig(data, "meta", "timestamp")
            state["date"] = _local.flexible_date(state["raw"])
            state["assistant"] = 0
            state["tool"] = 0
        elif kind == "AssistantMessage":
            state["assistant"] += text_bytes(data.get("content"))
        elif kind == "ToolResults":
            state["tool"] += text_bytes(data.get("content"))
        elif kind == "Clear":
            flush()
            state["started"] = False
            state["history"] = 0
    flush()
    return entries


def parse_v3_jsonl(path: Path) -> list[Entry] | None:
    """`<workspace>/<session>/messages.jsonl` + sibling `session.json`."""
    content = _local.read_text(path)
    if content is None:
        return None
    session = _json_file(path.parent / "session.json")
    session_id = _local.text(_dig(session, "id")) or path.parent.name
    model = _local.text(_dig(session, "modelId")) or "unknown"
    fallback = _local.flexible_date(_dig(session, "createdAt")) or _local.flexible_date(
        _dig(session, "lastModifiedAt")
    )

    entries: list[Entry] = []
    state = {"history": 0, "prompt": 0, "assistant": 0, "date": None,
             "started": False, "turn": 0}

    def flush() -> None:
        had_content = state["started"] and state["prompt"] + state["assistant"] > 0
        moment = state["date"] or fallback
        if had_content and moment is not None:
            entry = _estimate(
                f"kiro|v3|{session_id}|{state['turn']}",
                moment,
                model,
                state["history"] + state["prompt"],
                state["assistant"],
            )
            if entry is not None:
                entries.append(entry)
        state["history"] += state["prompt"] + state["assistant"]
        state.update(prompt=0, assistant=0, date=None)
        # Advance even for a turn outside any window so ids stay stable.
        if had_content:
            state["turn"] += 1

    def begin_prompt(content_value, moment) -> None:
        if state["started"]:
            flush()
        state["started"] = True
        state["prompt"] = text_bytes(content_value)
        state["date"] = moment
        state["assistant"] = 0

    def add_output(byte_count: int, moment=None) -> None:
        state["assistant"] += byte_count
        if not state["started"] and state["assistant"] > 0:
            state["started"] = True
        if state["date"] is None and moment is not None:
            state["date"] = moment

    for line in content.splitlines():
        event = _local.loads(line)
        if not isinstance(event, dict):
            continue
        moment = _local.flexible_date(event.get("timestamp"))
        payload = event.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            kind = payload["type"]
            if kind == "user":
                begin_prompt(payload.get("content"), moment)
            elif kind == "assistant":
                add_output(text_bytes(payload.get("content")), moment)
            elif kind == "tool_call":
                args = payload.get("args")
                if args is not None:
                    add_output(
                        len(args.encode("utf-8")) if isinstance(args, str)
                        else json_value_bytes(args)
                    )
            elif kind == "tool_result":
                state["prompt"] += text_bytes(payload.get("content"))
            elif kind == "turn_end":
                if state["date"] is None:
                    state["date"] = moment
                flush()
                state["started"] = False
            # usage_summary / session_metadata: credits are not API dollars.
        elif isinstance(event.get("role"), str):
            role = event["role"].strip()
            if role in ("user", "human", "prompt"):
                begin_prompt(event.get("content"), moment)
            elif role in ("assistant", "bot"):
                add_output(text_bytes(event.get("content")), moment)
    flush()
    return entries


# --- dispatch --------------------------------------------------------------


def parse_source(path: Path) -> list[Entry] | None:
    if path.suffix == ".sqlite3":
        return parse_database(path)
    if path.name == "messages.jsonl":
        return parse_v3_jsonl(path)
    return parse_cli_jsonl(path)


def source_signature(path: Path) -> tuple[float, int] | None:
    if path.suffix == ".sqlite3":
        return _local.db_signature(path) if path.is_file() else None
    return _local.file_signature(path)


class KiroProvider:
    """Kiro CLI local usage (byte-estimated tokens)."""

    id = "kiro"
    display_name = "Kiro"
    reports_cost = False
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, cache: ScanCache | None = None, home: Path | None = None) -> None:
        self._cache = cache
        self._home = home
        self._seen = _local.SeenEntries()

    def scan_entries(self) -> list[Entry]:
        scanned = _local.scan_cached(
            self._cache,
            self.id,
            self.PARSER_VERSION,
            source_files(roots(home=self._home)),
            parse_source,
            signature=source_signature,
        )
        return self._seen.merge(_local.dedup_keep_max(scanned))

    def fetch_daily(self, today: str | None = None) -> DailyUsage | None:
        return _local.daily(self.scan_entries(), today)

    def fetch_periods(self, today: str | None = None) -> dict:
        return _local.periods(self.scan_entries(), today)

    def fetch_enrichment(self) -> ProviderEnrichment:
        # Blocks/burn-rate remain unported for every provider; the *_ok flags
        # stay false so callers keep their previous values.
        return ProviderEnrichment()
