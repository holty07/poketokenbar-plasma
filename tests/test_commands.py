import json

from poketokenbar import commands, config, ctl


def test_enqueue_then_drain_returns_the_command(tmp_path):
    commands.enqueue("refresh", {}, spool=tmp_path)
    drained = commands.drain(spool=tmp_path)
    assert len(drained) == 1
    assert drained[0]["name"] == "refresh"


def test_drain_empties_the_spool(tmp_path):
    commands.enqueue("refresh", {}, spool=tmp_path)
    commands.drain(spool=tmp_path)
    assert commands.drain(spool=tmp_path) == []


def test_drain_preserves_enqueue_order(tmp_path):
    for i in range(3):
        commands.enqueue("refresh", {"n": i}, spool=tmp_path)
    assert [c["args"]["n"] for c in commands.drain(spool=tmp_path)] == [0, 1, 2]


def test_drain_discards_corrupt_files_without_failing(tmp_path):
    commands.enqueue("refresh", {}, spool=tmp_path)
    (tmp_path / "999-bad.json").write_text("{corrupt", encoding="utf-8")
    drained = commands.drain(spool=tmp_path)
    assert [c["name"] for c in drained] == ["refresh"]
    assert list(tmp_path.iterdir()) == []


def test_ctl_set_writes_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(config, "default_path", lambda: cfg)
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path / "spool")
    assert ctl.main(["set", "refresh_interval", "30"]) == 0
    assert json.loads(cfg.read_text(encoding="utf-8"))["refresh_interval"] == 30


def test_ctl_set_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path / "spool")
    assert ctl.main(["set", "nonsense", "1"]) != 0


def test_ctl_refresh_enqueues(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path)
    assert ctl.main(["refresh"]) == 0
    assert [c["name"] for c in commands.drain(spool=tmp_path)] == ["refresh"]


def test_ctl_buy_carries_count_and_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path)
    assert ctl.main(["buy", "mint", "3"]) == 0
    assert ctl.main(["buy", "egg", "--confirm"]) == 0
    assert ctl.main(["use", "rareCandy", "5"]) == 0
    got = [(c["name"], c["args"]) for c in commands.drain(spool=tmp_path)]
    assert got == [
        ("buy", {"key": "mint", "count": 3}),
        ("buy", {"key": "egg", "count": 1, "confirm": True}),
        ("use", {"key": "rareCandy", "count": 5}),
    ]


def test_ctl_rejects_bad_counts_and_confirm_on_use(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path)
    assert ctl.main(["buy", "mint", "0"]) != 0
    assert ctl.main(["buy", "mint", "lots"]) != 0
    assert ctl.main(["use", "rareCandy", "--confirm"]) != 0
    assert commands.drain(spool=tmp_path) == []


def test_ctl_pin_and_unpin(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path)
    assert ctl.main(["pin", "25"]) == 0
    assert ctl.main(["pin", "none"]) == 0
    assert ctl.main(["pin", "pikachu"]) != 0
    got = [c["args"] for c in commands.drain(spool=tmp_path)]
    assert got == [{"species_id": 25}, {"species_id": None}]


def test_ctl_set_accepts_a_fractional_difficulty(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(config, "default_path", lambda: cfg)
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path / "spool")
    assert ctl.main(["set", "growth_difficulty", "0.5"]) == 0
    assert json.loads(cfg.read_text(encoding="utf-8"))["growth_difficulty"] == 0.5
    assert ctl.main(["set", "growth_difficulty", "nan"]) != 0


def test_ctl_pin_appearance_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "spool_dir", lambda: tmp_path)
    assert ctl.main(["pin", "25", "--shiny"]) == 0
    assert ctl.main(["pin", "201", "b", "--normal"]) == 0
    assert ctl.main(["pin", "none", "--shiny"]) != 0
    got = [c["args"] for c in commands.drain(spool=tmp_path)]
    assert got == [{"species_id": 25, "shiny": True},
                   {"species_id": 201, "form": "b", "shiny": False}]
