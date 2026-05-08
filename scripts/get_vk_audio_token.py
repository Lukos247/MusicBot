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
"""

from __future__ import annotations

import getpass
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Kate Mobile constants (publicly known — these are not secrets, they
# are the application id/secret of the Kate Mobile Android app).
KATE_CLIENT_ID = "2685278"
KATE_CLIENT_SECRET = "lxhD8OD7dMsqtXIm5IUY"
KATE_USER_AGENT = (
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; "
    "x86; unknown Android SDK built for x86; en)"
)

OAUTH_URL = "https://oauth.vk.com/token"


def _post(params: dict) -> dict:
    body = urlencode(params).encode("utf-8")
    req = Request(
        OAUTH_URL,
        data=body,
        headers={
            "User-Agent": KATE_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # oauth.vk.com returns non-2xx responses with a JSON body on
        # error (invalid_grant, need_validation, etc). urllib raises
        # HTTPError; pull the body out manually.
        body = getattr(e, "read", None)
        if callable(body):
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                pass
        raise


def main() -> int:
    print("VK audio token receiver — Kate Mobile direct-auth flow")
    print("(your credentials stay local; only the resulting token is printed)\n")

    login = input("Login (phone or email): ").strip()
    if not login:
        print("Login is required.", file=sys.stderr)
        return 1
    password = getpass.getpass("Password: ")
    if not password:
        print("Password is required.", file=sys.stderr)
        return 1

    base_params = {
        "grant_type": "password",
        "client_id": KATE_CLIENT_ID,
        "client_secret": KATE_CLIENT_SECRET,
        "username": login,
        "password": password,
        "scope": "audio,offline",
        "2fa_supported": "1",
        "force_sms": "1",
        "v": "5.131",
    }

    response = _post(base_params)

    # If 2FA is on, VK responds with validation_type and validation_sid.
    if response.get("error") == "need_validation":
        print(f"\nTwo-factor required ({response.get('validation_type')}).")
        if response.get("phone_mask"):
            print(f"SMS sent to {response['phone_mask']}.")
        code = input("Enter the SMS / authenticator code: ").strip()
        if not code:
            print("Code is required.", file=sys.stderr)
            return 1
        params2 = dict(base_params)
        params2["code"] = code
        response = _post(params2)

    if response.get("error") == "need_captcha":
        print(f"\nCAPTCHA required: {response.get('captcha_img')}", file=sys.stderr)
        print("Open the URL above, solve the CAPTCHA, and paste the result.", file=sys.stderr)
        captcha_key = input("CAPTCHA solution: ").strip()
        params3 = dict(base_params)
        params3["captcha_sid"] = response["captcha_sid"]
        params3["captcha_key"] = captcha_key
        response = _post(params3)

    token = response.get("access_token")
    if not token:
        print(f"\nFailed to obtain token: {json.dumps(response, ensure_ascii=False, indent=2)}",
              file=sys.stderr)
        return 1

    print("\n=== SUCCESS ===")
    print(f"User ID: {response.get('user_id')}")
    print(f"Expires in: {response.get('expires_in')} (0 = never)")
    print(f"\nYour VK_TOKEN:\n{token}\n")
    print("Set it as a Fly secret:")
    print(f'  fly secrets set "VK_TOKEN={token}" -a musicbot-dgxowq')
    return 0


if __name__ == "__main__":
    sys.exit(main())
