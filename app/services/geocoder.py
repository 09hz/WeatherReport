"""Offline US place-name geocoding.

NWS accepts coordinates only, so a user typing "Austin, TX" or "78701" needs
those resolved first. This module answers from a dataset committed to the repo
(`app/data/`), so a lookup makes NO network request and adds no outbound host
beyond api.weather.gov. Regenerate the data with `scripts/build_geodata.py`.

What it accepts:

    78701            a 5-digit postal code (ZIP+4 is truncated to the 5)
    Austin, TX       city with a state code
    Austin, Texas    city with a state name
    Austin TX        comma optional
    Austin           bare city, ONLY when it exists in exactly one state

Ambiguity is never resolved by guessing. A bare city name present in several
states raises ValidationError listing the states, because the dataset carries
no population with which to pick a "main" Springfield, and silently choosing
one would report the wrong city's weather as if it were right.

A city's coordinate is the centroid of its postal codes, so it lands within
the city but is not a precise address. That is the correct resolution for a
weather observation, which comes from a station kilometres away regardless.
"""

from __future__ import annotations

import csv
import gzip
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple

from ..errors import NotFoundError, ValidationError

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLACES_PATH = DATA_DIR / "us_places.csv.gz"
STATES_PATH = DATA_DIR / "us_states.csv"

#: A query of this shape is a postal code, not a city.
_ZIP_RE = re.compile(r"^(\d{5})(?:-\d{4})?$")

#: How many candidate states an ambiguity error lists before summarising.
MAX_AMBIGUOUS_LISTED = 8

#: Longest state name in tokens ("Northern Mariana Islands"), the widest
#: trailing slice worth testing when the query has no comma.
MAX_STATE_TOKENS = 3

#: Abbreviations expanded when the typed form misses. The dataset spells
#: these out ("Saint Louis", not "St. Louis"), so a user typing the common
#: short form would otherwise get "no such place". The typed form is always
#: tried FIRST, which keeps the handful of genuine "St"/"Ft" place names
#: reachable.
ABBREVIATIONS = {
    "st": "saint",
    "ste": "sainte",
    "ft": "fort",
    "mt": "mount",
    "mtn": "mountain",
    "hts": "heights",
    "jct": "junction",
    "spgs": "springs",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}


class Place(NamedTuple):
    """A resolved location."""

    latitude: float
    longitude: float
    label: str
    kind: str  # "zip" or "city"

    def as_location(self) -> str:
        """The `lat,lon` string the NWS provider consumes."""
        return f"{self.latitude},{self.longitude}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "label": self.label,
            "matched": self.kind,
        }


def normalize_city(name: str) -> str:
    """Fold a city name to its lookup key.

    Must stay identical to `scripts/build_geodata.py:normalize_city`; the keys
    in the dataset were written by that function.
    """
    folded = "".join(ch.lower() if ch.isalnum() else " " for ch in name)
    return " ".join(folded.split())


def city_keys(name: str) -> list[str]:
    """Lookup keys to try for a typed city name, best first.

    The typed form comes first; an abbreviation-expanded form follows only
    when it differs. Order matters: trying the expansion first would make
    "St Marys, KS" resolve to a Saint Marys that is a different town.
    """
    primary = normalize_city(name)
    if not primary:
        return []
    expanded = " ".join(
        ABBREVIATIONS.get(token, token) for token in primary.split()
    )
    return [primary] if expanded == primary else [primary, expanded]


class PlaceIndex:
    """In-memory index over the bundled dataset, loaded on first use.

    Lazy on purpose: importing this module must not cost 70k rows of parsing
    in a process that never geocodes anything (the test suite, mostly).
    """

    def __init__(
        self, places_path: Path = PLACES_PATH, states_path: Path = STATES_PATH
    ) -> None:
        self._places_path = places_path
        self._states_path = states_path
        self._lock = threading.Lock()
        self._loaded = False
        self._zips: dict[str, Place] = {}
        self._cities: dict[tuple[str, str], Place] = {}
        self._city_states: dict[str, list[str]] = {}
        self._state_codes: dict[str, str] = {}

    # -- loading ------------------------------------------------------------

    def load(self) -> None:
        """Parse the dataset. Idempotent and safe to call concurrently."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # another thread won the race
                return
            self._load_states()
            self._load_places()
            self._loaded = True

    def _load_states(self) -> None:
        codes: dict[str, str] = {}
        if not self._states_path.is_file():
            raise GeocoderDataError(self._states_path)
        with self._states_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                code = (row.get("code") or "").strip().lower()
                name = (row.get("name") or "").strip()
                if not code:
                    continue
                codes[code] = code
                if name:
                    # Both "tx" and "texas" (and "u s virgin islands") map to
                    # the code the city keys are stored under.
                    codes[normalize_city(name)] = code
        self._state_codes = codes

    def _load_places(self) -> None:
        if not self._places_path.is_file():
            raise GeocoderDataError(self._places_path)
        city_states: dict[str, set[str]] = defaultdict(set)
        with gzip.open(self._places_path, "rt", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                kind = row.get("kind") or ""
                key = row.get("key") or ""
                label = row.get("label") or key
                if kind == "zip":
                    self._zips[key] = Place(lat, lon, label, "zip")
                elif kind == "city" and "|" in key:
                    city, state = key.rsplit("|", 1)
                    self._cities[(city, state)] = Place(lat, lon, label, "city")
                    city_states[city].add(state)
        self._city_states = {
            city: sorted(states) for city, states in city_states.items()
        }

    # -- querying -----------------------------------------------------------

    @property
    def state_codes(self) -> Mapping[str, str]:
        self.load()
        return self._state_codes

    def resolve(self, query: str) -> Place:
        """Resolve a free-text US place to coordinates.

        Raises ValidationError for an unusable or ambiguous query and
        NotFoundError when the place is simply not in the dataset.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValidationError(
                "Enter a US ZIP code or a city and state.",
                details={"field": "location"},
            )
        self.load()
        cleaned = " ".join(query.strip().split())

        zip_match = _ZIP_RE.match(cleaned)
        if zip_match:
            return self._resolve_zip(zip_match.group(1))
        return self._resolve_city(cleaned)

    def _resolve_zip(self, code: str) -> Place:
        place = self._zips.get(code)
        if place is None:
            raise NotFoundError(
                f"No US postal code {code} is in the bundled place data.",
                details={"field": "location", "query": code},
            )
        return place

    def _resolve_city(self, cleaned: str) -> Place:
        city_text, state_text = self._split_state(cleaned)

        if state_text is not None:
            state = self._state_codes.get(normalize_city(state_text))
            if state is None:
                raise ValidationError(
                    f"{state_text!r} is not a US state or territory.",
                    details={"field": "location", "query": cleaned},
                )
            for key in city_keys(city_text):
                place = self._cities.get((key, state))
                if place is not None:
                    return place
            raise NotFoundError(
                f"No place named {city_text!r} is in {state.upper()}.",
                details={"field": "location", "query": cleaned},
            )

        # No state given: only a city unique across all states can be
        # resolved without guessing.
        for key in city_keys(cleaned):
            states = self._city_states.get(key)
            if not states:
                continue
            if len(states) > 1:
                raise ValidationError(
                    f"{cleaned!r} is in {len(states)} states "
                    f"({_summarize(states)}). Add the state, e.g. "
                    f"'{cleaned}, {states[0].upper()}'.",
                    details={
                        "field": "location",
                        "query": cleaned,
                        "states": [s.upper() for s in states],
                    },
                )
            return self._cities[(key, states[0])]

        raise NotFoundError(
            f"No US place matching {cleaned!r} is in the bundled place data. "
            "Try a ZIP code, or 'City, ST'.",
            details={"field": "location", "query": cleaned},
        )

    def _split_state(self, cleaned: str) -> tuple[str, str | None]:
        """Split "Austin, TX" / "Austin TX" into ("Austin", "TX").

        Returns a None state when the query names no state, which includes the
        case where the whole query IS a state name ("Washington"): consuming it
        as the state would leave no city, so it falls through to being treated
        as a city name instead.
        """
        if "," in cleaned:
            city_text, _, state_text = cleaned.rpartition(",")
            if city_text.strip() and state_text.strip():
                return city_text.strip(), state_text.strip()
            return cleaned, None

        tokens = cleaned.split()
        # Longest trailing match wins, so "Sioux Falls South Dakota" does not
        # stop at "Dakota" and "Kansas City" is not read as the state Kansas.
        for size in range(min(MAX_STATE_TOKENS, len(tokens) - 1), 0, -1):
            candidate = " ".join(tokens[-size:])
            if normalize_city(candidate) in self._state_codes:
                return " ".join(tokens[:-size]), candidate
        return cleaned, None


class GeocoderDataError(RuntimeError):
    """The bundled dataset is missing — a packaging fault, not user input."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"Bundled place data is missing at {path}. "
            "Regenerate it with: python scripts/build_geodata.py"
        )
        self.path = path


def _summarize(states: Iterable[str]) -> str:
    """Render candidate states for an error message, capped in length."""
    codes = [s.upper() for s in states]
    head = codes[:MAX_AMBIGUOUS_LISTED]
    if len(codes) > len(head):
        return ", ".join(head) + f", +{len(codes) - len(head)} more"
    return ", ".join(head)


#: Process-wide index. The dataset is read-only, so one copy serves every
#: request and the parse cost is paid once per process.
_INDEX = PlaceIndex()


def resolve(query: str) -> Place:
    """Resolve a US place name or ZIP to coordinates. See `PlaceIndex.resolve`."""
    return _INDEX.resolve(query)


def looks_like_coordinates(text: str) -> bool:
    """True when `text` is already a "lat,lon" pair and needs no geocoding.

    Deliberately permissive: anything with exactly one comma and two numeric
    halves is handed to the coordinate validator, which owns the real range
    checks and the error messages for them.
    """
    if not isinstance(text, str) or text.count(",") != 1:
        return False
    left, _, right = text.partition(",")
    try:
        float(left.strip())
        float(right.strip())
    except ValueError:
        return False
    return True


__all__ = [
    "ABBREVIATIONS",
    "GeocoderDataError",
    "Place",
    "PlaceIndex",
    "city_keys",
    "looks_like_coordinates",
    "normalize_city",
    "resolve",
]
