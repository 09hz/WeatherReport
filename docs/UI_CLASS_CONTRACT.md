# UI class contract (FROZEN)

This file is the shared vocabulary between `app/static/css/brutalist.css`
(WeatherReport-ic2 / UI-1) and the page templates under `app/templates/`
(UI-2, UI-3). It exists so the stylesheet and the pages can be written in
parallel without either side guessing at the other's names.

Rules for everyone touching the UI:

- **Templates may only use class names listed here.** Need a new one? Add it
  to this file in the same change, with its semantics.
- **The stylesheet must style every class listed here**, including the ones no
  page uses yet — a page added later must not arrive unstyled.
- No inline `style=` attributes, no `<style>` blocks, no utility-soup
  (`.mt-4`, `.text-sm`). Semantics only.
- Modifiers use `--` (`.notice--error`); children use `__`
  (`.panel__title`). Blocks are single words.

Design constraints inherited from the product owner (UI-1 enforces them in
CSS): black / white / grey only, no transitions, no animations, no
`border-radius`, no `box-shadow`, no gradients, no webfonts or CDN links.

## Document shell

| Class | Element | Semantics |
|---|---|---|
| `page` | `<body>` | Root wrapper. Owns the page background and base type. |
| `skip-link` | first `<a>` in body | Keyboard jump to `#main`. Visible on focus. |
| `masthead` | `<header>` | Site header band. Heavy bottom border. |
| `masthead__title` | `<a>` inside masthead | Product wordmark, links to `/ui`. |
| `masthead__tagline` | `<p>` | One-line subtitle. Never essential information. |
| `nav` | `<nav>` | Primary navigation strip. |
| `nav__link` | `<a>` | One navigation target. |
| `nav__link--active` | `<a>` | The current section. Exactly zero or one per page. |
| `main` | `<main id="main">` | Page body region. |
| `footer` | `<footer>` | Build/version line and API link. |

## Page structure

| Class | Semantics |
|---|---|
| `view` | One page's outermost content block inside `main`. |
| `view__title` | The page's `<h1>`. One per page. |
| `view__lead` | Optional one-paragraph description under the title. |
| `panel` | A bordered section box. The workhorse container. |
| `panel__title` | `<h2>` header bar of a panel. |
| `panel__body` | Padded content region of a panel. |
| `stack` | Vertical flow container; separates children with margin only. |
| `grid` | Exposed-rule grid, one column by default. |
| `grid--2` | Two columns above the narrow breakpoint. |
| `grid--3` | Three columns above the narrow breakpoint. |

## Data display

| Class | Semantics |
|---|---|
| `datagrid` | `<dl>` of label/value pairs for a single record. |
| `datagrid__key` | `<dt>` — the field label. |
| `datagrid__value` | `<dd>` — the field value. |
| `datagrid__value--null` | `<dd>` whose underlying API value was `null`. Pairs with the `placeholder` text. |
| `table` | `<table>` of many records. Always has a `<caption>` or a preceding `panel__title`. |
| `table__head` | `<thead>`. |
| `table__row` | `<tr>` in the body. |
| `table__cell` | `<td>` / `<th>`. |
| `table__cell--numeric` | Right-aligned numeric cell. |
| `table__empty` | The single cell shown when a result set is empty. |
| `placeholder` | Inline span for a missing value. The TEMPLATE owns the glyph: `helpers.present()` supplies the em dash as data and the template prints it inside the span, followed by a `visually-hidden` "no value". The stylesheet must NOT add it via `::before`, or it doubles. **Never** render `None` or an empty cell. |
| `mono` | Force monospace on a fragment (IDs, coordinates, timestamps). |
| `tag` | Small bordered inline label (provider name, units, cache hit/miss). |

## Forms

| Class | Semantics |
|---|---|
| `form` | `<form>`. |
| `field` | Wrapper for one label + control + optional hint/error. |
| `field--invalid` | `field` whose control failed validation. |
| `field__label` | `<label>`. |
| `field__input` | `<input>` of any text-like type, and `<select>`. |
| `field__textarea` | `<textarea>`. |
| `field__hint` | Static help text (format, limits). |
| `field__error` | Validation message for this field. Rendered only inside `field--invalid`. |
| `actions` | Row holding the submit/cancel controls of a form. |
| `btn` | `<button>` / `<a>` acting as a control. Base look for all buttons. |
| `btn--primary` | The one affirmative action of a form. |
| `btn--danger` | Destructive action (delete). |
| `btn--quiet` | Secondary / cancel action. |

## Messages

| Class | Semantics |
|---|---|
| `notice` | Page-level message block. |
| `notice--ok` | Succeeded action (entry saved, snapshot stored). |
| `notice--warn` | Degraded state (provider unconfigured, cache stale). |
| `notice--error` | Failed action or request validation failure. |
| `notice__title` | Optional bold first line of a notice. |
| `notice__detail` | `<ul>`/`<p>` of specifics (API `details` payload). |

## Pagination

| Class | Semantics |
|---|---|
| `pager` | Wrapper for the limit/offset controls of a list. |
| `pager__status` | `showing X–Y of TOTAL` text. Driven by the API's `total_count`. |
| `pager__link` | Previous/next link. |
| `pager__link--disabled` | Rendered when `offset == 0` or `has_more` is false. Must not be clickable. |

## Accessibility helpers

| Class | Semantics |
|---|---|
| `visually-hidden` | Present to screen readers, removed from the visual layout. |

## Route map (owned by UI-0, filled in by UI-2/UI-3)

| Route | Template | Issue |
|---|---|---|
| `GET /ui` | `ui/index.html` | UI-0 |
| `GET,POST /ui/weather` | `ui/weather_current.html` | UI-2 |
| `GET /ui/weather/snapshots` | `ui/weather_snapshots.html` | UI-2 |
| `GET /ui/journal` | `ui/journal_list.html` | UI-3 |
| `GET,POST /ui/journal/new` | `ui/journal_form.html` | UI-3 |
| `GET,POST /ui/journal/<id>/edit` | `ui/journal_form.html` | UI-3 |
| `GET,POST /ui/journal/<id>/delete` | `ui/journal_delete.html` | UI-3 |

Saving a snapshot is a `POST /ui/weather` with `action=save`, which redirects to
the snapshot list — *not* a `POST` to `/ui/weather/snapshots`, which is GET
only. An earlier draft of this table said otherwise.

`GET /` stays the JSON endpoint index. See [[UI]] (docs/UI.md, UI-5) for why.
