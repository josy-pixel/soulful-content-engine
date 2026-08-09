# Translations

The interface is translated with Flask-Babel using the standard gettext
workflow. Source strings are written in **English** — that is the default locale
and it has no catalog, so English renders straight from the source text and
cannot drift from it.

Locales live in `i18n.py` (`LOCALES`). Adding a language is a translation-file
job: add its code, display name and text direction there, create the catalog,
translate, compile. No other code changes.

Run everything from the repo root, inside the virtualenv.

## Extract — after adding or changing any user-facing string

```
pybabel extract -F babel.cfg -k _l -k lazy_gettext --ignore-dirs '.* ._* tests scripts' -o messages.pot .
```

`-k _l -k lazy_gettext` picks up the lazy variants used for strings evaluated at
import time, which a plain `_()` scan would miss.

`--ignore-dirs` keeps test and helper-script strings out of the catalog so
nobody is asked to translate them. **Pass the list in full** — it replaces the
default rather than adding to it, and dropping `.*` would pull the entire
`.venv` into the scan (1290 files instead of 35).

## Create a new language catalog — once per language

```
pybabel init -i messages.pot -d translations -l he
```

Replace `he` with the new code. Never run this against a language that already
has a catalog — it overwrites the translations. Use `update` instead.

## Update existing catalogs — after every extract

```
pybabel update -i messages.pot -d translations
```

Merges new and changed source strings into every existing `.po`, keeping the
work already done. Changed strings are marked `#, fuzzy` and must be reviewed —
a fuzzy entry is NOT used at runtime.

## Compile — required before the app can serve a translation

```
pybabel compile -d translations
```

Produces the `.mo` files gettext actually reads. **An uncompiled or stale `.mo`
silently falls back to English**, which looks like a missing translation rather
than a build error, so compile whenever a `.po` changes.

## Checking your work

```
pybabel compile -d translations --statistics
```

Reports translated / fuzzy / untranslated counts per catalog.

## Layout

```
babel.cfg                       what pybabel scans
messages.pot                    extracted source strings (committed)
translations/he/LC_MESSAGES/messages.po    Hebrew catalog (committed, edited)
translations/he/LC_MESSAGES/messages.mo    compiled (see Stage 7 decision)
```

## Rules for translatable strings

- Named placeholders only: `_('Deleted %(name)s', name=client)`. Never an
  f-string inside `_()` — the extractor stores the already-interpolated text,
  so every value produces a different, untranslatable string.
- Never concatenate translated fragments. Word order differs by language, and
  in Hebrew the direction does too. One sentence, one string.
- Do not translate: log messages, exception internals, webhook payload fields,
  `X-Secret` values, machine-facing routes, or database values.
