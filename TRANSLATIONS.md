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
translations/he/LC_MESSAGES/messages.mo    compiled — COMMITTED, see below
```

## Deployment: the `.mo` files are committed to git

**Decision:** compiled catalogs are committed, not built during deploy.

Render's build command is `pip install -r requirements.txt`. Adding a
`pybabel compile` step there would put the catalog behind a build-time
instruction that lives outside this repo, in the Render dashboard — and if that
step were ever dropped or reordered, the failure mode is the worst kind:
**a missing `.mo` does not error, it silently serves English.** Nobody gets a
red build; the Hebrew simply stops, and the first report comes from a user.

Committing the `.mo` makes the artifact that ships the same artifact that was
reviewed, and needs no change to the deploy pipeline.

The cost of this choice is the opposite risk — editing a `.po` and forgetting to
recompile, which ships a stale translation just as silently. That is covered by
a CI test asserting every committed `.mo` matches its `.po`, so the mistake
fails the build instead of reaching production.

**Therefore: after editing any `.po`, run `pybabel compile -d translations` and
commit the `.mo` in the same commit.**

## Rules for translatable strings

- Named placeholders only: `_('Deleted %(name)s', name=client)`. Never an
  f-string inside `_()` — the extractor stores the already-interpolated text,
  so every value produces a different, untranslatable string.
- Never concatenate translated fragments. Word order differs by language, and
  in Hebrew the direction does too. One sentence, one string.
- Do not translate: log messages, exception internals, webhook payload fields,
  `X-Secret` values, machine-facing routes, or database values. The product name
  "Soulful Content Engine" is not translated either.

## Two conventions that are easy to miss in English review

**Wrap Latin-script values in `<bdi>` when they sit inside sentence flow.**
A client name, email, URL, handle or filename rendered mid-sentence in Hebrew
drags the surrounding punctuation to the wrong end of the line — the trailing
full stop of a sentence ends up before the value instead of after it. `<bdi>`
isolates the run and the punctuation stays put:

```
{% trans email=email %}Welcome, <strong><bdi>{{ email }}</bdi></strong>. Choose a password.{% endtrans %}
```

This is invisible to anyone reviewing the page in English, which is why it has
to be a rule rather than something spotted case by case. It applies to anything
rendered into sentence flow later too — the media pipeline will eventually put
filenames and URLs into these same templates.

**`data-label` attributes are content, not hooks — translate them.**
`mobile.css` renders the wide tables as stacked cards and uses `data-label` as
the visible column heading. Left untranslated, mobile Hebrew shows English
headings — the least-reviewed combination in the app, so the bug would live a
long time.
