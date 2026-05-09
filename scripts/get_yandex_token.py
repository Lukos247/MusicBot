"""Obtain a Yandex.Music access token via the OAuth implicit flow.

Yandex's implicit-flow OAuth (response_type=token) returns a long-lived
token (1 year typical) tied to the Yandex.Music desktop client. With an
active Yandex Plus subscription this token can stream + download tracks
through `yandex-music`.

Usage:
    python scripts/get_yandex_token.py

The script prints the OAuth URL, you open it in your browser (logged
into the Yandex account that has Plus), authorize, and copy the
`access_token` from the redirect URL fragment back into the script
prompt. The script optionally validates the token by hitting
account/status, then prints the `fly secrets set` command.

Note on SSL: handles antivirus / corporate HTTPS inspection just like
the previous VK helper — auto-retries with verify=False on the first
SSL chain error.
"""

from __future__ import annotations

import json
import sys

try:
    import requests
except ImportError:
    print("This script needs `requests`. Run: pip install requests", file=sys.stderr)
    sys.exit(2)

# Public client_id of the Yandex.Music desktop player. Anyone gets the
# same one; the scope is fixed by Yandex on the server side.
YANDEX_MUSIC_CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
OAUTH_URL = (
    "https://oauth.yandex.ru/authorize"
    f"?response_type=token&client_id={YANDEX_MUSIC_CLIENT_ID}"
)

VALIDATE_URL = "https://api.music.yandex.net/account/status"


def _get(url: str, headers: dict, *, insecure: bool = False) -> dict | None:
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=not insecure)
    except requests.exceptions.SSLError:
        if insecure:
            raise
        print(
            "\n[!] SSL verification failed — likely antivirus / corporate proxy.\n"
            "    Retrying with cert verification disabled.",
            file=sys.stderr,
        )
        try:
            from urllib3.exceptions import InsecureRequestWarning
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        except ImportError:
            pass
        return _get(url, headers, insecure=True)
    try:
        return r.json()
    except ValueError:
        print(f"Non-JSON response ({r.status_code}): {r.text[:200]}", file=sys.stderr)
        return None


def main() -> int:
    print("Yandex.Music token receiver — OAuth implicit flow\n")
    print("1) Open this URL in your browser (logged into the Yandex account")
    print("   that owns your Plus subscription):\n")
    print(f"   {OAUTH_URL}\n")
    print("2) Click 'Allow'. Yandex will redirect you to a URL of the form:")
    print("       https://oauth.yandex.ru/verification_code#access_token=y0_AgAAAAA...&token_type=bearer&...")
    print("3) Copy the value between `access_token=` and `&` (everything after")
    print("   `y0_AgAAAAA...` is part of the token; usually ~58 chars).\n")

    token = input("Paste your access_token: ").strip()
    if not token:
        print("Token is required.", file=sys.stderr)
        return 1
    # Strip any leading "access_token=" if user copied that too
    if token.startswith("access_token="):
        token = token[len("access_token="):]
    if "&" in token:
        token = token.split("&", 1)[0]

    # Validate
    print("\nValidating...", file=sys.stderr)
    headers = {
        "Authorization": f"OAuth {token}",
        "User-Agent": "Yandex-Music-API",
    }
    data = _get(VALIDATE_URL, headers)
    if data and data.get("result"):
        account = data["result"].get("account") or {}
        plus = data["result"].get("plus") or {}
        print(f"\n=== SUCCESS ===")
        print(f"Account: {account.get('displayName') or account.get('login') or '?'}")
        print(f"Login: {account.get('login') or '?'}")
        print(f"Plus active: {plus.get('hasPlus', False)}")
        print(f"\nYour YANDEX_TOKEN:\n{token}\n")
        print("Set it as a Fly secret:")
        print(f'  fly secrets set "YANDEX_TOKEN={token}" -a musicbot-dgxowq')
        return 0
    print(f"\nValidation failed. Response: {json.dumps(data, ensure_ascii=False, indent=2) if data else '(none)'}",
          file=sys.stderr)
    print("Token may still be valid — proceed cautiously.", file=sys.stderr)
    print(f"\nYour token (unverified):\n{token}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
