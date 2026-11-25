"""Build the bundled offline US place -> coordinate dataset.

NWS (api.weather.gov) only accepts coordinates, and the app must not depend on
a live geocoding service, so place-name resolution is served from a dataset
committed to the repo. This script regenerates it; nothing in the running
application ever downloads anything.

Source: GeoNames postal-code export, CC BY 4.0.
    https://download.geonames.org/export/zip/
Covers the US plus the territories NWS serves (PR, VI, GU, AS, MP).

Usage:
    python scripts/build_geodata.py                 # download, then build
    python scripts/build_geodata.py --source-dir D  # use already-downloaded
                                                    # <CC>.txt files in D

Output: app/data/us_places.csv.gz, with one row per record:

    kind,key,lat,lon,label

    kind=zip   key=the 5-digit postal code       label="Austin, TX"
    kind=city  key=normalized "city|state"       label="Austin, TX"

A city's coordinate is the mean of its postal-code coordinates (a centroid),
which is the only defensible point available: the source carries no
population with which to pick a "main" one. Nothing is interpolated beyond
that, and a record with no usable coordinate is dropped rather than guessed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "app" / "data" / "us_places.csv.gz"
STATES_PATH = PROJECT_ROOT / "app" / "data" / "us_states.csv"

BASE_URL = "https://download.geonames.org/export/zip"
#: US plus the territories within NWS coverage. GeoNames ships these as
#: separate files; the US file contains none of them.
COUNTRIES = ("US", "PR", "VI", "GU", "AS", "MP")

# GeoNames postal-code column layout (tab separated, no header).
COL_POSTAL = 1
COL_PLACE = 2
COL_STATE_NAME = 3
COL_STATE_CODE = 4
COL_LAT = 9
COL_LON = 10
MIN_COLUMNS = 11

# Territories whose GeoNames rows carry no state code: the country code *is*
# the postal state code the USPS uses.
TERRITORY_STATE_CODES = {
    "PR": ("PR", "Puerto Rico"),
    "VI": ("VI", "U.S. Virgin Islands"),
    "GU": ("GU", "Guam"),
    "AS": ("AS", "American Samoa"),
    "MP": ("MP", "Northern Mariana Islands"),
}


def normalize_city(name: str) -> str:
    """Fold a city name to its lookup key.

    Case, punctuation and repeated whitespace are all ignored, so "St. Louis",
    "st louis" and "ST  LOUIS" land on one key.
    """
    folded = "".join(ch.lower() if ch.isalnum() else " " for ch in name)
    return " ".join(folded.split())


def download(country: str) -> bytes:
    url = f"{BASE_URL}/{country}.zip"
    print(f"  fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.read(f"{country}.txt")


def read_rows(country: str, source_dir: Path | None) -> list[list[str]]:
    if source_dir is None:
        text = download(country).decode("utf-8")
    else:
        candidates = [source_dir / f"{country}.txt", source_dir / country / f"{country}.txt"]
        path = next((c for c in candidates if c.is_file()), None)
        if path is None:
            raise SystemExit(f"No {country}.txt found in {source_dir}")
        text = path.read_text(encoding="utf-8")
    return [line.split("\t") for line in text.splitlines() if line.strip()]


def build(source_dir: Path | None, output: Path, states_output: Path) -> tuple[int, int, int]:
    zips: dict[str, tuple[float, float, str]] = {}
    # Accumulate per city so the centroid is computed over every one of its
    # postal codes, not just the last one seen.
    city_points: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    city_labels: dict[tuple[str, str], str] = {}
    # code -> full name, so "Austin, Texas" resolves as well as "Austin, TX".
    # Derived from the source rather than hand-typed.
    state_names: dict[str, str] = {}

    for country in COUNTRIES:
        rows = read_rows(country, source_dir)
        kept = 0
        for row in rows:
            if len(row) < MIN_COLUMNS:
                continue
            postal = row[COL_POSTAL].strip()
            place = row[COL_PLACE].strip()
            # Territory files put a municipality/island code in the state
            # column (PR's Adjuntas reads "001"), so the country code is the
            # authority there. Trusting the column verbatim produced labels
            # like "Adjuntas, 001" and a lookup key no user would type.
            if country in TERRITORY_STATE_CODES:
                state = TERRITORY_STATE_CODES[country][0]
            else:
                state = row[COL_STATE_CODE].strip().upper()
            # A row with no place, no state or no coordinate cannot be
            # resolved to anything; it is dropped, never defaulted.
            if not place or not state:
                continue
            try:
                lat = float(row[COL_LAT])
                lon = float(row[COL_LON])
            except ValueError:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue

            label = f"{place}, {state}"
            if postal:
                zips[postal] = (lat, lon, label)
            key = (normalize_city(place), state.lower())
            city_points[key].append((lat, lon))
            city_labels.setdefault(key, label)
            if country in TERRITORY_STATE_CODES:
                state_names.setdefault(state, TERRITORY_STATE_CODES[country][1])
            else:
                full = row[COL_STATE_NAME].strip()
                if full:
                    state_names.setdefault(state, full)
            kept += 1
        print(f"  {country}: {kept} usable rows", file=sys.stderr)

    output.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 so rebuilding identical input produces an identical file and
    # does not show up as a spurious diff.
    with gzip.GzipFile(filename="", mode="wb", fileobj=output.open("wb"), mtime=0) as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(("kind", "key", "lat", "lon", "label"))
            for postal in sorted(zips):
                lat, lon, label = zips[postal]
                writer.writerow(("zip", postal, f"{lat:.4f}", f"{lon:.4f}", label))
            for key in sorted(city_points):
                points = city_points[key]
                lat = sum(p[0] for p in points) / len(points)
                lon = sum(p[1] for p in points) / len(points)
                writer.writerow(
                    (
                        "city",
                        f"{key[0]}|{key[1]}",
                        f"{lat:.4f}",
                        f"{lon:.4f}",
                        city_labels[key],
                    )
                )

    states_output.parent.mkdir(parents=True, exist_ok=True)
    with states_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("code", "name"))
        for code in sorted(state_names):
            writer.writerow((code, state_names[code]))

    return len(zips), len(city_points), len(state_names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Directory holding already-downloaded <CC>.txt files.",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--states-output", type=Path, default=STATES_PATH)
    args = parser.parse_args(argv)

    zip_count, city_count, state_count = build(
        args.source_dir, args.output, args.states_output
    )
    size_kb = args.output.stat().st_size / 1024
    print(
        f"wrote {args.output.relative_to(PROJECT_ROOT)}: "
        f"{zip_count} postal codes, {city_count} cities, {size_kb:.0f} KiB",
        file=sys.stderr,
    )
    print(
        f"wrote {args.states_output.relative_to(PROJECT_ROOT)}: "
        f"{state_count} states and territories",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
