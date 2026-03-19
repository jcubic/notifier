# Notifier — Agent Context

## What this is

A generic, config-driven web scraper that monitors websites for changes and sends email notifications. Config lives at `~/.notifier/config.json`, templates at `~/.notifier/templates/`, state at `~/.notifier/data/`. The script (`index.py`) is run periodically via cron using `run.sh` (which sets up pyenv).

## Key files

- `index.py` — main script, all logic in one file
- `config.json` — **the user's config is at `~/.notifier/config.json`**, the one in the project dir is an example
- `config.schema.json` — JSON Schema for validation, used by `--validate` and on every run
- `skeleton/` — default config + templates copied to `~/.notifier/` on first run
- `templates/` — example Mustache templates (useme, bankier, hackernews)
- `run.sh` — cron entry point, sets up pyenv without loading full `.bashrc`
- `pyrightconfig.json` — pyright config pointing to pyenv's site-packages

## Architecture

1. `defs` — reusable scraping definitions (URL + CSS selectors + pagination)
2. `rules` — reference a def, add schedule (cron expr), email template, recipient
3. `input` — optional array on a rule for scraping multiple pages with different params (e.g. multiple stock symbols)
4. `validator` — optional filter on each input entry, object or array:
   - `{"test": "{{price}} > 10"}` — numexpr expression
   - `{"match": {"value": "{{title}}", "regex": "^Ask HN"}}` — regex match
   - Array of validators = OR logic (any passes)
   - Object with both test+match = AND logic

## How to add a new page to scrape

1. Inspect the target page HTML (use browser DevTools or `python3 -c "import requests; ..."`)
2. Add a definition to `defs` in `~/.notifier/config.json`:
   - `url` — supports `{{param}}` Mustache variables
   - `query.type` — `"list"` (multiple items) or `"single"` (one item per page)
   - `query.selector` — CSS selector for item container
   - `query.variables` — each variable: `selector` + `value` (`type: "text"` or `type: "attribute"` with `name`)
   - Optional: `pagination`, `filter`, `id`, `value.parse: "number"`, `value.regex`, `value.prefix`
   - Optional: `sibling: true` on a variable to search next sibling element
3. Add a rule to `rules`:
   - `ref` — definition name
   - `name` — unique, used as state filename
   - `schedule` — cron expression (e.g. `"0 8 * * *"`)
   - `subject` — Mustache template for email subject
   - `template` — path to body template relative to `~/.notifier/`
   - `email` — recipient
   - `params` or `input` — parameter values (input supports array + validators)
4. Create a Mustache template file. Available vars: `{{count}}`, `{{now}}`, `{{search_url}}`, `{{#items}}...{{/items}}` with `{{index}}` and all extracted variables
5. Validate: `python3 index.py --validate`

## CLI flags

```
--validate    validate config and exit
--dry-run     fetch and display data, no emails, no state changes
--save-email  save emails to file instead of sending
--force       ignore schedules, run all rules
-q, --quiet   suppress all output
```

## Dependencies

Python 3.12+ with: `requests`, `beautifulsoup4`, `pystache`, `croniter`, `numexpr`, `jsonschema`

Installed for both pyenv Python (3.12) and system Python (3.14) at `/usr/bin/python3`.

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
