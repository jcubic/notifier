# Notifier — Agent Context

## IMPORTANT: Data-Driven Architecture

**Never add hardcoded logic to the code.** This project is entirely data-driven — all scraping logic, filtering, and behavior must be configured through `config.json`, not by modifying `index.py`. The code must remain generic. If a new feature or filter is needed, implement it as a generic config option that any rule can use, not as a special case in the code.

## What this is

A generic, config-driven web scraper that monitors websites for changes and sends email notifications. Config lives at `~/.notifier/config.json`, templates at `~/.notifier/templates/`, state at `~/.notifier/data/`. The script (`index.py`) is run periodically via cron using `run.sh` (which sets up pyenv).

## Key files

- `index.py` — main script, all logic in one file
- `config.json` — **the user's config is at `~/.notifier/config.json`**, the one in the project dir is an example
- `config.schema.json` — JSON Schema for validation, used by `--validate` and on every run
- `requirements.txt` — pinned minimum versions for all dependencies
- `skeleton/` — default config + templates copied to `~/.notifier/` on first run
- `templates/` — example Liquid templates (useme, bankier, hackernews)
- `run.sh` — cron entry point, sets up pyenv without loading full `.bashrc`
- `pyrightconfig.json` — pyright config pointing to pyenv's site-packages

## Architecture

1. `defs` — reusable scraping definitions (URL + CSS selectors + pagination)
   - `format` — `"html"` (default) or `"xml"` for RSS/Atom feeds and XML documents (uses lxml XML parser)
   - `userAgent` — optional custom User-Agent header for HTTP requests
   - `defs.commands` — reusable Liquid tag commands (e.g. `{% fresh date 604800 %}`, `{% today date %}`), usable in validator `test`/`match` expressions
2. `rules` — reference a def, add schedule (cron expr), email template, recipient
3. `input` — optional array on a rule for scraping multiple pages with different params (e.g. multiple stock symbols)
4. `validator` — optional filter on each input entry, object or array:
   - `{"test": "{{price}} > 10"}` — numexpr expression
   - `{"match": {"var": "title", "regex": "^Ask HN"}}` — regex match
   - `{"match": {"var": "skills", "include": ["Linux"]}}` — include items where list contains any listed string (exact element match)
   - `{"match": {"var": "skills", "exclude": ["Angular", "C#"]}}` — exclude items where list contains any listed string
   - `var` — direct variable lookup (preserves lists); `value` — Liquid template (always string); use one or the other
   - Array of validators = OR logic (any passes)
   - `require: true` on a validator makes it mandatory (AND with others); remaining validators still OR-combine
   - Object with both test+match = AND logic
   - `match.exist: false` passes when pattern does NOT match (useful for detecting something disappearing from a page)
5. Threshold crossing — all fetched items are saved with a `_valid` flag. When an item passes a validator after previously failing (or vice versa), it triggers a re-notification. Enables alerts like "price went back above $75k after dipping".
6. `parse: "json"` + `query` (JMESPath) — extracts structured data from embedded JSON. Variables are processed AFTER HTML extraction and ID assignment (so `{{id}}` is available in `query.path`). Parsed JSON is cached per page via module-level `_json_cache` keyed by `(id(soup), selector)`, cleared at start of each `parse_items()` call.

## How to add a new page to scrape

1. Inspect the target page HTML (use browser DevTools or `python3 -c "import requests; ..."`)
2. Add a definition to `defs` in `~/.notifier/config.json`:
   - `url` — supports `{{param}}` Liquid variables
   - `format` — `"html"` (default) or `"xml"` for RSS/Atom feeds and XML documents
   - `userAgent` — optional custom User-Agent string (some feeds block default agents)
   - `query.type` — `"list"` (multiple items) or `"single"` (one item per page)
   - `query.selector` — CSS selector for item container (for XML: element names like `item`, `entry`)
   - `query.variables` — each variable: `selector` + `value` (`type: "text"` or `type: "attribute"` with `name`)
   - Optional: `pagination`, `filter`, `id`, `value.parse: "number"|"money"|"list"|"json"`, `value.regex`, `value.prefix`
   - Optional: `sibling: true` on a variable to search next sibling element
   - Optional: `collect: true` on a variable to extract ALL matching elements as a list (use `{% for %}` in templates)
   - Optional: `selector: ":self"` to reference the container element itself (e.g. when container is `<a>` and you need its `href`)
   - Optional: `value.parse: "json"` + `value.query` with JMESPath for extracting structured data from embedded JSON (Next.js `__NEXT_DATA__`, JSON in attributes, etc.). `value.query.path` supports Liquid variables like `{{id}}` rendered per item. See README "JSON extraction" section.
3. Add a rule to `rules`:
   - `ref` — definition name
   - `name` — unique, used as state filename
   - `schedule` — cron expression (e.g. `"0 8 * * *"`)
   - `subject` — Liquid template for email subject
   - `template` — path to body template relative to `~/.notifier/`
   - `email` — recipient
   - `params` or `input` — parameter values (input supports array + validators)
4. Create a Liquid template file. Available vars: `{{ count }}`, `{{ now }}`, `{{ search_url }}`, `{% for item in items %}...{% endfor %}` with `{{ item.index }}` and all extracted variables
5. Validate: `python3 index.py --validate`

## CLI flags

```
--validate        validate config and exit
--dry-run         fetch and display data, no emails, no state changes
--save-email      save emails to file instead of sending
--force           ignore schedules, run all rules
--force <rule>    ignore schedule, run only the named rule (errors if name not found)
-v, --verbose     show detailed progress output
-q, --quiet       suppress all output including errors
```

## Validation

`validate_config()` runs in this order, each step exits on failure:

1. JSON Schema validation (`config.schema.json`) via `jsonschema.Draft202012Validator`
2. Cron syntax for each rule's `schedule` (string or array) via `croniter.is_valid()`
3. CSS selector syntax for `query.selector`, `query.expect[]`, `query.filter.selector`, `pagination.selector`, and every variable's `selector` via `soup.select()` (catches `SelectorSyntaxError`). `:self` is skipped.
4. JMESPath syntax for `value.query.path` and `value.query.variables.*.path` via `jmespath.compile()`. Liquid placeholders `{{...}}` are replaced with `0` before checking so they don't fail parsing.

## Dependencies

Python 3.12+ with: `requests`, `beautifulsoup4`, `lxml`, `python-liquid`, `croniter`, `numexpr`, `jsonschema`, `babel`, `jmespath`

Install via `pip install -r requirements.txt`. Installed for both pyenv Python (3.12) and system Python (3.14) at `/usr/bin/python3`.

## Error handling

- Missing dependency at import → error email sent, exits
- Invalid config → error email with validation errors, exits
- Runtime crash in `main()` → error email with traceback
- `send_error_email()` uses only stdlib (no third-party deps) so it works even when the crash is caused by a missing library

## Cron setup

```cron
*/5 * * * * /home/kuba/projects/jcubic/notifier/run.sh >> /home/kuba/.notifier/notifier.log 2>&1
```

There's also a symlink at `~/bin/notifier` → `index.py` (use `os.path.realpath(__file__)` not `abspath` to resolve paths correctly).
