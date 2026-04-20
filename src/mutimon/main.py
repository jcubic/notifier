#!/usr/bin/env python3

"""
Mutimon — config-driven web scraper and email notifier.

Reads scraping rules from ~/.mutimon/config.json, extracts data from websites
using CSS selectors, detects new items, and sends email notifications using
Liquid templates. Processes all rules defined in config on each run.

Usage:
    mon                       # process rules whose schedule is due
    mon --force               # ignore schedules and run all rules
    mon --force <rule>        # ignore schedule and run a specific rule
    mon --dry-run             # fetch and display data without sending emails
    mon --save-email          # save emails to file instead of sending
    mon --validate            # validate config and exit
    mon --verbose             # show detailed progress output
    mon -q                    # run silently (for cron)

Designed to run as a daily cron job.
"""

# === Standard library imports (always available) ===
import argparse
import hashlib
import json
import os
import re
import shutil
import smtplib
import sys
import traceback
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin

# ========================= PATHS =========================
MUTIMON_DIR = os.path.expanduser("~/.mutimon")
CONFIG_FILE = os.path.join(MUTIMON_DIR, "config.json")
TEMPLATES_DIR = os.path.join(MUTIMON_DIR, "templates")
DATA_DIR = os.path.join(MUTIMON_DIR, "data")
SKELETON_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "skeleton")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

verbose = False


def log(msg):
    """Print a message only when --verbose is enabled."""
    if verbose:
        print(msg)


def info(msg):
    """Print an essential message always (unless --quiet)."""
    print(msg)


# =========================================================


def send_error_email(subject, body):
    """
    Send an error notification using only stdlib.

    Reads SMTP config and recipient emails directly from config.json.
    This function must not depend on any third-party library so it works
    even when the error is a missing dependency.
    """
    try:
        if not os.path.exists(CONFIG_FILE):
            return

        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)

        server_config = config.get("email", {}).get("server", {})
        host = server_config.get("host")
        port = server_config.get("port", 587)
        sender = server_config.get("email")
        password = server_config.get("password")

        if not all([host, sender, password]):
            return

        # Collect unique recipient emails from all rules
        recipients = set()
        for rule in config.get("rules", []):
            email = rule.get("email")
            if email:
                recipients.add(email)
        if not recipients:
            recipients.add(sender)

        for recipient in recipients:
            msg = EmailMessage()
            msg["From"] = f"Mutimon <{sender}>"
            msg["To"] = recipient
            msg["Subject"] = subject
            msg.set_content(body)

            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(sender, password)
                server.send_message(msg)

    except Exception:
        # If sending the error email itself fails, just print — don't recurse
        traceback.print_exc()


# === Third-party imports ===
try:
    import requests
    import jmespath
    from liquid import Environment as LiquidEnvironment
    from liquid import Tag as LiquidTag
    from liquid.ast import Node as LiquidNode
    from liquid.token import TOKEN_TAG, TOKEN_EXPRESSION
    import numexpr
    from babel import Locale
    from babel.numbers import parse_decimal
    from croniter import croniter
    from bs4 import BeautifulSoup
    from jsonschema import Draft202012Validator
except ImportError:
    tb = traceback.format_exc()
    print(tb, file=sys.stderr)
    send_error_email(
        "[mutimon] Missing dependency",
        f"Mutimon failed to start at {datetime.now()}.\n\n{tb}",
    )
    sys.exit(1)


def init_config():
    """Create ~/.mutimon with skeleton files if config is missing."""
    if os.path.exists(CONFIG_FILE):
        return

    print(f"Config not found at {CONFIG_FILE}")
    print(f"Creating skeleton configuration in {MUTIMON_DIR}...")

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(TEMPLATES_DIR, exist_ok=True)

    if os.path.isdir(SKELETON_DIR):
        # Copy skeleton config.json
        src_config = os.path.join(SKELETON_DIR, "config.json")
        if os.path.exists(src_config):
            shutil.copy2(src_config, CONFIG_FILE)

        # Copy skeleton templates
        src_templates = os.path.join(SKELETON_DIR, "templates")
        if os.path.isdir(src_templates):
            for name in os.listdir(src_templates):
                src = os.path.join(src_templates, name)
                dst = os.path.join(TEMPLATES_DIR, name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
    else:
        # Fallback: write a minimal config inline
        minimal = {
            "email": {
                "server": {
                    "host": "smtp.example.com",
                    "port": 587,
                    "password": "your-password-here",
                    "email": "you@example.com",
                }
            },
            "defs": {},
            "rules": [],
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(minimal, f, ensure_ascii=False, indent=2)

    print(f"Done. Edit {CONFIG_FILE} to configure your scraping rules.")
    sys.exit(0)


def is_skeleton_config():
    """Check if the current config is unchanged from the skeleton default."""
    src_config = os.path.join(SKELETON_DIR, "config.json")
    if not os.path.exists(src_config) or not os.path.exists(CONFIG_FILE):
        return False
    with open(src_config, "r", encoding="utf-8") as f:
        skeleton = f.read()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        current = f.read()
    return skeleton == current


def print_setup_guide():
    """Print a quick setup guide when the config hasn't been customized yet."""
    print("It looks like you haven't configured Mutimon yet.\n")
    print("Quick setup:")
    print(f"  1. Open {CONFIG_FILE}")
    print("  2. Set your SMTP server credentials in the \"email\" section")
    print("  3. Add a scraping definition to \"defs\" (URL + CSS selectors)")
    print("  4. Add a rule to \"rules\" (schedule, template, recipient)")
    print("  5. Create a Liquid template in ~/.mutimon/templates/")
    print("  6. Run: mon --validate")
    print("  7. Test: mon --dry-run --force")
    print("  8. Add a cron job: (crontab -l 2>/dev/null; mon --cron) | crontab -")
    print()
    print("TIP: You can use an AI assistant to add new websites:")
    print("     claude -p \"$(cat $(mon --ai-guide)) Add https://example.com to mon\"")
    print()
    print("     Run 'mon --ai-guide' to get the path to the instruction file.")
    sys.exit(0)


def _hash_dict(d):
    """Return a SHA-256 hex digest of a dict's canonical JSON."""
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


def _load_skeleton_email_server():
    """Load the email.server object from the skeleton config."""
    src_config = os.path.join(SKELETON_DIR, "config.json")
    if not os.path.exists(src_config):
        return None
    with open(src_config, "r", encoding="utf-8") as f:
        return json.load(f).get("email", {}).get("server", {})


def load_config():
    """Load the configuration file."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_config(config):
    """
    Validate config against the JSON Schema, then check cron expressions,
    CSS selectors, and JMESPath paths for syntax errors.

    Sends an error email listing all validation errors and exits
    if the config is invalid.
    """
    schema_file = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "config.schema.json"
    )
    if not os.path.exists(schema_file):
        print(
            f"Warning: Schema file not found at {schema_file}, skipping validation.",
            file=sys.stderr,
        )
        return

    with open(schema_file, "r", encoding="utf-8") as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(config))

    if errors:
        _report_validation_errors(errors)

    # Additional syntax checks beyond JSON Schema
    syntax_errors = []
    syntax_errors.extend(_validate_cron_expressions(config))
    syntax_errors.extend(_validate_css_selectors(config))
    syntax_errors.extend(_validate_jmespath_paths(config))

    if syntax_errors:
        _report_validation_errors(syntax_errors)


def _report_validation_errors(errors):
    """Format and report validation errors, then exit."""
    lines = [f"Config validation failed with {len(errors)} error(s):\n"]
    for i, err in enumerate(errors, 1):
        if isinstance(err, str):
            lines.append(f"  {i}. {err}")
        else:
            path = ".".join(str(p) for p in err.absolute_path) or "(root)"
            lines.append(f"  {i}. [{path}] {err.message}")

    msg = "\n".join(lines)
    print(msg, file=sys.stderr)
    send_error_email(
        "[mutimon] Invalid configuration",
        f"Config at {CONFIG_FILE} is invalid.\n\n{msg}",
    )
    sys.exit(1)


def _validate_cron_expressions(config):
    """Check all schedule cron expressions for syntax errors."""
    errors = []
    for rule in config.get("rules", []):
        schedule = rule.get("schedule")
        if not schedule:
            continue
        expressions = schedule if isinstance(schedule, list) else [schedule]
        for expr in expressions:
            if not croniter.is_valid(expr):
                name = rule.get("name", "?")
                errors.append(
                    f"[rules.{name}.schedule] Invalid cron expression: '{expr}'"
                )
    return errors


def _validate_css_selectors(config):
    """Check all CSS selectors for syntax errors."""
    errors = []
    soup = BeautifulSoup("<div></div>", "html.parser")

    def check_selector(selector, path):
        try:
            soup.select(selector)
        except Exception as e:
            errors.append(f"[{path}] Invalid CSS selector '{selector}': {e}")

    for def_name, definition in config.get("defs", {}).items():
        if def_name == "commands":
            continue
        query = definition.get("query", {})
        if query.get("selector"):
            check_selector(query["selector"], f"defs.{def_name}.query.selector")
        for expect_sel in query.get("expect", []):
            check_selector(expect_sel, f"defs.{def_name}.query.expect")
        for reject_sel in query.get("reject", []):
            check_selector(reject_sel, f"defs.{def_name}.query.reject")
        filter_spec = query.get("filter", {})
        if filter_spec.get("selector"):
            check_selector(
                filter_spec["selector"], f"defs.{def_name}.query.filter.selector"
            )
        pagination = definition.get("pagination", {})
        if pagination.get("selector"):
            check_selector(
                pagination["selector"], f"defs.{def_name}.pagination.selector"
            )
        for var_name, var_spec in query.get("variables", {}).items():
            sel = var_spec.get("selector")
            if sel and sel != ":self":
                check_selector(
                    sel, f"defs.{def_name}.query.variables.{var_name}.selector"
                )
    return errors


def _validate_jmespath_paths(config):
    """Check all JMESPath path expressions for syntax errors."""
    errors = []
    for def_name, definition in config.get("defs", {}).items():
        if def_name == "commands":
            continue
        query = definition.get("query", {})
        for var_name, var_spec in query.get("variables", {}).items():
            value = var_spec.get("value", {})
            if value.get("parse") != "json":
                continue
            json_query = value.get("query", {})
            path = json_query.get("path")
            if path:
                # Strip Liquid variables before checking JMESPath syntax
                test_path = re.sub(r"\{\{[^}]*\}\}", "0", path)
                try:
                    jmespath.compile(test_path)
                except jmespath.exceptions.ParseError as e:
                    errors.append(
                        f"[defs.{def_name}.query.variables.{var_name}"
                        f".value.query.path] Invalid JMESPath: {e}"
                    )
            for sub_var, sub_spec in json_query.get("variables", {}).items():
                sub_path = sub_spec.get("path")
                if sub_path:
                    try:
                        jmespath.compile(sub_path)
                    except jmespath.exceptions.ParseError as e:
                        errors.append(
                            f"[defs.{def_name}.query.variables.{var_name}"
                            f".value.query.variables.{sub_var}.path] "
                            f"Invalid JMESPath: {e}"
                        )
    return errors


def load_state(rule_name):
    """Load state for a specific rule from ~/.mutimon/data/<rule_name>."""
    state_file = os.path.join(DATA_DIR, rule_name)
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_state(rule_name, items):
    """Save state for a specific rule to ~/.mutimon/data/<rule_name>."""
    state_file = os.path.join(DATA_DIR, rule_name)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def load_last_run(rule_name):
    """Load the last run timestamp for a rule."""
    run_file = os.path.join(DATA_DIR, f".lastrun_{rule_name}")
    if os.path.exists(run_file):
        try:
            with open(run_file, "r", encoding="utf-8") as f:
                return datetime.fromisoformat(f.read().strip())
        except (ValueError, IOError):
            pass
    return None


def save_last_run(rule_name):
    """Save the current timestamp as last run for a rule."""
    run_file = os.path.join(DATA_DIR, f".lastrun_{rule_name}")
    with open(run_file, "w", encoding="utf-8") as f:
        f.write(datetime.now().isoformat())


def should_run_now(rule):
    """
    Check if a rule should run based on its cron schedule.

    The script is invoked periodically by system cron (e.g. every 5 minutes
    or every hour). We check if the current time (truncated to the start of
    the minute) matches the rule's cron expression. The last-run timestamp
    prevents duplicate runs within the same minute window.

    Returns True if:
      - No schedule is defined (always run)
      - The current minute matches the cron expression AND the rule
        hasn't already run in this minute window
    """
    schedule = rule.get("schedule")
    if not schedule:
        return True

    # Truncate to the start of the current minute
    now = datetime.now().replace(second=0, microsecond=0)

    # Support string or array of cron expressions (any match = run)
    schedules = schedule if isinstance(schedule, list) else [schedule]
    if not any(croniter.match(s, now) for s in schedules):
        return False

    # Prevent duplicate runs within the same minute
    rule_name = rule["name"]
    last_run = load_last_run(rule_name)
    if last_run is not None:
        last_run_minute = last_run.replace(second=0, microsecond=0)
        if last_run_minute >= now:
            return False

    return True


liquid = LiquidEnvironment()


class CommandNode(LiquidNode):
    """Node that renders a command template with bound arguments."""

    __slots__ = ("_env", "template_str", "arg_names", "raw_args")

    def __init__(self, token, env, template_str, arg_names, raw_args):
        super().__init__(token)
        self._env = env
        self.template_str = template_str
        self.arg_names = arg_names
        self.raw_args = raw_args

    def render_to_output(self, context, buffer):
        kwargs = {}
        for name, (kind, raw) in zip(self.arg_names, self.raw_args):
            if kind == "word":
                kwargs[name] = context.resolve(raw, default=raw)
            elif kind == "integer":
                kwargs[name] = int(raw)
            elif kind == "float":
                kwargs[name] = float(raw)
            else:
                kwargs[name] = raw
        tpl = self._env.from_string(self.template_str)
        result = tpl.render(**kwargs)
        buffer.write(result)
        return len(result)


def make_command_tag(cmd_name, template_str, arg_names):
    """Create a Liquid Tag subclass for a named command."""

    class DynamicCommandTag(LiquidTag):
        name = cmd_name
        block = False

        def parse(self, stream):
            token = stream.eat(TOKEN_TAG)
            raw_args = []
            if stream.current.kind == TOKEN_EXPRESSION:
                inner = stream.into_inner(tag=token)
                while inner.current != inner.eof:
                    t = inner.next()
                    raw_args.append((t.kind, t.value))
            return CommandNode(token, self.env, template_str, arg_names, raw_args)

    return DynamicCommandTag


def replace_regex(value, pattern, replacement):
    """Built-in filter: regex replacement on a string value."""
    return re.sub(pattern, replacement, str(value))


def make_filter(expression, env):
    """Create a Liquid filter function from a filter expression string.

    The expression uses standard Liquid filter syntax, e.g.:
        "replace_regex: '\\s+', ' ' | strip"

    Compiled once into a template: {{ __value__ | <expression> }}
    """
    template = env.from_string("{{ __value__ | " + expression + " }}")

    def filter_func(value):
        return template.render(__value__=value)

    return filter_func


def setup_liquid(config):
    """Register custom command tags and filters from config into the Liquid environment."""
    global liquid
    commands = config.get("defs", {}).get("commands", {})
    for cmd_name, cmd_def in commands.items():
        template_str = cmd_def["template"]
        arg_names = cmd_def.get("args", [])
        liquid.add_tag(make_command_tag(cmd_name, template_str, arg_names))
    # Register built-in base filters
    liquid.add_filter("replace_regex", replace_regex)
    # Register user-defined filters from config (Liquid filter expressions)
    filters = config.get("defs", {}).get("filters", {})
    for filter_name, filter_expr in filters.items():
        liquid.add_filter(filter_name, make_filter(filter_expr, liquid))


def render_url(url_template, params):
    """Render a URL template with Liquid params."""
    return liquid.from_string(url_template).render(**params)


def detect_language(html, response_headers=None):
    """
    Detect the page language from HTML lang attribute or Content-Language header.
    Returns a babel Locale or defaults to en.
    """
    # Try <html lang="...">
    soup = BeautifulSoup(html[:2000], "html.parser")
    html_tag = soup.find("html")
    lang = str(html_tag.get("lang", "")) if html_tag else ""

    # Fall back to Content-Language header
    if not lang and response_headers:
        lang = response_headers.get("Content-Language", "")

    # Clean up (e.g. "en-US" -> "en_US", "pl" -> "pl")
    lang = lang.strip().split(",")[0].strip() if lang else "en"

    try:
        return Locale.parse(lang.replace("-", "_"))
    except Exception:
        return Locale.parse("en")


def fetch_page(url, user_agent=None, is_xml=False):
    """Fetch a single page and return its HTML and detected locale."""
    headers = {"User-Agent": user_agent or USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if is_xml:
        locale = Locale.parse("en")
    else:
        locale = detect_language(resp.text, resp.headers)
    return resp.text, locale


def extract_value(element, value_spec, default=None, locale=None):
    """
    Extract a value from a BeautifulSoup element based on the value spec.

    value_spec: {
      "type": "text" | "attribute",
      "name": "href",       # only for type=attribute
      "regex": "pattern",   # optional: extract group(1) from value
      "prefix": "https://..." # optional: prepend to final value
    }
    """
    if element is None:
        return default

    if value_spec["type"] == "text":
        raw = element.get_text(strip=True)
    elif value_spec["type"] == "attribute":
        raw = element.get(value_spec["name"], "")
        if raw is None:
            raw = ""
    else:
        return default

    if not raw:
        return default if default is not None else raw

    # Apply regex extraction if specified
    regex = value_spec.get("regex")
    if regex:
        match = re.search(regex, raw)
        if match:
            raw = match.group(1) if match.lastindex else match.group(0)
        else:
            return default if default is not None else ""

    # Apply prefix if specified
    prefix = value_spec.get("prefix")
    if prefix:
        raw = prefix + raw

    # Apply type parsing if specified
    parse = value_spec.get("parse")
    if parse == "number":
        raw = parse_number(raw)
    elif parse == "money":
        raw = parse_money(raw, locale=locale)
    elif parse == "list":
        delimiter = value_spec.get("delimiter", r"\s*,\s*")
        raw = [s for s in re.split(delimiter, raw) if s]
    elif parse == "json":
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else {}

    return raw


_json_cache = {}


def query_json(json_data, query_spec, item_data):
    """Query parsed JSON data using JMESPath and extract variables.

    query_spec: {
      "type": "list" | "single",
      "path": "data.items[?id == `{{id}}`]",  # JMESPath (Liquid-rendered)
      "variables": {
        "city": { "path": "displayWorkplace" },
        "url":  { "path": "offerAbsoluteUri" }
      }
    }

    Returns a list of dicts (type=list) or a flat dict (type=single).
    If path is omitted, the root JSON object is used.
    """
    query_type = query_spec.get("type", "single")
    empty = [] if query_type == "list" else {}

    # Navigate with JMESPath (Liquid-rendered for variable interpolation)
    jmespath_expr = query_spec.get("path")
    if jmespath_expr:
        rendered_path = liquid.from_string(jmespath_expr).render(**item_data)
        result = jmespath.search(rendered_path, json_data)
    else:
        result = json_data

    if result is None:
        return empty

    variables = query_spec.get("variables", {})

    def extract_from_entry(entry):
        data = {}
        for var_name, var_spec in variables.items():
            var_path = var_spec.get("path")
            if var_path:
                data[var_name] = jmespath.search(var_path, entry)
            else:
                data[var_name] = entry
            if data[var_name] is None:
                data[var_name] = ""
        return data

    if query_type == "list":
        if not isinstance(result, list):
            result = [result]
        return [extract_from_entry(entry) for entry in result]
    else:
        if isinstance(result, list):
            result = result[0] if result else {}
        return extract_from_entry(result)


def parse_number(value):
    """
    Parse a plain numeric string into a float.

    Handles integers and floats with optional thousands separators.
    Does NOT handle currency symbols — use parse="money" for prices.

    Examples: "1234", "1,234", "3.14", "-0.84", "+5.2"
    """
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    # Strip non-numeric chars except digits, dot, comma, minus, plus
    s = re.sub(r"[^\d,.\-+]", "", s)
    # Remove commas (thousands separators in plain numbers)
    s = s.replace(",", "")
    if not s:
        return 0
    try:
        result = float(s)
        if result == int(result):
            return int(result)
        return result
    except (ValueError, TypeError):
        return 0


def parse_money(value, locale=None):
    """
    Parse a monetary string into a float using locale-aware parsing via babel.

    Strips currency symbols, whitespace, and percentage signs before parsing.
    Uses the page's detected locale to correctly interpret decimal and
    thousands separators (e.g. "$70,528.40" in en vs "11,8000 zł" in pl).

    Falls back to a basic parser if babel fails.
    """
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    # Strip leading non-numeric chars (currency symbols like $, €)
    s = re.sub(r"^[^\d\-+]+", "", s)
    # Strip trailing non-numeric chars (currency codes like zł, %, USD)
    s = re.sub(r"[^\d,.]+$", "", s)
    # Replace non-breaking spaces with regular spaces
    s = s.replace("\xa0", " ").strip()

    if not s:
        return 0.0

    if locale is None:
        locale = Locale.parse("en")

    try:
        return float(parse_decimal(s, locale=locale))
    except Exception:
        # Fallback: strip everything except digits, dot, minus
        s = re.sub(r"[^\d.\-]", "", s)
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0


def extract_id(item_data, id_spec, element=None):
    """
    Extract a unique ID from item data.

    id_spec: {
      "source": "url",          # which variable to use as source
      "regex": ",(\\d+)/$"      # optional regex to extract the ID
    }

    Or for reading an HTML attribute directly from the element:

    id_spec: {
      "type": "attribute",
      "name": "id"              # HTML attribute name on the matched element
    }
    """
    if not id_spec:
        # Fallback: use url if available, otherwise hash all values
        return item_data.get("url", str(hash(frozenset(item_data.items()))))

    # Read ID from an HTML attribute on the element itself
    if id_spec.get("type") == "attribute" and element is not None:
        return str(element.get(id_spec.get("name", "id"), ""))

    source_value = item_data.get(id_spec.get("source", ""), "")
    regex = id_spec.get("regex")
    if regex and source_value:
        match = re.search(regex, source_value)
        if match:
            return match.group(1) if match.lastindex else match.group(0)

    return source_value


def should_include(element, filter_spec):
    """
    Check if an element passes the filter.

    filter_spec: {
      "selector": ".job__header-details--date",
      "exclude_class": "job__header-details--closed"
    }
    """
    if not filter_spec:
        return True

    target = element.select_one(filter_spec["selector"])
    if target is None:
        return False

    exclude_class = filter_spec.get("exclude_class")
    if exclude_class:
        classes = target.get("class") or []
        if exclude_class in classes:
            return False

    return True


def extract_variables(element, variables_spec, locale=None):
    """Extract all defined variables from an element.

    Supports an optional "sibling" key in the variable spec. When present,
    the selector is applied to the next sibling element(s) instead of
    inside the matched element. Example:

        "score": {
          "sibling": true,
          "selector": ".score",
          "value": { "type": "text" }
        }

    Supports an optional "collect" key. When true, all elements matching
    the selector are collected, each value is extracted, and they are
    joined with the "separator" string (default: ", "). Example:

        "skills": {
          "selector": ".skill-tag",
          "value": { "type": "text" },
          "collect": true,
          "separator": ", "
        }
    """
    data = {}
    for var_name, var_spec in variables_spec.items():
        # json+query variables need item ID, extracted separately in parse_items()
        value = var_spec.get("value", {})
        if value.get("parse") == "json" and value.get("query"):
            continue
        search_root = element
        if var_spec.get("sibling"):
            # Walk through next siblings until we find a non-spacer element
            sibling = element.find_next_sibling()
            while (
                sibling
                and sibling.get("class")
                and "spacer" in sibling.get("class", [])
            ):
                sibling = sibling.find_next_sibling()
            search_root = sibling

        if search_root is None:
            data[var_name] = var_spec.get("default", "")
            continue

        default = var_spec.get("default")
        selector = var_spec["selector"]

        if var_spec.get("collect"):
            # Collect all matching elements into a list
            sub_elements = search_root.select(selector)
            values = []
            for sub_el in sub_elements:
                val = extract_value(sub_el, var_spec["value"], locale=locale)
                if val is not None and val != "":
                    values.append(str(val))
            data[var_name] = values
        elif selector == ":self":
            # Special selector: use the container element itself
            value = extract_value(
                search_root, var_spec["value"], default, locale=locale
            )
            data[var_name] = value if value is not None else ""
        else:
            sub_element = search_root.select_one(var_spec["selector"])
            value = extract_value(
                sub_element, var_spec["value"], default, locale=locale
            )
            data[var_name] = value if value is not None else ""
    return data


def parse_items(html, query_spec, locale=None, bs_parser="html.parser"):
    """
    Parse items from HTML based on query specification.

    Returns a list of dicts with extracted variables + 'id' field.
    """
    _json_cache.clear()
    soup = BeautifulSoup(html, bs_parser)
    query_type = query_spec["type"]
    selector = query_spec["selector"]
    variables = query_spec.get("variables", {})
    filter_spec = query_spec.get("filter")
    id_spec = query_spec.get("id")

    # Identify json+query variables (need item ID for JMESPath Liquid rendering)
    json_query_vars = {
        name: spec
        for name, spec in variables.items()
        if spec.get("value", {}).get("parse") == "json"
        and spec.get("value", {}).get("query")
    }

    def extract_json_query_vars(data, element):
        """Extract json+query variables using cached parsed JSON."""
        for var_name, var_spec in json_query_vars.items():
            value_spec = var_spec["value"]
            query_spec_json = value_spec["query"]
            sel = var_spec["selector"]

            # Cache parsed JSON per selector to avoid re-parsing per item
            cache_key = (id(soup), sel)
            if cache_key not in _json_cache:
                json_el = soup.select_one(sel)
                raw = extract_value(json_el, value_spec, locale=locale)
                if not isinstance(raw, dict) and not isinstance(raw, list):
                    data[var_name] = [] if query_spec_json.get("type") == "list" else {}
                    continue
                _json_cache[cache_key] = raw
            json_data = _json_cache[cache_key]
            data[var_name] = query_json(json_data, query_spec_json, data)

    if query_type == "single":
        element = soup.select_one(selector)
        if element is None:
            return []
        if not should_include(element, filter_spec):
            return []
        data = extract_variables(element, variables, locale=locale)
        data["id"] = extract_id(data, id_spec, element)
        if json_query_vars:
            extract_json_query_vars(data, element)
        return [data]

    elif query_type == "list":
        elements = soup.select(selector)
        items = []
        for el in elements:
            if not should_include(el, filter_spec):
                continue
            data = extract_variables(el, variables, locale=locale)
            data["id"] = extract_id(data, id_spec, el)
            if json_query_vars:
                extract_json_query_vars(data, el)
            items.append(data)
        return items

    return []


def find_next_page_url(html, pagination_spec, current_url):
    """
    Find the next page URL based on pagination config.

    Supports two pagination types:

    "next_link" - follow a specific "next" link on the page:
        { "type": "next_link", "selector": "a.morelink", "base_url": "..." }

    "numbered" - find the next numbered page after the active one:
        { "type": "numbered", "selector": ".pagination .pagination__page",
          "active_class": "pagination__page--active", "base_url": "..." }
    """
    if not pagination_spec:
        return None

    soup = BeautifulSoup(html, "html.parser")
    base_url = pagination_spec.get("base_url", current_url)
    pag_type = pagination_spec.get("type", "next_link")

    if pag_type == "next_link":
        link = soup.select_one(pagination_spec["selector"])
        if link:
            href = str(link.get("href", ""))
            if href:
                return urljoin(base_url, href)
        return None

    elif pag_type == "numbered":
        all_pages = soup.select(pagination_spec["selector"])
        active_class = pagination_spec.get("active_class", "")
        found_active = False
        for page_link in all_pages:
            classes = page_link.get("class") or []
            if active_class and active_class in classes:
                found_active = True
                continue
            if found_active:
                href = str(page_link.get("href", ""))
                if href:
                    return urljoin(base_url, href)
                break
        return None

    return None


def check_expect(html, expect_selectors, url, bs_parser="html.parser"):
    """
    Verify that expected CSS selectors exist on the page.

    Returns a list of missing selector strings. An empty list means
    all expectations are met. Used to detect HTML structure changes
    that would silently break scraping.
    """
    if not expect_selectors:
        return []

    soup = BeautifulSoup(html, bs_parser)
    missing = []
    for selector in expect_selectors:
        if not soup.select_one(selector):
            missing.append(selector)

    return missing


def fetch_all_items(definition, params):
    """
    Fetch all items across all pages for a definition.
    Returns list of item dicts.

    Raises ValueError if 'expect' selectors are missing from the page.
    """
    pagination_spec = definition.get("pagination")
    query_spec = definition["query"]
    max_pages = pagination_spec.get("max_pages", 1) if pagination_spec else 1
    expect_selectors = query_spec.get("expect")
    bs_parser = "xml" if definition.get("format") == "xml" else "html.parser"
    user_agent = definition.get("userAgent")
    all_items = []
    page_num = 1
    url = render_url(definition["url"], params)

    while page_num <= max_pages:
        log(f"  Fetching page {page_num}: {url}")
        html, locale = fetch_page(url, user_agent=user_agent, is_xml=bs_parser == "xml")

        # Check expected structure on first page only
        if page_num == 1 and expect_selectors:
            missing = check_expect(html, expect_selectors, url, bs_parser=bs_parser)
            if missing:
                raise ValueError(
                    f"HTML structure changed at {url}. "
                    f"Missing expected selector(s): {', '.join(missing)}"
                )

        # Check reject selectors — if any match, skip this page (no real results)
        reject_selectors = query_spec.get("reject")
        if reject_selectors:
            soup = BeautifulSoup(html, bs_parser)
            for sel in reject_selectors:
                if soup.select_one(sel):
                    log(f"  Reject selector matched: {sel} — skipping results")
                    return all_items

        items = parse_items(html, query_spec, locale=locale, bs_parser=bs_parser)

        if not items:
            break

        all_items.extend(items)

        next_url = find_next_page_url(html, pagination_spec, url)
        if next_url:
            url = next_url
            page_num += 1
        else:
            break

    return all_items


def load_template(template_path):
    """Load a Liquid template file."""
    # Template paths in config are relative to the config file directory
    if not os.path.isabs(template_path):
        template_path = os.path.join(MUTIMON_DIR, template_path)

    if not os.path.exists(template_path):
        print(f"Warning: Template not found at {template_path}", file=sys.stderr)
        return None

    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def render_email(template_str, subject_template, items, params, definition):
    """
    Render the email body and subject using Liquid templates.

    Context includes:
      - all params (e.g. query)
      - items: list of extracted data dicts (with index added)
      - count: number of items
      - now: current datetime string
      - search_url: the rendered URL
    """
    search_url = render_url(definition["url"], params)

    # Add 1-based index to items
    indexed_items = []
    for i, item in enumerate(items, 1):
        item_copy = dict(item)
        item_copy["index"] = i
        indexed_items.append(item_copy)

    context = dict(params)
    context["items"] = indexed_items
    context["count"] = len(items)
    context["now"] = str(datetime.now())
    context["search_url"] = search_url

    body = liquid.from_string(template_str).render(**context)
    subject = (
        liquid.from_string(subject_template).render(**context)
        if subject_template
        else ""
    )

    return subject, body


def send_email(config, recipient, subject, body):
    """Send an email notification."""
    server_config = config["email"]["server"]
    sender = server_config["email"]

    msg = EmailMessage()
    msg["From"] = f"Mutimon <{sender}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(server_config["host"], server_config["port"]) as server:
        server.starttls()
        server.login(sender, server_config["password"])
        server.send_message(msg)
    info(f"  Email sent to {recipient}")


def save_email_to_file(rule_name, subject, body):
    """Save email to a file for debugging/verification."""
    email_dir = os.path.join(DATA_DIR, "emails")
    os.makedirs(email_dir, exist_ok=True)
    email_file = os.path.join(email_dir, f"{rule_name}.txt")
    with open(email_file, "w", encoding="utf-8") as f:
        f.write(f"Subject: {subject}\n")
        f.write(f"Date: {datetime.now()}\n")
        f.write("=" * 60 + "\n\n")
        f.write(body)
    log(f"  Email saved to {email_file}")


def evaluate_single_validator(validator, item):
    """
    Evaluate a single validator object against an item's variables.

    The validator is a dict with optional keys (AND logic — all must pass):

      "test": numexpr expression with Liquid variables, e.g.:
          "{{ price }} > 9.5"
          "({{ price }} > 80) & ({{ change_pct }} < 0)"

      "match": regex match against a rendered variable, e.g.:
          {"value": "{{ title }}", "regex": "^Ask HN"}

    Returns True if all specified conditions pass, False otherwise.
    """
    # Evaluate "test" condition (numexpr expression)
    test_expr = validator.get("test")
    if test_expr:
        try:
            rendered = liquid.from_string(test_expr).render(**item)
            if not bool(numexpr.evaluate(rendered)):
                return False
        except Exception as e:
            print(
                f"Warning: Validator test failed for '{test_expr}': {e}",
                file=sys.stderr,
            )
            return False

    # Evaluate "match" condition(s) (regex/include/exclude match — AND logic)
    match_spec = validator.get("match")
    if match_spec:
        # Allow single object or array of match objects
        match_list = match_spec if isinstance(match_spec, list) else [match_spec]
        for m in match_list:
            try:
                if "var" in m:
                    value = item.get(m["var"], "")
                else:
                    value = liquid.from_string(m["value"]).render(**item)
                should_exist = m.get("exist", True)
                strict = m.get("strict", False)
                if "exclude" in m:
                    if isinstance(value, list):
                        matched = not any(s in value for s in m["exclude"])
                    elif strict:
                        matched = not any(s == value for s in m["exclude"])
                    else:
                        matched = not any(s in value for s in m["exclude"])
                elif "include" in m:
                    if isinstance(value, list):
                        matched = any(s in value for s in m["include"])
                    elif strict:
                        matched = any(s == value for s in m["include"])
                    else:
                        matched = any(s in value for s in m["include"])
                else:
                    if isinstance(value, list):
                        value = ", ".join(value)
                    matched = bool(re.search(m["regex"], value))
                if not should_exist:
                    matched = not matched
                if not matched:
                    return False
            except Exception as e:
                print(
                    f"Warning: Validator match failed for '{m}': {e}", file=sys.stderr
                )
                return False

    return True


def evaluate_validator(validator, item):
    """
    Evaluate a validator against an item's variables.

    Accepts:
      - None/empty: always passes
      - dict: a single validator (AND logic within)
      - list: multiple validators with two-tier logic:
          - Validators with "require": true must ALL pass (AND)
          - Remaining validators use OR logic (at least one must pass)
          - If only required validators exist, OR check is skipped
    """
    if not validator:
        return True

    if isinstance(validator, list):
        required = [v for v in validator if v.get("require")]
        optional = [v for v in validator if not v.get("require")]

        # All required validators must pass
        for v in required:
            if not evaluate_single_validator(v, item):
                return False

        # At least one optional validator must pass (if any exist)
        if optional:
            return any(evaluate_single_validator(v, item) for v in optional)

        return True

    return evaluate_single_validator(validator, item)


def resolve_validator(validator, validators_defs):
    """
    Resolve @id references in a validator against defs.validators.

    A validator (or array element) with {"@id": "name"} is replaced
    by the corresponding entry from validators_defs.
    """
    if not validator:
        return validator
    if isinstance(validator, dict) and "@id" in validator:
        return validators_defs.get(validator["@id"])
    if isinstance(validator, list):
        resolved = []
        for v in validator:
            if isinstance(v, dict) and "@id" in v:
                ref = validators_defs.get(v["@id"])
                if ref is not None:
                    if isinstance(ref, list):
                        resolved.extend(ref)
                    else:
                        resolved.append(ref)
            else:
                resolved.append(v)
        return resolved
    return validator


def resolve_inputs(rule, validators_defs=None):
    """
    Resolve the input entries for a rule.

    Returns a list of {params, validator} dicts.

    Supports:
      - No 'input' field: uses rule's 'params' directly, no validator
      - 'input' as a single object: wraps in a list
      - 'input' as an array: used as-is

    Each input entry can have:
      - 'params': dict of URL template variables
      - 'validator': numexpr expression string with Liquid variables
    """
    if validators_defs is None:
        validators_defs = {}
    input_spec = rule.get("input")
    if input_spec is None:
        return [{"params": rule.get("params", {}), "validator": None}]
    if isinstance(input_spec, dict):
        input_spec = [input_spec]
    return [
        {
            "params": entry.get("params", rule.get("params", {})),
            "validator": resolve_validator(
                entry.get("validator"), validators_defs
            ),
        }
        for entry in input_spec
    ]


def process_rule(config, rule, save_only=False):
    """Process a single rule: fetch, diff, notify."""
    rule_name = rule["name"]
    ref = rule["ref"]
    recipient = rule.get("email", config["email"]["server"]["email"])
    template_path = rule.get("template", "")
    subject_template = rule.get("subject", "")

    log(f"Processing rule: '{rule_name}' (ref: {ref})")

    # Look up definition
    definition = config["defs"].get(ref)
    if not definition:
        print(f"Error: Definition '{ref}' not found in config.defs", file=sys.stderr)
        return

    # Resolve inputs (multiple pages with different params + validators)
    validators_defs = config.get("defs", {}).get("validators", {})
    inputs = resolve_inputs(rule, validators_defs)
    all_items = []

    for input_entry in inputs:
        params = input_entry["params"]
        validator = input_entry["validator"]

        try:
            items = fetch_all_items(definition, params)
        except ValueError as e:
            # Structure change detected — send error email
            msg = str(e)
            print(f"Warning: {msg}", file=sys.stderr)
            send_error_email(
                f"[mutimon] HTML structure changed for '{rule_name}'",
                f"Rule '{rule_name}' (ref: {ref}) detected a page structure change.\n\n{msg}",
            )
            continue
        except Exception as e:
            print(f"Error: fetching data for params {params}: {e}", file=sys.stderr)
            continue

        # Merge params into each item and re-derive ID
        id_spec = definition["query"].get("id")
        for item in items:
            for k, v in params.items():
                if k not in item:
                    item[k] = v
            if id_spec and not item.get("id"):
                item["id"] = extract_id(item, id_spec)

        # Mark each item with validator result
        for item in items:
            item["_valid"] = evaluate_validator(validator, item) if validator else True

        valid_count = sum(1 for i in items if i["_valid"])
        if validator and valid_count != len(items):
            log(f"  Validator: {valid_count}/{len(items)} item(s) passed")

        all_items.extend(items)

    # Deduplicate items by ID (multiple inputs may return overlapping results)
    if len(inputs) > 1:
        seen_ids = set()
        unique_items = []
        for item in all_items:
            item_id = item.get("id")
            if item_id is not None and item_id in seen_ids:
                continue
            if item_id is not None:
                seen_ids.add(item_id)
            unique_items.append(item)
        if len(unique_items) < len(all_items):
            log(f"  Deduplicated: {len(all_items)} → {len(unique_items)} item(s)")
        all_items = unique_items

    log(f"  Found {len(all_items)} item(s) total.")

    if not all_items:
        log("  No items found. Nothing to do.")
        save_last_run(rule_name)
        return

    # Load previous state and build lookup by ID
    known_items = load_state(rule_name)
    known_by_id = {}
    for item in known_items:
        if "id" in item:
            known_by_id[item["id"]] = item

    # Find items to notify about:
    # - New items that pass the validator
    # - Known items that previously failed the validator but now pass
    #   (threshold crossing: e.g. price dropped below and rose back above)
    notify_items = []
    for item in all_items:
        item_id = item.get("id")
        if not item["_valid"]:
            continue
        prev = known_by_id.get(item_id)
        if prev is None:
            # New item that passes validator
            notify_items.append(item)
        elif not prev.get("_valid", True):
            # Was invalid before, now valid — threshold crossed
            notify_items.append(item)

    if notify_items:
        info(f"[{rule_name}] {len(notify_items)} new item(s) — sending notification")

        # Use first input's params as the base template context
        base_params = inputs[0]["params"]

        # Load and render template
        template_str = load_template(template_path)
        if template_str:
            subject, body = render_email(
                template_str, subject_template, notify_items, base_params, definition
            )
            if save_only:
                save_email_to_file(rule_name, subject, body)
            else:
                try:
                    send_email(config, recipient, subject, body)
                except Exception as e:
                    print(
                        f"Error: Failed to send email: {e}", file=sys.stderr
                    )
                    print(
                        "State not saved — will retry on next run.",
                        file=sys.stderr,
                    )
                    return
                save_email_to_file(rule_name, subject, body)
        else:
            print("Warning: No template found, skipping email.", file=sys.stderr)
    else:
        log(f"[{rule_name}] No changes to notify about.")

    # Save ALL items (including those that failed validator) with _valid status
    save_state(rule_name, all_items)
    save_last_run(rule_name)
    log(f"  State saved for '{rule_name}'")


def run():
    guide_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "AI_GUIDE.md"
    )
    parser = argparse.ArgumentParser(
        description="Mutimon — config-driven web scraper and email notifier.",
        epilog=f"To add websites using AI:\n"
        f"  claude -p \"$(cat {guide_path}) Add https://example.com to mon\"",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse data but don't send emails or update state",
    )
    parser.add_argument(
        "--save-email",
        action="store_true",
        help="Save email to file instead of sending",
    )
    parser.add_argument(
        "--force",
        nargs="?",
        const=True,
        default=False,
        metavar="RULE",
        help="Ignore schedules. Without argument: run all rules. "
        "With a rule name: run only that rule.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate the config file against the schema and exit",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed progress output",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress all output",
    )
    parser.add_argument(
        "--ai-guide",
        action="store_true",
        help="Print the path to the AI instruction file for adding websites",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all rule names (usable with --force <rule>)",
    )
    parser.add_argument(
        "--cron",
        nargs="?",
        const="*/5 * * * *",
        default=None,
        metavar="SCHEDULE",
        help="Print a cron entry with the resolved path to mon. "
        "Optional schedule argument (default: '*/5 * * * *').",
    )
    args = parser.parse_args()

    if args.ai_guide:
        print(guide_path)
        return

    if args.cron is not None:
        mon_path = shutil.which("mon")
        if mon_path:
            mon_path = os.path.realpath(mon_path)
        else:
            mon_path = os.path.realpath(sys.argv[0])
        log_path = os.path.join(MUTIMON_DIR, "mutimon.log")
        print(f"{args.cron} {mon_path} -q >> {log_path} 2>&1")
        return

    global verbose
    verbose = args.verbose

    if args.quiet:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")

    init_config()
    if is_skeleton_config():
        print_setup_guide()
    config = load_config()

    if args.list:
        rules = config.get("rules", [])
        if not rules:
            print("No rules defined.")
        else:
            for rule in rules:
                print(rule["name"])
        return

    # Check if SMTP credentials are still the skeleton defaults
    skeleton_server = _load_skeleton_email_server()
    user_server = config.get("email", {}).get("server", {})
    if skeleton_server and _hash_dict(user_server) == _hash_dict(skeleton_server):
        print(
            "Error: SMTP credentials in config are still the default placeholder values.",
            file=sys.stderr,
        )
        print(
            f"Edit the \"email.server\" section in {CONFIG_FILE} with your real SMTP settings.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.validate:
        validate_config(config)
        setup_liquid(config)
        print(f"Config at {CONFIG_FILE} is valid.")
        return

    validate_config(config)
    setup_liquid(config)

    rules = config.get("rules", [])
    if not rules:
        log("No rules to process.")
        return

    force_all = args.force is True
    force_rule = args.force if isinstance(args.force, str) else None

    if force_rule:
        rule_names = [r["name"] for r in rules]
        if force_rule not in rule_names:
            print(
                f"Error: Rule '{force_rule}' not found. "
                f"Available rules: {', '.join(rule_names)}",
                file=sys.stderr,
            )
            sys.exit(1)

    log(f"Processing {len(rules)} rule(s)...")

    for rule in rules:
        rule_name = rule["name"]

        if force_rule and rule_name != force_rule:
            continue

        if not force_all and not force_rule and not args.dry_run and not should_run_now(rule):
            schedule = rule.get("schedule", "")
            log(f"Skipping '{rule_name}' (schedule: {schedule})")
            continue

        if args.dry_run:
            ref = rule["ref"]
            definition = config["defs"].get(ref)
            if not definition:
                print(f"Error: Definition '{ref}' not found", file=sys.stderr)
                continue
            info(f"[DRY RUN] Rule: '{rule_name}'")
            inputs = resolve_inputs(rule)
            all_items = []
            for input_entry in inputs:
                params = input_entry["params"]
                validator = input_entry["validator"]
                try:
                    items = fetch_all_items(definition, params)
                    if validator:
                        items = [i for i in items if evaluate_validator(validator, i)]
                    id_spec = definition["query"].get("id")
                    for item in items:
                        for k, v in params.items():
                            if k not in item:
                                item[k] = v
                        if id_spec and not item.get("id"):
                            item["id"] = extract_id(item, id_spec)
                    all_items.extend(items)
                except Exception as e:
                    print(f"  Error for params {params}: {e}", file=sys.stderr)
            info(f"  Found {len(all_items)} item(s)")
            for item in all_items[:3]:
                log(f"    id={item.get('id', '?')}: {item}")
            if len(all_items) > 3:
                log(f"    ... and {len(all_items) - 3} more")
        else:
            process_rule(config, rule, save_only=args.save_email)

    log("Done.")


def main():
    """Entry point for the mon command."""
    try:
        run()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        send_error_email(
            "[mutimon] Fatal error",
            f"Mutimon crashed at {datetime.now()}.\n\n{tb}",
        )


if __name__ == "__main__":
    main()
