# Bundled place data

Offline US place-name geocoding for the weather lookup. NWS
(api.weather.gov) accepts coordinates only, so a typed `Austin, TX` or
`78701` is resolved here before any request is made. **Nothing in the
running application downloads this data** — that is the point of bundling
it, and it is why the app's only outbound host is still api.weather.gov.

| File | Contents |
| --- | --- |
| `us_places.csv.gz` | 41,197 postal codes and 29,668 cities as `kind,key,lat,lon,label` |
| `us_states.csv` | 57 state/territory codes and full names |

Coverage is the US plus the territories NWS serves: PR, VI, GU, AS, MP.

A city's coordinate is the **centroid of its postal codes**, not a precise
address. The source carries no population figures, so there is no defensible
way to pick a "main" point, and the resolution is irrelevant anyway: the
observation comes from the nearest reporting station, typically kilometres
off.

## Regenerating

    python scripts/build_geodata.py

The script downloads the current GeoNames export and rewrites both files.
Run it when the data goes stale (new ZIPs appear a few times a year). The
output is deterministic — identical input produces a byte-identical file, so
a no-op rebuild shows no diff.

## Attribution

Derived from the [GeoNames](https://www.geonames.org/) postal-code export,
licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Attribution is a licence condition, carried in the UI footer of every page
(`app/templates/base.html`). Do not remove it.
