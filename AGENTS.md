# PokeTokenBar for Plasma — Agent Guide

A KDE plasmoid that tracks AI coding token usage and displays it as a Pokémon companion.

## Project Overview
- **Before editing any file, read it first. Before modifying a function, grep for all callers. Research before you edit.**

**Type**: Python + QML (KDE Plasma widget) + systemd daemon

**Core Architecture**:
- `poketokend` daemon (`daemon.py`) polls providers every 120s, writes `state.json`
- `poketokenctl` CLI (`ctl.py`) sends commands via file-based spool
- Plasmoid reads `state.json` via `cat` command (not HTTP for security)

**Data Flow**:
```
AI Coding Logs (Claude, Codex, etc.)
        ↓
UsageProvider implementations scan .jsonl/.db files
        ↓
daemon.py aggregates DailyUsage per provider
        ↓
state.json (written atomically) ← CompanionStore applies Pokémon game logic
        ↓
QML plasmoid displays companion + usage stats
```

## Essential Commands

### Development
```bash
# Run tests
uv run pytest -q

# Run the daemon (requires Plasma environment for widgets)
python -m poketokenbar.daemon

# Use the control CLI
poketokenctl refresh
poketokenctl set <key> <value>
poketokenctl folder add|remove <provider> <path>
poketokenctl snapshot|restore <id>
poketokenctl export|import <path>
poketokenctl buy <item> [count] [--confirm]
poketokenctl use <item> [count]
poketokenctl pin <species-id> [unown-letter] [--shiny|--normal]
```

### Build (Plasma 6)
```bash
./install.sh                    # Full install to ~/.local/
./packaging/build-plasmoids.sh  # Build .plasmoid files only
```

### Build (Plasma 5 compatibility)
```bash
./install.sh --plasma5
./packaging/build-plasmoids5.sh
```

## Code Organization

```
poketokenbar/           # Main Python package
├── daemon.py          # Main daemon loop, state machine
├── ctl.py             # CLI interface (poketokenctl)
├── commands.py        # Spool-based command queue
├── config.py          # Settings load/save (XDG config)
├── state.py           # state.json build/write
├── models.py          # Entry, DailyUsage, BlockUsage dataclasses
├── companion*.py      # Pokémon game state (CompanionStore, CompanionState, MonState)
├── balance.py         # Game constants (EV thresholds, prices, rarity)
├── pricing.py         # Token-to-dollar cost calculation
├── history.py         # Per-day usage aggregation
├── pokeapi.py         # PokeAPI client for species data
├── sprites.py         # Sprite caching
├── notify.py          # Desktop notifications
├── cache.py           # File scan result caching (SQLite)
├── profile.py         # Individual Pokémon stats (IVs, moves)
├── shop.py            # Item costs and inventory
├── snapshots.py       # Save backup/restore
├── limits.py          # Token limit tracking (disabled in this fork)
├── status.py          # Provider status checking
├── tokens.py          # Token aggregation helpers
├── providers/         # UsageProvider implementations
│   ├── base.py        # UsageProvider protocol, scan helpers
│   ├── _local.py      # Local file utilities
│   ├── claude.py      # Claude Code .jsonl parser
│   ├── codex.py       # Codex rollout parser
│   ├── hermes.py      # Hermes Agent SQLite reader
│   ├── aside.py       # Aside profile database
│   ├── opencode.py    # opencode.db reader
│   ├── cursor.py      # Cursor state.vscdb reader
│   ├── kimi_code.py   # Kimi Code wire.jsonl parser
│   ├── pi.py          # Pi session JSONL parser
│   ├── omp.py         # oh-my-pi session JSONL parser
│   ├── kiro.py        # Kiro CLI parser
│   ├── antigravity.py # Antigravity token-only provider
│   └── crush.py       # Crush per-project crush.db reader
└── companion_store.py # Ties providers to companion game logic

plasmoid/               # QML UI files
├── org.kde.plasma.poketokenbar/
│   └── contents/ui/main.qml  # Panel widget
└── org.kde.plasma.poketokenpet/
    └── contents/ui/main.qml  # Desktop pet widget

packaging/              # Build scripts
tests/                  # pytest tests (540+ tests)
```

## Provider Extension Pattern

To add a new AI coding provider:

1. Create `poketokenbar/providers/<name>.py`
2. Implement `UsageProvider` protocol with:
   - `id`, `display_name`, `reports_cost` attributes
   - `fetch_daily(today)` → `DailyUsage | None`
3. Register in `daemon.py:main()` providers list
4. Provider must implement `scan_entries()` for history collection

Providers read from:
- `~/.claude/projects/**/*.jsonl` (Claude Code)
- `~/.codex/sessions/**/*.jsonl` (Codex)
- `~/.hermes/state.db` (Hermes Agent)
- `~/.local/share/opencode/opencode.db` (opencode)
- `~/.aside/u/*/state.db` (Aside)
- `<project>/.crush/crush.db` via `~/.local/share/crush/projects.json` (Crush)
- etc.

## Key Patterns

### 1. Token Counting Deduplication
- `_int(value)` clamps to `0 <= value <= 10^12`
- `dedup_keep_max(entries)` keeps largest total per message/requestId or session
- Global dedup on `(message.id, requestId)` for Claude, on cumulative totals for Codex

### 2. Cost Calculation
- `pricing.entry_cost(entry)` uses `explicit_cost` if available (Claude's cost-state ledger)
- Falls back to `pricing TABLE` of rates (USD per million tokens)
- Long-context pricing: 2x input, 1.5x output for GPT >272K tokens

### 3. Scatter-Folder Support (#177)
- Providers can have `extra_roots` attribute
- Config key `extra_scan_folders` maps provider ID to list of paths
- Edit via `poketokenctl folder add|remove <provider> <path>`

### 4. Commands via File Spool
```python
# Enqueue
commands.enqueue("refresh", {})

# Drain in daemon
for command in commands.drain(spool=self.spool):
    name = command.get("name")
    args = command.get("args") or {}
```

### 5. Atomic State Writes
- `state.write()` uses temp + rename for atomicity
- Panel polls every 2s via `cat` command

## Testing

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_claude_provider.py

# List tests
uv run pytest --collect-only
```

All 540+ tests pass. Test patterns:
- Use fixtures from `conftest.py`
- Mock external resources (filesystem, network)
- Use `tmp_path` fixture for isolation

## Configuration

Settings in `~/.config/poketokenbar/config.json`:
- `refresh_interval` (default: 120s)
- `warn_threshold`/`crit_threshold` (default: 80/95%)
- `show_tokens_in_menu`/`show_cost_in_menu`/`show_limit_in_menu`
- `panel_percent_source` ("limits" or "evolution")
- `floating_pet_enabled`, `floating_pet_size`
- `language` (default: "en", supports 8 languages)
- `growth_difficulty`/`shop_difficulty` (default: 1.0, range: 0.1-2.0)
- `extra_scan_folders` (JSON dict of provider ID → paths)

## Gotchas & Non-Obvious Patterns

1. **No rate limiting** - The daemon burns tokens fast when refresh_interval=0
2. **Zero-cost providers** - Antigravity, Grok, and unknown models show $0 until `pricing.TABLE` is updated
3. **Profile migration** - `claimed_today_tokens_by_provider` is `None` on fresh installs, requiring special handling in `CompanionStore.update()`
4. **XDG paths** - Uses `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`
5. **Plasma 5 vs 6** - UI files differ; Plasma 5 uses Qt 5, Plasma 6 uses Qt 6
6. **Django logging** - Daemon logs to journal via systemd; stderr output is visible with `journalctl -u poketokend`
7. **No checkpoint** - `checkpoint_start()` in Barboss is not implemented; daemon always polls from scratch
8. **Evolution progress** - `panel_percent_source=evolution` shows companion progress instead of token limits

## Data Locations

| Path | Contents |
|------|----------|
| `~/.local/state/poketokenbar/state.json` | Current state for UI |
| `~/.config/poketokenbar/config.json` | Settings |
| `~/.local/share/poketokenbar/companion.json` | Save file (Pokédex, companion) |
| `~/.cache/poketokenbar/` | Scan cache, sprites, PokéAPI data |

## Upstream Reference

This is a Linux port of [chattymin/PokeTokenBar](https://github.com/chattymin/PokeTokenBar). The Swift source contains comments explaining design decisions (e.g., "ports CompanionStore.swift").