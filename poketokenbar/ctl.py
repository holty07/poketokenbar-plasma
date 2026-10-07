"""poketokenctl — the plasmoid's only way to talk to the daemon."""

from __future__ import annotations

import sys

from . import commands, config


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: poketokenctl {set <key> <value>|refresh|buy <key> [count] [--confirm]|"
              "use <key> [count]|pin <species-id [unown-letter] [--shiny|--normal]|none>|"
              "snapshot|restore <id>|folder add|remove <provider> <path>|"
              "export <path>|import <path>}",
              file=sys.stderr)
        return 2

    action, rest = argv[0], argv[1:]
    if action == "set":
        if len(rest) != 2:
            print("usage: poketokenctl set <key> <value>", file=sys.stderr)
            return 2
        try:
            config.set_value(config.default_path(), rest[0], rest[1])
        except (KeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        commands.enqueue("reload_config", {})
        return 0

    if action == "refresh":
        commands.enqueue("refresh", {})
        return 0

    if action == "folder":
        if len(rest) != 3 or rest[0] not in ("add", "remove"):
            print("usage: poketokenctl folder add|remove <provider-id> <path>", file=sys.stderr)
            return 2
        from .providers import PROVIDERS

        known = {p.id for p in PROVIDERS if hasattr(p, "extra_roots")}
        if rest[1] not in known:
            print(f"{rest[1]} has no scan folders; one of: {', '.join(sorted(known))}",
                  file=sys.stderr)
            return 1
        config.edit_scan_folder(config.default_path(), rest[0], rest[1], rest[2])
        commands.enqueue("reload_config", {})
        return 0

    if action == "snapshot":
        commands.enqueue("snapshot", {})
        return 0

    if action == "restore":
        if len(rest) != 1:
            print("usage: poketokenctl restore <snapshot-id>", file=sys.stderr)
            return 2
        commands.enqueue("restore", {"id": rest[0]})
        return 0

    if action in ("export", "import"):
        if len(rest) != 1:
            print(f"usage: poketokenctl {action} <path>", file=sys.stderr)
            return 2
        from pathlib import Path

        commands.enqueue(action, {"path": str(Path(rest[0]).expanduser().resolve())})
        return 0

    if action in ("buy", "use"):
        confirm = "--confirm" in rest
        rest = [r for r in rest if r != "--confirm"]
        if not 1 <= len(rest) <= 2 or (confirm and action != "buy"):
            usage = "<key> [count] [--confirm]" if action == "buy" else "<key> [count]"
            print(f"usage: poketokenctl {action} {usage}", file=sys.stderr)
            return 2
        try:
            count = int(rest[1]) if len(rest) == 2 else 1
        except ValueError:
            print(f"count must be a whole number, got {rest[1]!r}", file=sys.stderr)
            return 2
        if count < 1:
            print("count must be at least 1", file=sys.stderr)
            return 2
        args = {"key": rest[0], "count": count}
        if confirm:
            args["confirm"] = True
        commands.enqueue(action, args)
        return 0

    if action == "pin":
        shiny = True if "--shiny" in rest else (False if "--normal" in rest else None)
        rest = [r for r in rest if r not in ("--shiny", "--normal")]
        if not 1 <= len(rest) <= 2 or (rest[0] == "none" and (len(rest) == 2 or shiny is not None)):
            print("usage: poketokenctl pin <species-id [unown-letter]|none>", file=sys.stderr)
            return 2
        if rest[0] == "none":
            species = None
        else:
            try:
                species = int(rest[0])
            except ValueError:
                print(f"species id must be a number or 'none', got {rest[0]!r}", file=sys.stderr)
                return 2
        args = {"species_id": species}
        if len(rest) == 2:
            args["form"] = rest[1]
        if shiny is not None:
            args["shiny"] = shiny
        commands.enqueue("pin", args)
        return 0

    print(f"unknown command: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
