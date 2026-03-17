#!/usr/bin/env python3

"""
Generic web scraper and notifier.

Reads scraping rules from ~/.notifier/config.json, extracts data from websites
using CSS selectors, detects new items, and sends email notifications using
Mustache templates. Processes all rules defined in config on each run.

Usage:
    index.py                  # process all rules
    index.py --dry-run        # fetch and display data without sending emails
    index.py --save-email     # save emails to file instead of sending

Designed to run as a daily cron job.
"""

import argparse
import json
import os
import re
import shutil
import sys
import smtplib
import requests
import pystache
from email.message import EmailMessage
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# ========================= PATHS =========================
NOTIFIER_DIR = os.path.expanduser("~/.notifier")
CONFIG_FILE = os.path.join(NOTIFIER_DIR, "config.json")
TEMPLATES_DIR = os.path.join(NOTIFIER_DIR, "templates")
DATA_DIR = os.path.join(NOTIFIER_DIR, "data")
SKELETON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skeleton")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# =========================================================


def init_config():
    """Create ~/.notifier with skeleton files if config is missing."""
    if os.path.exists(CONFIG_FILE):
        return

    print(f"Config not found at {CONFIG_FILE}")
    print(f"Creating skeleton configuration in {NOTIFIER_DIR}...")

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


def load_config():
    """Load the configuration file."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state(rule_name):
    """Load state for a specific rule from ~/.notifier/data/<rule_name>."""
    state_file = os.path.join(DATA_DIR, rule_name)
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_state(rule_name, items):
    """Save state for a specific rule to ~/.notifier/data/<rule_name>."""
    state_file = os.path.join(DATA_DIR, rule_name)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def render_url(url_template, params):
    """Render a URL template with Mustache-style params."""
    return pystache.render(url_template, params)


def fetch_page(url):
    """Fetch a single page and return its HTML."""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_value(element, value_spec, default=None):
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

    return raw


def extract_id(item_data, id_spec):
    """
    Extract a unique ID from item data.

    id_spec: {
      "source": "url",          # which variable to use
      "regex": ",(\\d+)/$"      # regex to extract the ID
    }
    """
    if not id_spec:
        # Fallback: use url if available, otherwise hash all values
        return item_data.get("url", str(hash(frozenset(item_data.items()))))

    source_value = item_data.get(id_spec["source"], "")
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


def extract_variables(element, variables_spec):
    """Extract all defined variables from an element."""
    data = {}
    for var_name, var_spec in variables_spec.items():
        sub_element = element.select_one(var_spec["selector"])
        default = var_spec.get("default")
        value = extract_value(sub_element, var_spec["value"], default)
        data[var_name] = value if value is not None else ""
    return data


def parse_items(html, query_spec):
    """
    Parse items from HTML based on query specification.

    Returns a list of dicts with extracted variables + 'id' field.
    """
    soup = BeautifulSoup(html, "html.parser")
    query_type = query_spec["type"]
    selector = query_spec["selector"]
    variables = query_spec.get("variables", {})
    filter_spec = query_spec.get("filter")
    id_spec = query_spec.get("id")

    if query_type == "single":
        element = soup.select_one(selector)
        if element is None:
            return []
        if not should_include(element, filter_spec):
            return []
        data = extract_variables(element, variables)
        data["id"] = extract_id(data, id_spec)
        return [data]

    elif query_type == "list":
        elements = soup.select(selector)
        items = []
        for el in elements:
            if not should_include(el, filter_spec):
                continue
            data = extract_variables(el, variables)
            data["id"] = extract_id(data, id_spec)
            items.append(data)
        return items

    return []


def has_more_pages(html, page_num):
    """Check if there are more pages by looking for a page link with a higher number."""
    soup = BeautifulSoup(html, "html.parser")
    # Look for any pagination page link with a number higher than current
    page_links = soup.select(".pagination .pagination__page")
    for link in page_links:
        text = link.get_text(strip=True)
        try:
            if int(text) > page_num:
                return True
        except ValueError:
            continue
    return False


def build_page_url(base_url, url_template, params, page_num):
    """Build URL for a specific page number."""
    if page_num == 1:
        return render_url(url_template, params)
    # Add page parameter to the URL
    first_page_url = render_url(url_template, params)
    separator = "&" if "?" in first_page_url else "?"
    return f"{first_page_url}{separator}page={page_num}"


def fetch_all_items(definition, params):
    """
    Fetch all items across all pages for a definition.
    Returns list of item dicts.
    """
    pagination_spec = definition.get("pagination")
    query_spec = definition["query"]
    max_pages = pagination_spec.get("max_pages", 1) if pagination_spec else 1
    all_items = []
    page_num = 1

    while page_num <= max_pages:
        url = build_page_url(
            pagination_spec.get("base_url", "") if pagination_spec else "",
            definition["url"],
            params,
            page_num,
        )
        print(f"  [{datetime.now()}] Fetching page {page_num}: {url}")
        html = fetch_page(url)
        items = parse_items(html, query_spec)

        if not items:
            break

        all_items.extend(items)

        if pagination_spec and has_more_pages(html, page_num):
            page_num += 1
        else:
            break

    return all_items


def load_template(template_path):
    """Load a Mustache template file."""
    # Template paths in config are relative to the config file directory
    if not os.path.isabs(template_path):
        template_path = os.path.join(NOTIFIER_DIR, template_path)

    if not os.path.exists(template_path):
        print(f"  Warning: Template not found at {template_path}")
        return None

    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def render_email(template_str, subject_template, items, params, definition):
    """
    Render the email body and subject using Mustache templates.

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

    renderer = pystache.Renderer(escape=lambda u: u)
    body = renderer.render(template_str, context)
    subject = renderer.render(subject_template, context) if subject_template else ""

    return subject, body


def send_email(config, recipient, subject, body):
    """Send an email notification."""
    server_config = config["email"]["server"]
    sender = server_config["email"]

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(server_config["host"], server_config["port"]) as server:
            server.starttls()
            server.login(sender, server_config["password"])
            server.send_message(msg)
        print(f"  [{datetime.now()}] Email sent to {recipient}")
    except Exception as e:
        print(f"  [{datetime.now()}] Failed to send email: {e}")


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
    print(f"  [{datetime.now()}] Email saved to {email_file}")


def process_rule(config, rule, save_only=False):
    """Process a single rule: fetch, diff, notify."""
    rule_name = rule["name"]
    ref = rule["ref"]
    params = rule.get("params", {})
    recipient = rule.get("email", config["email"]["server"]["email"])
    template_path = rule.get("template", "")
    subject_template = rule.get("subject", "")

    print(f"\n[{datetime.now()}] Processing rule: '{rule_name}' (ref: {ref})")

    # Look up definition
    definition = config["defs"].get(ref)
    if not definition:
        print(f"  Error: Definition '{ref}' not found in config.defs")
        return

    # Fetch current items
    try:
        all_items = fetch_all_items(definition, params)
    except Exception as e:
        print(f"  [{datetime.now()}] Error fetching data: {e}")
        return

    print(f"  [{datetime.now()}] Found {len(all_items)} item(s).")

    if not all_items:
        print(f"  [{datetime.now()}] No items found. Nothing to do.")
        return

    # Load previous state
    known_items = load_state(rule_name)
    known_ids = {item["id"] for item in known_items if "id" in item}

    # Find new items
    new_items = [item for item in all_items if item.get("id") not in known_ids]

    if new_items:
        print(f"  [{datetime.now()}] {len(new_items)} NEW item(s) detected!")

        # Load and render template
        template_str = load_template(template_path)
        if template_str:
            subject, body = render_email(
                template_str, subject_template, new_items, params, definition
            )
            if save_only:
                save_email_to_file(rule_name, subject, body)
            else:
                send_email(config, recipient, subject, body)
                save_email_to_file(rule_name, subject, body)
        else:
            print(f"  [{datetime.now()}] No template found, skipping email.")
    else:
        print(f"  [{datetime.now()}] No new items (all already known).")

    # Save current state
    save_state(rule_name, all_items)
    print(f"  [{datetime.now()}] State saved for '{rule_name}'")


def main():
    parser = argparse.ArgumentParser(
        description="Generic web scraper and email notifier."
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
    args = parser.parse_args()

    init_config()
    config = load_config()

    rules = config.get("rules", [])
    if not rules:
        print("No rules to process.")
        return

    print(f"[{datetime.now()}] Processing {len(rules)} rule(s)...")

    for rule in rules:
        if args.dry_run:
            rule_name = rule["name"]
            ref = rule["ref"]
            params = rule.get("params", {})
            definition = config["defs"].get(ref)
            if not definition:
                print(f"  Error: Definition '{ref}' not found")
                continue
            print(f"\n[DRY RUN] Rule: '{rule_name}'")
            try:
                items = fetch_all_items(definition, params)
                print(f"  Found {len(items)} item(s)")
                for item in items[:3]:
                    print(f"    id={item.get('id', '?')}: {item}")
                if len(items) > 3:
                    print(f"    ... and {len(items) - 3} more")
            except Exception as e:
                print(f"  Error: {e}")
        else:
            process_rule(config, rule, save_only=args.save_email)

    print(f"\n[{datetime.now()}] Done.")


if __name__ == "__main__":
    main()
