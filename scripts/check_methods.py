"""What can this Razorpay account actually do?

Run:  python -m scripts.check_methods

Not every payment method is enabled on every account, and the ones that are
enabled differ between test and live. Assuming otherwise costs an evening: the
first payment we tried was a card, on an account that accepts domestic cards
only, with a card number from Razorpay's own documentation that turns out to be
an international BIN. See BUGLOG 2026-08-24.

`/methods` is unauthenticated and takes only the public key id — it is the same
call Razorpay Checkout makes before deciding which tabs to draw. So this is the
account telling us what it supports, rather than us guessing from docs.

The demo depends on this. If a method is off here, it cannot appear in the video.
"""

from __future__ import annotations

import sys

import httpx

from app.config import get_settings

# Methods Recoup could plausibly use to collect a recovered payment.
OF_INTEREST = (
    "card", "debit_card", "credit_card", "netbanking", "upi", "upi_intent",
    "wallet", "emi", "paylater", "nach",
)


def _describe(value: object) -> tuple[bool, str]:
    """Return (enabled, human description) for whatever shape the field is."""
    if isinstance(value, bool):
        return value, "yes" if value else "no"
    if isinstance(value, dict):
        # Two shapes live in this response. Most are {option: bool}. netbanking is
        # {bank_code: bank_name} — every value a string, none of them True. Treating
        # both the same way reports "none enabled" for a bank list 40 long.
        if all(isinstance(v, bool) for v in value.values()):
            on = sorted(k for k, v in value.items() if v is True)
        else:
            on = sorted(value)
        if not on:
            return False, "none enabled"
        if len(on) > 6:
            return True, f"{len(on)} options"
        return True, ", ".join(on)
    if isinstance(value, list):
        return bool(value), f"{len(value)} options" if value else "none enabled"
    return False, str(value)


def main() -> int:
    settings = get_settings()
    settings.require_razorpay()

    try:
        response = httpx.get(
            f"{settings.razorpay_base_url}/methods",
            params={"key_id": settings.razorpay_key_id},
            timeout=20,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"could not reach /methods: {exc}")
        return 1

    methods = response.json()
    print(f"account: {settings.razorpay_key_id}  (test mode)\n")

    usable: list[str] = []
    for name in OF_INTEREST:
        if name not in methods:
            continue
        enabled, detail = _describe(methods[name])
        mark = "ON " if enabled else "off"
        print(f"  {mark}  {name:<14} {detail}")
        if enabled:
            usable.append(name)

    print()
    if "netbanking" in usable:
        print("Happy path for the demo: netbanking. Test mode shows a simulated bank")
        print("page with Success and Failure buttons, so both outcomes are reachable")
        print("on demand, which is what the failure demo needs.")
    elif "upi" in usable:
        print("Happy path for the demo: UPI. success@razorpay / failure@razorpay.")
    else:
        print("No reliably simulatable method is enabled. Check the dashboard.")

    if methods.get("card") and not methods.get("card_networks", {}):
        print()
        print("Cards are enabled but this account is domestic-only: an international")
        print("BIN (including 4111 1111 1111 1111) fails at payment_initiation with")
        print("error_source=business. Useful as a failure generator, not a happy path.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
