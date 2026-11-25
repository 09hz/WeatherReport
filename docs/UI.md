# Weather-journal UI

The server-rendered front end for this project's JSON API. No JavaScript, no
colour, no network dependencies. Related notes: [[UI_CLASS_CONTRACT]],
[[PROJECT_MEMORY]].

## Running it

```bash
.venv/bin/python run.py
# or: FLASK_APP=app .venv/bin/python -m flask run
```

Then open <http://127.0.0.1:5000/ui>. Everything works without credentials:
with no provider configured, every page still renders and the header carries a
standing warning, while live lookups answer with a readable error page instead
of a blank screen. Stored snapshots and the whole journal are unaffected,
because they only touch the local database.

## Route map

| Method(s) | Path | Page |
|---|---|---|
| GET | `/ui` | Overview, links to both halves and to the machine interface |
| GET, POST | `/ui/weather` | Lookup form; POST renders current conditions, or saves a snapshot and redirects |
| GET | `/ui/weather/snapshots` | Stored snapshots, paginated, optional `?location=` filter |
| GET | `/ui/journal` | Entries, with `?q=` search and `?location=` filter, paginated |
| GET, POST | `/ui/journal/new` | Create an entry |
| GET, POST | `/ui/journal/<id>/edit` | Edit an entry |
| GET, POST | `/ui/journal/<id>/delete` | Confirm, then delete an entry |

Two conventions hold across all of them:

- **Every write is POST-then-redirect**, so refreshing a page never repeats a
  save, an edit or a deletion.
- **Every expected failure is a page.** Views catch `AppError` and render it as
  a `.notice--error` block while keeping the error's own status code, so a
  missing entry is a 404 *page* and an unconfigured provider is a 503 *page* —
  never a JSON envelope, never a traceback. A validation failure additionally
  re-renders the form with the user's input intact and the offending control
  marked, because losing a long description to a mistyped date is the worst
  thing this UI could do.

A `GET` on the lookup page deliberately does not call the provider; it only
prefills the form from `?location=`. Navigating must not spend a rate-limit
slot.

## Design constraints

From the product owner, and treated as non-negotiable: black, white and grey
only; no transitions; no animations; no `border-radius`; no `box-shadow`; no
gradients; no webfonts or CDN links; no JavaScript. Because colour is
unavailable, state is carried by border weight, black/white inversion and
type — severity, emphasis and focus all read without a single hue.

`tests/test_ui_foundation.py` asserts the prohibitions directly against
`app/static/css/brutalist.css`, so the constraints survive a future rewrite of
the stylesheet rather than living only in this document.

## Why /ui and not /

`GET /` returns the JSON index of API endpoints, and the API's own test suite
asserts that response. Mounting the HTML UI at the root would have meant either
breaking those assertions or content-negotiating the root route, and a route
whose body depends on an `Accept` header is a route that breaks differently for
browsers, curl and tests. A prefix costs nothing and keeps one URL with one
answer. The footer of every page links back to the JSON index so the machine
interface is never hidden.

For the same reason the UI talks to the Python service layer directly rather
than calling its own HTTP API: a view issuing a request back into the app it is
running inside would deadlock the single-threaded development server and would
charge every page view to the rate limiter twice.

## The class contract

[[UI_CLASS_CONTRACT]] freezes the CSS class vocabulary: every class name the
templates may use, with its semantics, plus the design prohibitions above.

It exists because the stylesheet and the pages were written in parallel by
different authors. A frozen vocabulary is what let them agree without
coordinating: the stylesheet styles every listed class, including ones no page
uses yet, so a page added later never arrives unstyled, and the templates use
nothing that is not listed. A test enforces the second half of that bargain —
if a template introduces an unlisted class, the suite fails rather than
quietly rendering it unstyled.

Adding a class therefore means editing the contract **in the same change**, and
styling it in the same change too. Inline `style=` attributes and `<style>`
blocks are not an escape hatch; they are forbidden.

## Where the code lives

```
app/routes/ui/__init__.py   blueprint (/ui), nav, per-request chrome context
app/routes/ui/helpers.py    page_window, pager, notice_from_error,
                            field_errors, present — shared so every page
                            paginates and renders nulls identically
app/routes/ui/weather.py    the weather pages
app/routes/ui/journal.py    the journal pages
app/templates/base.html     masthead / nav / main / footer
app/templates/ui/*.html     one file per page
app/static/css/brutalist.css
tests/test_ui_foundation.py the shell, the constraints, the contract
tests/test_ui_pages.py      every route, every write flow, every error path
```
