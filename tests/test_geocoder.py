"""Tests for the offline place-name geocoder.

Most tests build a PlaceIndex over a tiny fixture dataset so they assert
behaviour rather than the contents of the 70k-row bundled file. A handful of
tests do read the real dataset, because "Austin, TX resolves" is exactly the
promise the feature makes and a fixture cannot verify it.
"""

from __future__ import annotations

import csv
import gzip
from pathlib import Path

import pytest

from app.errors import NotFoundError, ValidationError
from app.services import geocoder

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

FIXTURE_PLACES = [
    ("zip", "78701", 30.2713, -97.7426, "Austin, TX"),
    ("zip", "60601", 41.8858, -87.6181, "Chicago, IL"),
    ("city", "austin|tx", 30.3094, -97.7623, "Austin, TX"),
    ("city", "austin|mn", 43.6666, -92.9746, "Austin, MN"),
    ("city", "chicago|il", 41.8542, -87.6761, "Chicago, IL"),
    ("city", "saint louis|mo", 38.6403, -90.2863, "Saint Louis, MO"),
    ("city", "sioux falls|sd", 43.5900, -96.7507, "Sioux Falls, SD"),
    ("city", "washington|dc", 38.8965, -77.0214, "Washington, DC"),
    ("city", "kansas city|mo", 39.1068, -94.5736, "Kansas City, MO"),
    ("city", "kansas city|ks", 39.1067, -94.6275, "Kansas City, KS"),
    ("city", "san juan|pr", 18.4663, -66.1057, "San Juan, PR"),
]

FIXTURE_STATES = [
    ("TX", "Texas"),
    ("MN", "Minnesota"),
    ("IL", "Illinois"),
    ("MO", "Missouri"),
    ("KS", "Kansas"),
    ("SD", "South Dakota"),
    ("DC", "District of Columbia"),
    ("PR", "Puerto Rico"),
]


@pytest.fixture
def index(tmp_path: Path) -> geocoder.PlaceIndex:
    places = tmp_path / "places.csv.gz"
    with gzip.open(places, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("kind", "key", "lat", "lon", "label"))
        writer.writerows(FIXTURE_PLACES)

    states = tmp_path / "states.csv"
    with states.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("code", "name"))
        writer.writerows(FIXTURE_STATES)

    return geocoder.PlaceIndex(places_path=places, states_path=states)


# --------------------------------------------------------------------------- #
# postal codes
# --------------------------------------------------------------------------- #


def test_resolves_a_zip_code(index):
    place = index.resolve("78701")
    assert (place.latitude, place.longitude) == (30.2713, -97.7426)
    assert place.label == "Austin, TX"
    assert place.kind == "zip"


def test_zip_plus_four_is_truncated_to_the_five_digit_code(index):
    assert index.resolve("78701-1234").label == "Austin, TX"


def test_surrounding_whitespace_is_ignored(index):
    assert index.resolve("  78701  ").label == "Austin, TX"


def test_unknown_zip_is_not_found(index):
    with pytest.raises(NotFoundError) as excinfo:
        index.resolve("99999")
    assert excinfo.value.details["field"] == "location"


# --------------------------------------------------------------------------- #
# city + state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "query",
    [
        "Austin, TX",
        "Austin, Texas",
        "Austin TX",
        "austin texas",
        "  AUSTIN ,  tx ",
        "Austin, tx.",
    ],
)
def test_city_state_spellings_all_resolve(index, query):
    place = index.resolve(query)
    assert place.label == "Austin, TX"
    assert place.kind == "city"


def test_multi_word_state_name_without_a_comma(index):
    # Must not stop at "Dakota" and look for a city called "Sioux Falls South".
    assert index.resolve("Sioux Falls South Dakota").label == "Sioux Falls, SD"


def test_city_sharing_a_word_with_a_state_is_not_eaten(index):
    # "Kansas City" must not be read as city="" + state="Kansas".
    assert index.resolve("Kansas City, MO").label == "Kansas City, MO"


def test_territory_resolves(index):
    assert index.resolve("San Juan, PR").label == "San Juan, PR"


def test_unknown_state_is_a_validation_error(index):
    with pytest.raises(ValidationError) as excinfo:
        index.resolve("Paris, France")
    assert "not a US state" in str(excinfo.value)


def test_known_state_but_unknown_city_is_not_found(index):
    with pytest.raises(NotFoundError):
        index.resolve("Nowhereville, TX")


# --------------------------------------------------------------------------- #
# ambiguity: never guessed
# --------------------------------------------------------------------------- #


def test_bare_city_unique_to_one_state_resolves(index):
    assert index.resolve("Chicago").label == "Chicago, IL"


def test_bare_ambiguous_city_lists_the_states_instead_of_guessing(index):
    with pytest.raises(ValidationError) as excinfo:
        index.resolve("Austin")
    error = excinfo.value
    assert error.details["states"] == ["MN", "TX"]
    # The message must tell the user how to fix it, not just that it failed.
    assert "Austin, MN" in str(error)


def test_ambiguity_message_is_capped(index, monkeypatch):
    monkeypatch.setattr(geocoder, "MAX_AMBIGUOUS_LISTED", 1)
    with pytest.raises(ValidationError) as excinfo:
        index.resolve("Kansas City")
    assert "+1 more" in str(excinfo.value)


def test_a_query_that_is_only_a_state_name_is_not_a_place(index):
    # "Texas" would otherwise consume itself as the state and search for "".
    with pytest.raises(NotFoundError):
        index.resolve("Texas")


# --------------------------------------------------------------------------- #
# abbreviations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("query", ["St. Louis, MO", "St Louis MO", "Saint Louis, MO"])
def test_saint_abbreviation_resolves(index, query):
    assert index.resolve(query).label == "Saint Louis, MO"


def test_typed_form_is_tried_before_the_expansion(index):
    """A real "St"/"Ft" name must win over its expanded spelling."""
    keys = geocoder.city_keys("St Louis")
    assert keys == ["st louis", "saint louis"]


def test_city_keys_returns_one_key_when_nothing_expands(index):
    assert geocoder.city_keys("Chicago") == ["chicago"]


# --------------------------------------------------------------------------- #
# bad input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("query", ["", "   ", None, 5])
def test_unusable_query_is_a_validation_error(index, query):
    with pytest.raises(ValidationError):
        index.resolve(query)


def test_missing_dataset_raises_a_packaging_error(tmp_path: Path):
    broken = geocoder.PlaceIndex(
        places_path=tmp_path / "nope.csv.gz", states_path=tmp_path / "nope.csv"
    )
    with pytest.raises(geocoder.GeocoderDataError) as excinfo:
        broken.resolve("78701")
    assert "build_geodata" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# coordinate passthrough
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text", ["30.27,-97.74", " 30.27 , -97.74 ", "0,0", "-45.5,122.6"]
)
def test_coordinates_are_recognized_and_skip_geocoding(text):
    assert geocoder.looks_like_coordinates(text) is True


@pytest.mark.parametrize(
    "text", ["Austin, TX", "78701", "", "30.27", "a,b", "1,2,3", None, 5]
)
def test_non_coordinates_are_rejected(text):
    assert geocoder.looks_like_coordinates(text) is False


# --------------------------------------------------------------------------- #
# the real bundled dataset
# --------------------------------------------------------------------------- #


def test_bundled_dataset_is_present():
    assert geocoder.PLACES_PATH.is_file(), "run scripts/build_geodata.py"
    assert geocoder.STATES_PATH.is_file(), "run scripts/build_geodata.py"


def test_bundled_dataset_covers_all_states_and_territories():
    state_codes = geocoder._INDEX.state_codes
    for code in ("tx", "ak", "hi", "dc", "pr", "gu", "vi", "as", "mp"):
        assert code in state_codes, code
    # Full names must resolve too, or "Austin, Texas" fails.
    for name in ("texas", "district of columbia", "puerto rico"):
        assert name in state_codes, name


@pytest.mark.parametrize(
    "query,expected",
    [
        ("78701", "Austin, TX"),
        ("Austin, TX", "Austin, TX"),
        ("Austin, Texas", "Austin, TX"),
        ("Washington, DC", "Washington, DC"),
        ("Chicago", "Chicago, IL"),
        ("St. Louis, MO", "Saint Louis, MO"),
        ("Juneau AK", "Juneau, AK"),
        ("Honolulu, HI", "Honolulu, HI"),
        ("San Juan, PR", "San Juan, PR"),
    ],
)
def test_real_dataset_resolves_common_queries(query, expected):
    assert geocoder.resolve(query).label == expected


def test_real_dataset_coordinates_are_plausible():
    austin = geocoder.resolve("Austin, TX")
    assert 29.5 < austin.latitude < 31.0
    assert -98.5 < austin.longitude < -97.0


def test_real_dataset_refuses_to_guess_springfield():
    with pytest.raises(ValidationError) as excinfo:
        geocoder.resolve("Springfield")
    assert len(excinfo.value.details["states"]) > 5
