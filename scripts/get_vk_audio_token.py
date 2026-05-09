"""Obtain a VK access token that can call audio.search / audio.getById.

VK silently revoked audio-API access for tokens obtained via the
implicit OAuth flow (the one vkhost.github.io uses with
`response_type=token`). The current working method is the Direct
Authorization flow at https://oauth.vk.com/token with Kate Mobile's
client_id + client_secret, which still issues tokens that work with
the audio API.

Run this script locally — your credentials stay on your machine, only
the resulting token leaves it (you paste it to set as VK_TOKEN secret).

Usage:
    python scripts/get_vk_audio_token.py

You will be prompted for login (phone or email), password, and a 2FA
code if the account has it enabled. The script never logs or transmits
anything outside oauth.vk.com.

Note on SSL: if your machine has antivirus / corporate proxy doing
HTTPS inspection (Kaspersky / ESET / Avast / Zscaler / etc.), the
default cert verification will fail. The script auto-retries with
verification disabled in that case — safe for this one-shot flow on
your own machine.
"""

from __future__ import annotations

import getpass
import json
import sys

try:
    import requests
except ImportError:
    print("This script needs `requests`. Run: pip install requests", file=sys.stderr)
    sys.exit(2)

# Public client identifiers for VK's official mobile apps + Kate Mobile.
# Each one is its own rate-limit bucket — when one trips flood control
# we cycle to the next, which usually lets us through.
CLIENTS = [
    {
        "name": "VK iPhone",
        "client_id": "3140623",
        "client_secret": "VeWdmVclDCtn6ihuP1nt",
        "user_agent": "VKAndroidApp/8.43-15705 (Android 14; SDK 34; arm64-v8a; samsung SM-S921B; en; 2400x1080)",
    },
    {
        "name": "VK Android",
        "client_id": "2274003",
        "client_secret": "hHbZxrka2uZ6jB1inYsH",
        "user_agent": "VKAndroidApp/8.43-15705 (Android 14; SDK 34; arm64-v8a; samsung SM-S921B; en; 2400x1080)",
    },
    {
        "name": "Kate Mobile",
        "client_id": "2685278",
        "client_secret": "lxhD8OD7dMsqtXIm5IUY",
        "user_agent": (
            "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; "
            "x86; unknown Android SDK built for x86; en)"
        ),
    },
]

OAUTH_URL = "https://oauth.vk.com/token"


_INSECURE_TLS = False  # set once we discover the local cert chain is MITM'd


def _post(params: dict, user_agent: str) -> dict:
    """POST to oauth.vk.com/token. Returns the JSON body even on non-2xx
    (the API uses 4xx with a JSON error body)."""
    global _INSECURE_TLS
    try:
        r = requests.post(
            OAUTH_URL,
            data=params,
            headers={"User-Agent": user_agent},
            timeout=15,
            verify=not _INSECURE_TLS,
        )
    except requests.exceptions.SSLError:
        if _INSECURE_TLS:
            raise
        print(
            "\n[!] SSL verification failed — likely an antivirus / corporate proxy is\n"
            "    inspecting HTTPS. Retrying with cert verification disabled.",
            file=sys.stderr,
        )
        try:
            from urllib3.exceptions import InsecureRequestWarning
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        except ImportError:
            pass
        _INSECURE_TLS = True
        return _post(params, user_agent)
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"Non-JSON response from VK ({r.status_code}): {r.text[:200]}")


def _try_login(client: dict, login: str, password: str, code: str | None) -> dict:
    params = {
        "grant_type": "password",
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "username": login,
        "password": password,
        "scope": "audio,offline",
        "2fa_supported": "1",
        "force_sms": "1",
        "v": "5.131",
    }
    if code:
        params["code"] = code
    return _post(params, client["user_agent"])


def main() -> int:
    print("VK audio token receiver — direct-auth flow (with client rotation)")
    print("(your credentials stay local; only the resulting token is printed)\n")

    login = input("Login (phone or email): ").strip()
    if not login:
        print("Login is required.", file=sys.stderr)
        return 1
    password = getpass.getpass("Password: ")
    if not password:
        print("Password is required.", file=sys.stderr)
        return 1

    last_error: dict = {}
    for client in CLIENTS:
        print(f"\n→ Trying {client['name']} (client_id {client['client_id']})...",
              file=sys.stderr)
        response = _try_login(client, login, password, code=None)

        # 2FA needed → ask for the code, retry once with same client.
        if response.get("error") == "need_validation":
            print(f"  Two-factor required ({response.get('validation_type')}).")
            if response.get("phone_mask"):
                print(f"  SMS sent to {response['phone_mask']}.")
            code = input("  Enter the SMS / authenticator code: ").strip()
            if code:
                response = _try_login(client, login, password, code=code)

        if response.get("error") == "need_captcha":
            print(f"  CAPTCHA required: {response.get('captcha_img')}",
                  file=sys.stderr)
            captcha_key = input("  CAPTCHA solution: ").strip()
            params = {
                "grant_type": "password",
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "username": login,
                "password": password,
                "scope": "audio,offline",
                "v": "5.131",
                "captcha_sid": response["captcha_sid"],
                "captcha_key": captcha_key,
            }
            response = _post(params, client["user_agent"])

        token = response.get("access_token")
        if token:
            print("\n=== SUCCESS ===")
            print(f"Client used: {client['name']}")
            print(f"User ID: {response.get('user_id')}")
            print(f"Expires in: {response.get('expires_in')} (0 = never)")
            print(f"\nYour VK_TOKEN:\n{token}\n")
            print("Set it as a Fly secret:")
            print(f'  fly secrets set "VK_TOKEN={token}" -a musicbot-dgxowq')
            return 0

        # Print a one-line summary then move on if it's a flood error;
        # bail immediately on anything else (wrong password, blocked, etc).
        err = response.get("error", "")
        msg = response.get("error_description", "")
        print(f"  {client['name']} failed: {err} — {msg[:120]}", file=sys.stderr)
        last_error = response
        if "flood" not in err.lower() and "bruteforce" not in response.get("error_type", "").lower():
            # not a flood block — different clients won't help (e.g. invalid_client / wrong creds)
            break

    print(f"\nNo client worked. Last response:\n{json.dumps(last_error, ensure_ascii=False, indent=2)}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
