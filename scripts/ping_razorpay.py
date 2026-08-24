"""Smoke test: prove the credentials work and the client can reach Razorpay.

Run:  python -m scripts.ping_razorpay

Creates nothing. It only reads, so it is safe to run as often as you like.
"""

from __future__ import annotations

import sys

from app.config import get_settings
from app.razorpay_client import CircuitOpen, RazorpayClient, RazorpayError


def main() -> int:
    settings = get_settings()
    print(f"key id      : {settings.razorpay_key_id}")
    print(f"base url    : {settings.razorpay_base_url}")

    try:
        with RazorpayClient() as client:
            result = client.ping()
    except CircuitOpen as exc:
        print(f"\nCIRCUIT OPEN: {exc}")
        return 2
    except RazorpayError as exc:
        print(f"\nFAILED after {exc.attempts} attempt(s): {exc}")
        if exc.status == 401:
            print("401 means the key id and secret do not match. Regenerate the")
            print("test key in the Razorpay dashboard and update .env.")
        return 1

    payments = result.data.get("items", [])
    print(f"\nOK in {result.elapsed_ms}ms after {result.attempts} attempt(s)")
    print(f"payments visible on this account: {result.data.get('count', 0)}")
    if payments:
        p = payments[0]
        rupees = p.get("amount", 0) / 100
        print(f"most recent: {p.get('id')}  Rs {rupees:,.2f}  {p.get('status')}")
    else:
        print("no payments yet, which is expected on a fresh test account")
    return 0


if __name__ == "__main__":
    sys.exit(main())
