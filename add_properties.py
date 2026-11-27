#!/usr/bin/env python3
"""Append custom properties to Splunk Observability Cloud host dimensions.

For every hostname in a CSV file this script:
  1. GETs the existing dimension (/v2/dimension/<key>/<host>) and reads its
     current customProperties and tags,
  2. merges the properties from the config file into the existing ones,
  3. PUTs the merged result back (unless --dry-run is set).

Because the dimension PUT replaces the whole set of custom properties, the
existing properties, tags and description are always sent back along with
the new properties so nothing is lost.

The API token is read from an environment variable (default SFX_TOKEN) and
is never stored in the config file.

Examples:
  export SFX_TOKEN=...            # org access token / user API token
  python add_properties.py --csv hosts.csv --dry-run
  python add_properties.py --csv hosts.csv
  python add_properties.py --csv hosts.csv --overwrite
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

# Documented limits for custom properties on a dimension.
MAX_PROPERTIES_PER_DIMENSION = 75
MAX_NAME_LENGTH = 128
MAX_VALUE_LENGTH = 256

# If the first CSV cell is one of these (case-insensitive) it is treated as a header.
HEADER_NAMES = {"host", "hostname", "host_name", "name"}

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ConfigError(Exception):
    """Invalid or missing configuration."""


class FatalApiError(Exception):
    """An API error that makes continuing pointless (e.g. bad token)."""


# --------------------------------------------------------------------------- #
# Config / CSV loading
# --------------------------------------------------------------------------- #
def load_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ConfigError("config file %s is not valid JSON: %s" % (path, exc))

    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a JSON object")

    api_url = raw.get("api_url")
    realm = raw.get("realm")
    if not api_url:
        if not realm:
            raise ConfigError("config needs either 'realm' (e.g. 'eu0') or 'api_url'")
        api_url = "https://api.%s.signalfx.com" % realm

    props_raw = raw.get("properties")
    if not isinstance(props_raw, dict) or not props_raw:
        raise ConfigError("config 'properties' must be a non-empty object of name: value")

    properties: Dict[str, str] = {}
    for name, value in props_raw.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError("property %r must have a string or number value" % name)
        value = str(value)
        if not name or len(name) > MAX_NAME_LENGTH:
            raise ConfigError("property name %r must be 1-%d characters" % (name, MAX_NAME_LENGTH))
        if len(value) > MAX_VALUE_LENGTH:
            raise ConfigError("value of property %r exceeds %d characters" % (name, MAX_VALUE_LENGTH))
        properties[name] = value

    return {
        "api_url": api_url.rstrip("/"),
        "dimension_key": raw.get("dimension_key", "host"),
        "token_env": raw.get("token_env", "SFX_TOKEN"),
        "properties": properties,
    }


def read_hosts(path: str) -> List[str]:
    """Return unique hostnames (first CSV column), skipping header/blank/# lines."""
    hosts: List[str] = []
    seen = set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for index, row in enumerate(csv.reader(fh)):
                if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                    continue
                host = row[0].strip()
                if not hosts and index == 0 and host.lower() in HEADER_NAMES:
                    continue
                if host not in seen:
                    seen.add(host)
                    hosts.append(host)
    except FileNotFoundError:
        raise ConfigError("CSV file not found: %s" % path)
    return hosts


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #
def api_request(session: requests.Session, method: str, url: str, retries: int = 4,
                **kwargs: Any) -> requests.Response:
    """Issue a request, retrying on rate limiting and transient server errors."""
    for attempt in range(retries + 1):
        resp = session.request(method, url, timeout=30, **kwargs)
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            try:
                delay = float(resp.headers.get("Retry-After", ""))
            except ValueError:
                delay = float(2 ** attempt)
            time.sleep(delay)
            continue
        if resp.status_code in (401, 403):
            raise FatalApiError(
                "HTTP %d from %s - check the token and that it has admin/power access"
                % (resp.status_code, url)
            )
        return resp
    return resp  # pragma: no cover


def dimension_url(base: str, key: str, host: str) -> str:
    return "%s/v2/dimension/%s/%s" % (base, quote(key, safe=""), quote(host, safe=""))


Props = Optional[Dict[str, Any]]
Result = Tuple[str, str, Props, Props]


def format_properties(label: str, props: Dict[str, Any], before: Props = None) -> List[str]:
    """Format a property set for display.

    When `before` is given, properties that are new are marked '+' and
    properties whose value differs from `before` are marked '~'.
    """
    lines = ["    %s (%d):" % (label, len(props))]
    if not props:
        lines.append("        (none)")
    for name in sorted(props):
        marker = " "
        if before is not None:
            if name not in before:
                marker = "+"
            elif str(before[name]) != str(props[name]):
                marker = "~"
        lines.append("      %s %s = %s" % (marker, name, props[name]))
    return lines


def process_host(session: requests.Session, cfg: Dict[str, Any], host: str,
                 overwrite: bool, dry_run: bool) -> Result:
    """Handle one host. Returns (status, message, before, after).

    status is one of: updated, would_update, unchanged, not_found, error.
    `before` is the host's custom properties as read from the API. `after` is
    the proposed set in a dry run, or the set re-read from the API after the
    write. Either can be None when the host couldn't be read.
    """
    key = cfg["dimension_key"]
    url = dimension_url(cfg["api_url"], key, host)
    wanted: Dict[str, str] = cfg["properties"]

    resp = api_request(session, "GET", url)
    if resp.status_code == 404:
        return "not_found", "no %s=%s dimension exists in the org" % (key, host), None, None
    if not resp.ok:
        return "error", "GET failed: HTTP %d %s" % (resp.status_code, resp.text[:200]), None, None

    dim = resp.json()
    existing: Dict[str, Any] = dim.get("customProperties") or {}
    merged: Dict[str, Any] = dict(existing)

    added: List[str] = []
    changed: List[str] = []
    skipped: List[str] = []
    for name, value in wanted.items():
        if name not in existing:
            merged[name] = value
            added.append(name)
        elif str(existing[name]) == value:
            continue  # already correct
        elif overwrite:
            changed.append("%s: %r -> %r" % (name, existing[name], value))
            merged[name] = value
        else:
            skipped.append("%s (existing %r, wanted %r)" % (name, existing[name], value))

    notes = []
    if skipped:
        notes.append("kept existing value, use --overwrite to replace: " + "; ".join(skipped))

    if not added and not changed:
        return "unchanged", "; ".join(["nothing to change"] + notes), existing, existing

    if len(merged) > MAX_PROPERTIES_PER_DIMENSION:
        return ("error",
                "would exceed %d custom properties on this dimension" % MAX_PROPERTIES_PER_DIMENSION,
                existing, None)

    summary = []
    if added:
        summary.append("add " + ", ".join("%s=%r" % (n, wanted[n]) for n in added))
    if changed:
        summary.append("change " + "; ".join(changed))
    summary.extend(notes)
    message = " | ".join(summary)

    if dry_run:
        return "would_update", message, existing, merged

    body = {
        "key": key,
        "value": host,
        "description": dim.get("description") or "",
        "customProperties": merged,
        "tags": dim.get("tags") or [],
    }
    put = api_request(session, "PUT", url, json=body)
    if not put.ok:
        return "error", "PUT failed: HTTP %d %s" % (put.status_code, put.text[:200]), existing, None

    # Re-read so the output shows what the API actually stored.
    verify = api_request(session, "GET", url)
    if verify.ok:
        stored = verify.json().get("customProperties") or {}
        missing = [n for n, v in merged.items() if str(stored.get(n)) != str(v)]
        if missing:
            message += " | WARNING: not visible after write yet: " + ", ".join(missing)
        return "updated", message, existing, stored
    return "updated", message + " | could not re-read to verify", existing, merged


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append custom properties to Splunk Observability Cloud host dimensions.",
        epilog="Dry run still reads (GET) each dimension but never writes (PUT).",
    )
    parser.add_argument("--csv", required=True, help="CSV file; hostnames in the first column")
    parser.add_argument("--config", default="config.json", help="JSON config file (default: config.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change without modifying anything")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace existing properties that have a different value "
                             "(default: keep the existing value and warn)")
    parser.add_argument("--quiet", action="store_true",
                        help="don't list each host's properties before and after, "
                             "only the one-line result")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.config)
        hosts = read_hosts(args.csv)
    except ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    token = os.environ.get(cfg["token_env"])
    if not token:
        print("error: environment variable %s is not set" % cfg["token_env"], file=sys.stderr)
        return 2
    if not hosts:
        print("error: no hostnames found in %s" % args.csv, file=sys.stderr)
        return 2

    print("%s%s: %d host(s), dimension key %r, properties %s" % (
        "[DRY RUN] " if args.dry_run else "", cfg["api_url"], len(hosts),
        cfg["dimension_key"], json.dumps(cfg["properties"])))

    session = requests.Session()
    session.headers.update({"X-SF-Token": token, "Content-Type": "application/json"})

    counts: Dict[str, int] = {}
    try:
        for host in hosts:
            try:
                status, message, before, after = process_host(
                    session, cfg, host, args.overwrite, args.dry_run)
            except requests.RequestException as exc:
                status, message, before, after = "error", str(exc), None, None
            counts[status] = counts.get(status, 0) + 1
            print("%-13s %s - %s" % (status.upper(), host, message))
            if not args.quiet:
                if before is not None:
                    print("\n".join(format_properties("BEFORE", before)))
                if after is not None and status != "unchanged":
                    label = "AFTER (proposed)" if args.dry_run else "AFTER (stored)"
                    print("\n".join(format_properties(label, after, before)))
                if before is not None or after is not None:
                    print()
    except FatalApiError as exc:
        print("fatal: %s" % exc, file=sys.stderr)
        return 1

    print("\nSummary: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
    return 1 if counts.get("error") or counts.get("not_found") else 0


if __name__ == "__main__":
    sys.exit(main())
