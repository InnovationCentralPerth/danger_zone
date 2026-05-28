#!/usr/bin/env python3
"""GitHub device-flow login helper.

Runs the OAuth 2.0 device authorisation flow against github.com, prints
the verification URL + user code, polls for the access token, and writes
``GITHUB_TOKEN=<token>`` into the project ``.env`` file.

Usage
-----
    uv run python scripts/gh_device_login.py
    # or
    python scripts/gh_device_login.py --client-id <id> --scope read:user

Defaults
--------
* ``client_id`` = GitHub CLI's well-known public client
  (``178c6fc778ccc68e1d6a``). Override with ``--client-id`` or
  ``GITHUB_OAUTH_CLIENT_ID`` env var if you have your own OAuth app.
* ``scope`` = ``read:user`` (minimum needed for an identity token).
  Models API access is gated by user enrolment, not OAuth scope.
* ``--env-file`` defaults to ``./.env`` in the repo root.

Why this script
---------------
Avoids the friction of creating a PAT in the GitHub UI. The user just
visits a URL, types a six-character code, and the script handles the
rest. Same pattern the ``gh`` CLI uses.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time
import webbrowser
from pathlib import Path

import httpx

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_CLIENT_ID = "178c6fc778ccc68e1d6a"  # GitHub CLI public client
DEFAULT_SCOPE = "read:user"
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Friendlier output colours when stdout is a tty.
_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
CYAN = "\033[36m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def request_device_code(client_id: str, scope: str) -> dict:
    r = httpx.post(
        DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": scope},
        headers={"Accept": "application/json"},
        timeout=10.0,
    )
    r.raise_for_status()
    return r.json()


def poll_for_token(client_id: str, device_code: str, interval: int, expires_in: int) -> str:
    """Block until the user authorises or the device code expires."""
    deadline = time.monotonic() + expires_in
    sleep_for = max(interval, 1)
    while time.monotonic() < deadline:
        time.sleep(sleep_for)
        r = httpx.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        r.raise_for_status()
        body = r.json()
        if "access_token" in body:
            return body["access_token"]
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            # GitHub asks us to back off; honour the new interval if given.
            sleep_for = int(body.get("interval", sleep_for)) + 1
            continue
        if err in {"expired_token", "access_denied"}:
            raise RuntimeError(f"Device flow failed: {err}: {body.get('error_description', '')}")
        # Unknown error — keep trying until expiry, but surface it.
        print(f"{YELLOW}poll returned: {body}{RESET}", file=sys.stderr)
    raise RuntimeError("Device code expired before authorisation completed")


def upsert_env_token(env_file: Path, token: str, key: str = "GITHUB_TOKEN") -> None:
    """Replace or append ``KEY=VALUE`` in ``env_file``. Creates the file if missing."""
    line = f"{key}={token}\n"
    if not env_file.exists():
        env_file.write_text(line)
        return
    contents = env_file.read_text()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(contents):
        contents = pattern.sub(line.rstrip(), contents)
        if not contents.endswith("\n"):
            contents += "\n"
    else:
        if contents and not contents.endswith("\n"):
            contents += "\n"
        contents += line
    env_file.write_text(contents)


def verify_token(token: str) -> str | None:
    """Confirm the token is good by calling /user. Returns the login, or None."""
    try:
        r = httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        if r.status_code == 200:
            return r.json().get("login")
    except httpx.HTTPError:
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub device-flow login for danger_zone")
    ap.add_argument(
        "--client-id",
        default=os.environ.get("GITHUB_OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID),
        help=(
            f"OAuth client ID (default: {DEFAULT_CLIENT_ID}, the public gh CLI client). "
            "Override with GITHUB_OAUTH_CLIENT_ID env var or this flag."
        ),
    )
    ap.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help=f"OAuth scopes, space-separated (default: {DEFAULT_SCOPE!r})",
    )
    ap.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Path to .env file to update (default: {DEFAULT_ENV_FILE})",
    )
    ap.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open the verification URL in a browser",
    )
    ap.add_argument(
        "--print-only",
        action="store_true",
        help="Print the token to stdout instead of writing to the .env file",
    )
    args = ap.parse_args()

    print(f"{BOLD}danger_zone — GitHub device-flow login{RESET}")
    print(f"client_id : {args.client_id}")
    print(f"scope     : {args.scope}\n")

    try:
        device = request_device_code(args.client_id, args.scope)
    except httpx.HTTPError as e:
        print(f"{RED}Failed to request device code: {e}{RESET}", file=sys.stderr)
        return 1

    user_code = device["user_code"]
    verification_uri = device["verification_uri"]
    device_code = device["device_code"]
    interval = int(device.get("interval", 5))
    expires_in = int(device.get("expires_in", 900))

    print(f"{BOLD}1.{RESET} Open this URL in any browser:")
    print(f"   {CYAN}{verification_uri}{RESET}\n")
    print(f"{BOLD}2.{RESET} Enter this one-time code:")
    print(f"   {GREEN}{BOLD}{user_code}{RESET}\n")
    print(f"   (code expires in {expires_in // 60} min; polling every {interval}s)\n")

    if not args.no_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(verification_uri)

    print(f"{YELLOW}Waiting for you to authorise…{RESET} ", end="", flush=True)
    try:
        token = poll_for_token(args.client_id, device_code, interval, expires_in)
    except RuntimeError as e:
        print(f"\n{RED}{e}{RESET}", file=sys.stderr)
        return 1
    print(f"{GREEN}done.{RESET}\n")

    login = verify_token(token)
    if login:
        print(f"{GREEN}Authenticated as {BOLD}{login}{RESET}{GREEN} ✓{RESET}\n")
    else:
        print(f"{YELLOW}Got a token but /user check failed (token may still work).{RESET}\n")

    if args.print_only:
        print(token)
        return 0

    upsert_env_token(args.env_file, token)
    print(f"Wrote GITHUB_TOKEN to {BOLD}{args.env_file}{RESET}")
    print(
        f"\n{BOLD}Next:{RESET} restart the server so the new token is picked up:\n"
        f"  {CYAN}DEBUG=true uvicorn app.main:app --reload --port 8001{RESET}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
