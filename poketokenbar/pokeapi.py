"""PokéAPI access — ports PokeAPIClient.swift.

Species data is fetched at runtime and cached on disk; nothing Pokémon-related
is bundled in the repository.

Everything here is best effort. If the network is down the caller keeps the
tokens in the egg and hatches later — progress is never discarded.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .balance import DITTO_SPECIES_ID, Rarity
from .companion import EvoLine

REST_BASE = "https://pokeapi.co/api/v2"
GRAPHQL_URL = "https://graphql.pokeapi.co/v1beta2"
# Gen I-V. The animated Black/White sprites the panel uses stop here.
MAX_SPECIES_ID = 649
# PokéAPI has no Portuguese or Russian species names; those fall back to English.
LANG_CODES = ("ko", "en", "ja-Hrkt", "ja", "es", "fr", "de")
# PokéAPI's GraphQL endpoint answers 403 to urllib's default User-Agent.
USER_AGENT = "poketokenbar/0.1 (+https://github.com/chattymin/PokeTokenBar)"


class PokeAPIError(Exception):
    pass


@dataclass(slots=True)
class BaseSpecies:
    id: int
    capture_rate: int


def _get_json(url: str, timeout: float = 15.0):
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise PokeAPIError(f"GET {url}: {exc}") from exc


def _post_json(url: str, payload: dict, timeout: float = 20.0):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body)
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise PokeAPIError(f"POST {url}: {exc}") from exc


class PokeAPI:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or (Path.home() / ".cache" / "poketokenbar")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._species: dict[int, dict] = {}
        self._lines: dict[int, EvoLine] = {}
        # Details are immutable; keep what was read so payloads don't re-read
        # and re-parse a file per species every poll.
        self._details: dict[int, dict] = {}

    # --- hatch candidates --------------------------------------------------

    @property
    def _index_file(self) -> Path:
        return self.cache_dir / "base-species.json"

    def base_species_index(self) -> list[BaseSpecies]:
        """Every Gen I-V evolution-line start, with its capture rate.

        One GraphQL query, cached to disk. Ditto is excluded from the normal
        pool; it only appears through the disguise mechanic.
        """
        if self._index_file.is_file():
            try:
                raw = json.loads(self._index_file.read_text(encoding="utf-8"))
                if raw:
                    return [BaseSpecies(r["id"], r["capture_rate"]) for r in raw]
            except (ValueError, KeyError, TypeError):
                pass  # rebuild below

        query = (
            "{ pokemonspecies(where: {evolves_from_species_id: {_is_null: true}, "
            f"id: {{_lte: {MAX_SPECIES_ID}, _neq: {DITTO_SPECIES_ID}}}}}, "
            "order_by: {id: asc}) { id capture_rate } }"
        )
        payload = _post_json(GRAPHQL_URL, {"query": query})
        rows = (payload.get("data") or {}).get("pokemonspecies") or []
        if not rows:
            raise PokeAPIError("empty base species index")

        out = [
            BaseSpecies(int(r["id"]), int(r["capture_rate"]))
            for r in rows
            if r.get("capture_rate") is not None
        ]
        tmp = self._index_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([{"id": b.id, "capture_rate": b.capture_rate} for b in out]),
            encoding="utf-8",
        )
        tmp.replace(self._index_file)
        return out

    # --- lines -------------------------------------------------------------

    def species(self, species_id: int) -> dict:
        if species_id in self._species:
            return self._species[species_id]
        cached = self.cache_dir / "species" / f"{species_id}.json"
        if cached.is_file():
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                self._species[species_id] = data
                return data
            except ValueError:
                pass
        data = _get_json(f"{REST_BASE}/pokemon-species/{species_id}")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(data), encoding="utf-8")
        self._species[species_id] = data
        return data

    def line(self, base_species_id: int) -> EvoLine:
        """The evolution line starting at base_species_id.

        Branching lines pick one path; the panel shows a single companion.
        """
        if base_species_id in self._lines:
            return self._lines[base_species_id]

        base = self.species(base_species_id)
        chain_url = (base.get("evolution_chain") or {}).get("url")
        if not chain_url or not chain_url.startswith("https://pokeapi.co/"):
            raise PokeAPIError(f"bad evolution chain url for {base_species_id}")
        chain = _get_json(chain_url)

        path: list[int] = []
        node = chain.get("chain")
        while node:
            species_ref = node.get("species") or {}
            species_id = _id_from_url(species_ref.get("url", ""))
            if species_id is None or species_id > MAX_SPECIES_ID:
                break
            path.append(species_id)
            nxt = node.get("evolves_to") or []
            node = nxt[0] if nxt else None

        if not path:
            raise PokeAPIError(f"empty evolution path for {base_species_id}")

        rarity = Rarity.classify(
            int(base.get("capture_rate") or 255),
            bool(base.get("is_legendary")),
            bool(base.get("is_mythical")),
        )
        names: dict[int, dict[str, str]] = {}
        for sid in path:
            try:
                entry = self.species(sid)
            except PokeAPIError:
                continue
            by_lang = {
                n["language"]["name"]: n["name"]
                for n in entry.get("names", [])
                if n.get("language", {}).get("name") in LANG_CODES
            }
            names[sid] = by_lang

        evo = EvoLine(base_id=base_species_id, path_ids=path, rarity=rarity, names=names)
        self._lines[base_species_id] = evo
        return evo

    # --- battle details (#264) ----------------------------------------------

    DETAILS_TTL = 30 * 86400

    def _details_file(self, species_id: int) -> Path:
        return self.cache_dir / "details" / f"{species_id}.json"

    def details_cached(self, species_id: int) -> dict | None:
        """Details from disk only — never the network. Stale is fine here:
        base stats and learnsets don't change."""
        if species_id in self._details:
            return self._details[species_id]
        try:
            raw = json.loads(self._details_file(species_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        details = raw.get("details") if isinstance(raw, dict) else None
        if details:
            self._details[species_id] = details
        return details

    def details(self, species_id: int) -> dict:
        """Default-form battle metadata: disk cache (30 days) -> REST, with a
        stale disk copy as the offline fallback."""
        path = self._details_file(species_id)
        stale = None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            stale = raw.get("details")
            if time.time() - float(raw.get("fetched_at", 0)) < self.DETAILS_TTL and stale:
                return stale
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            mon = _get_json(f"{REST_BASE}/pokemon/{species_id}")
            species = self.species(species_id)
        except PokeAPIError:
            if stale:
                return stale
            raise
        details = normalize_details(species_id, mon, species)
        self._details[species_id] = details
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "details": details}), encoding="utf-8")
        tmp.replace(path)
        return details

    # --- rolling -----------------------------------------------------------

    def roll_base_species(
        self, rng, tier: Rarity | None = None, collected_bases: set[int] | None = None
    ) -> int:
        """Capture-rate-weighted pick, so commons are common.

        capture_rate runs 3 (legendary-ish) to 255 (Caterpie). Using it directly
        as the weight reproduces the official rarity curve. Lines already
        graduated weigh half (upstream chooseBase / CollectionWeight), nudging
        toward new species without ruling repeats out.
        """
        from .balance import collection_weight

        collected_bases = collected_bases or set()
        candidates = self.base_species_index()
        if tier is not None:
            ceiling = tier.capture_rate_ceiling
            if ceiling is not None:
                candidates = [c for c in candidates if c.capture_rate <= ceiling]
        if not candidates:
            raise PokeAPIError("no hatch candidates")
        weights = [
            collection_weight(c.capture_rate, c.id in collected_bases) for c in candidates
        ]
        return rng.choices(candidates, weights=weights, k=1)[0].id


def normalize_details(species_id: int, mon: dict, species: dict) -> dict:
    """Keep only what profiles need. Moves are cut to the Gen V learnset at
    the trust boundary, before memory or disk — PokéAPI's cross-generation
    move history is ~15x larger than what is used."""
    from .profile import VERSION_GROUP

    moves = []
    for move in mon.get("moves") or []:
        learn = [
            {"method": (row.get("move_learn_method") or {}).get("name", ""),
             "level": int(row.get("level_learned_at") or 0)}
            for row in move.get("version_group_details") or []
            if (row.get("version_group") or {}).get("name") == VERSION_GROUP
        ]
        if learn:
            moves.append({"name": (move.get("move") or {}).get("name", ""), "learn": learn})
    moves.sort(key=lambda m: m["name"])
    gender_rate = species.get("gender_rate")
    return {
        "species_id": species_id,
        "name": mon.get("name", ""),
        "height": mon.get("height"),
        "weight": mon.get("weight"),
        "gender_rate": gender_rate if isinstance(gender_rate, int) else -1,
        "types": [
            (t.get("type") or {}).get("name", "")
            for t in sorted(mon.get("types") or [], key=lambda t: t.get("slot", 0))
        ],
        "stats": {
            (s.get("stat") or {}).get("name", ""): s.get("base_stat", 0)
            for s in mon.get("stats") or []
        },
        "abilities": sorted(
            (
                {"name": (a.get("ability") or {}).get("name", ""), "slot": a.get("slot", 0),
                 "hidden": bool(a.get("is_hidden"))}
                for a in mon.get("abilities") or []
            ),
            key=lambda a: a["slot"],
        ),
        "moves": moves,
    }


def _id_from_url(url: str) -> int | None:
    parts = [p for p in url.rstrip("/").split("/") if p]
    if not parts:
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None
