#!/usr/bin/env python3
"""Show, grant and revoke a virtual key's (or a team's) pass-through routes.

Every pass-through in litellm_config.yaml carries ``auth: true``, so a virtual
key reaches one only when the route is in its ``metadata.allowed_passthrough_routes``
-- or its team's, when the key has no list of its own (the two are never
combined). This script edits that list through the admin API with
DEFAULT_LITELLM_MASTER_KEY from the repo-root .env. It writes ``metadata``
directly rather than the top-level ``allowed_passthrough_routes`` field, so it
works with or without LITELLM_LICENSE. See ai/litellm/LITELLM.md § Pass-through auth.

Usage, from the repo root (KEY is the sk-... key or its Key ID from the UI):

  uv run --no-sync python ai/litellm/scripts/passthrough_routes.py routes
  uv run --no-sync python ai/litellm/scripts/passthrough_routes.py show   --key KEY
  uv run --no-sync python ai/litellm/scripts/passthrough_routes.py add    --key KEY /v1/classifier /v1/detector
  uv run --no-sync python ai/litellm/scripts/passthrough_routes.py remove --key KEY /v1/detector
  uv run --no-sync python ai/litellm/scripts/passthrough_routes.py set    --key KEY /v1/classifier
  uv run --no-sync python ai/litellm/scripts/passthrough_routes.py clear  --key KEY

Every command but ``routes`` takes ``--team TEAM_ID`` in place of ``--key``.

  routes   list the mounted pass-throughs (paths you can grant)
  show     the key's own list, its team's, and which one applies
  add      add routes to the list            remove  take routes off it
  set      replace the list                  clear   delete the list

A route is a mounted path (``/v1/classifier``) or a path under one
(``/v1/classifier/jobs``); it covers that path, everything below it and every
method. Anything else is refused unless ``--force``. The leading slash is
optional: Git Bash on Windows rewrites an argument starting with ``/`` into a
Windows path (``C:/Program Files/Git/v1/classifier``), so there write
``v1/classifier``, or prefix the command with ``MSYS_NO_PATHCONV=1``. ``--dry-run`` prints the
change without writing it; ``--url`` points at another proxy (default
https://api.zeoenergy.com: the edge on the LAN, the tunnel outside).

How each write is made:
  key   GET /key/info, then POST /key/update with the key's whole metadata and
        the new list -- /key/update replaces metadata wholesale, so every other
        field is sent back as read.
  team  PATCH /team/{id} with only the list -- it merges metadata (RFC 7386),
        and a null value deletes the field.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import requests

from common.env import load_env

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_URL = "https://api.zeoenergy.com"
FIELD = "allowed_passthrough_routes"
PLACEHOLDER_KEY = "sk-your-master-key"  # .env.example's value
TIMEOUT_S = 30


class Admin:
    """Master-key calls to the LiteLLM admin API; any 4xx/5xx ends the script."""

    def __init__(self, base_url: str, master_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {master_key}"

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self.session.request(method, self.base_url + path, timeout=TIMEOUT_S, **kwargs)
        except requests.RequestException as exc:
            sys.exit(f"{method} {self.base_url}{path} failed: {exc}")
        if resp.status_code >= 400:
            sys.exit(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def mounted(self) -> dict[str, list[str]]:
        """Mounted pass-through path -> its methods (config file and UI-created)."""
        body = self.call("GET", "/config/pass_through_endpoint")
        return {ep["path"]: ep.get("methods") or [] for ep in body.get("endpoints", [])}

    def key_info(self, key: str) -> dict:
        return self.call("GET", "/key/info", params={"key": key})["info"]

    def team_info(self, team_id: str) -> dict:
        return self.call("GET", "/team/info", params={"team_id": team_id})["team_info"]


def master_key() -> str:
    env_path = REPO_ROOT / ".env"
    load_env(str(env_path))
    key = os.environ.get("DEFAULT_LITELLM_MASTER_KEY", "")
    if not key or key == PLACEHOLDER_KEY:
        sys.exit(f"DEFAULT_LITELLM_MASTER_KEY is not set in {env_path}")
    return key


def routes_of(record: dict) -> list[str]:
    return list((record.get("metadata") or {}).get(FIELD) or [])


def describe(routes: list[str]) -> str:
    return ", ".join(routes) if routes else "(none)"


def normalize_route(arg: str) -> str:
    """``v1/classifier`` -> ``/v1/classifier``; refuses a path Git Bash rewrote."""
    if re.match(r"^[A-Za-z]:[\\/]", arg):
        sys.exit(
            f"{arg!r} is a Windows path: Git Bash rewrote a /v1/... argument.\n"
            "Drop the leading slash (v1/classifier), prefix the command with "
            "MSYS_NO_PATHCONV=1, or run it from PowerShell."
        )
    return arg if arg.startswith("/") else "/" + arg


def unknown_routes(routes: list[str], mounted: dict[str, list[str]]) -> list[str]:
    """Routes that are neither a mounted path nor under one (LiteLLM's own match rule)."""
    return [
        route
        for route in routes
        if not any(route == path or route.startswith(path.rstrip("/") + "/") for path in mounted)
    ]


def updated_routes(command: str, current: list[str], given: list[str]) -> Optional[list[str]]:
    """The list after ``command``; None deletes the field."""
    if command == "set":
        routes = given
    elif command == "add":
        routes = current + given
    elif command == "remove":
        missing = [route for route in given if route not in current]
        if missing:
            print(f"not on the list, nothing to remove: {describe(missing)}")
        routes = [route for route in current if route not in given]
    else:  # clear
        routes = []
    routes = list(dict.fromkeys(routes))
    return routes or None


def cmd_routes(admin: Admin, args: argparse.Namespace) -> None:
    for path, methods in sorted(admin.mounted().items()):
        print(f"{path:<24} {', '.join(methods) or 'all methods'}")


def cmd_show(admin: Admin, args: argparse.Namespace) -> None:
    if args.team:
        print(f"team {args.team}: {describe(routes_of(admin.team_info(args.team)))}")
        return
    info = admin.key_info(args.key)
    own = routes_of(info)
    print(f"key {info.get('key_alias') or args.key}: {describe(own)}")
    team_id = info.get("team_id")
    if not team_id:
        print("no team; the key's own list applies")
        return
    team = routes_of(admin.team_info(team_id))
    print(f"team {team_id}: {describe(team)}")
    print("applies: the key's own list" if own else "applies: the team's list")


def cmd_write(admin: Admin, args: argparse.Namespace) -> None:
    given = [normalize_route(arg) for arg in getattr(args, "routes", [])]
    if given and not args.force:
        unknown = unknown_routes(given, admin.mounted())
        if unknown:
            sys.exit(
                f"not a mounted pass-through: {describe(unknown)}\n"
                "run `routes` to list them, or pass --force to write anyway"
            )

    if args.team:
        target = f"team {args.team}"
        current = routes_of(admin.team_info(args.team))
    else:
        info = admin.key_info(args.key)
        target = f"key {info.get('key_alias') or args.key}"
        current = routes_of(info)

    new = updated_routes(args.command, current, given)
    print(f"{target}: {describe(current)}  ->  {describe(new or [])}")
    if new == (current or None):
        print("no change")
        return
    if args.dry_run:
        print("dry run, nothing written")
        return

    if args.team:
        team = admin.call("PATCH", f"/team/{args.team}", json={"metadata": {FIELD: new}})
        stored = routes_of(team)
    else:
        metadata = dict(info.get("metadata") or {})
        if new is None:
            metadata.pop(FIELD, None)
        else:
            metadata[FIELD] = new
        admin.call("POST", "/key/update", json={"key": args.key, "metadata": metadata})
        stored = routes_of(admin.key_info(args.key))
    print(f"stored: {describe(stored)}")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=DEFAULT_URL, help=f"LiteLLM base URL (default {DEFAULT_URL})")

    target = argparse.ArgumentParser(add_help=False)
    who = target.add_mutually_exclusive_group(required=True)
    who.add_argument("--key", help="virtual key: sk-... or its Key ID (hash)")
    who.add_argument("--team", help="team ID")

    write = argparse.ArgumentParser(add_help=False)
    write.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    write.add_argument("--force", action="store_true", help="allow routes that are not mounted")

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("routes", parents=[common], help="list the mounted pass-throughs").set_defaults(
        func=cmd_routes
    )
    sub.add_parser("show", parents=[common, target], help="show a key's or team's list").set_defaults(
        func=cmd_show
    )
    for name, help_text in (
        ("add", "add routes to the list"),
        ("remove", "take routes off the list"),
        ("set", "replace the list"),
    ):
        p = sub.add_parser(name, parents=[common, target, write], help=help_text)
        p.add_argument("routes", nargs="+", metavar="ROUTE", help="e.g. /v1/classifier")
        p.set_defaults(func=cmd_write)
    sub.add_parser("clear", parents=[common, target, write], help="delete the list").set_defaults(
        func=cmd_write
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    args.func(Admin(args.url, master_key()), args)


if __name__ == "__main__":
    main()
