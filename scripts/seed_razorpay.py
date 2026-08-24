"""Create real entities in Razorpay test mode, and record each one in the ledger.

Run:  python -m scripts.seed_razorpay

This exists to prove a specific claim: that the objects in the Razorpay dashboard
were created by this code, and that every one of them has a matching audit entry
on our side. Those two lists agreeing is the whole point of the ledger.

Customer details use Razorpay's own documentation placeholders, and notifications
are forced off by the client, so nothing is sent to anyone.
"""

from __future__ import annotations

import sys

from app import ledger
from app.db import SessionLocal, init_db
from app.razorpay_client import RazorpayClient, RazorpayError

# Razorpay's documentation placeholder identity. Obviously fake, deliberately.
# The contact is NOT +919999999999: /customers accepts that, but /payment_links
# rejects repeated digits as a likely-fake number. See BUGLOG 2026-08-24.
DEMO_CUSTOMER = {
    "name": "Gaurav Kumar",
    "email": "gaurav.kumar@example.com",
    "contact": "+919876543210",
}

DEMO_AMOUNT_PAISE = 120_000  # Rs 1,200.00


def main() -> int:
    init_db()
    created: list[tuple[str, str]] = []

    with SessionLocal() as session, RazorpayClient() as client:
        try:
            customer = client.create_customer(
                **DEMO_CUSTOMER, notes={"source": "recoup-seed"}
            )
            customer_id = customer.data["id"]
            created.append(("customer", customer_id))
            ledger.record(
                session, actor="system", event="seed_customer_created",
                payload={"razorpay_id": customer_id,
                         "attempts": customer.attempts,
                         "elapsed_ms": customer.elapsed_ms},
            )

            order = client.create_order(
                amount_paise=DEMO_AMOUNT_PAISE,
                receipt="recoup-seed-001",
                notes={"source": "recoup-seed"},
            )
            order_id = order.data["id"]
            created.append(("order", order_id))
            ledger.record(
                session, actor="system", event="seed_order_created",
                payload={"razorpay_id": order_id,
                         "amount_paise": DEMO_AMOUNT_PAISE,
                         "attempts": order.attempts,
                         "elapsed_ms": order.elapsed_ms},
            )

            link = client.create_payment_link(
                amount_paise=DEMO_AMOUNT_PAISE,
                description="Recoup seed: recovery link for order recoup-seed-001",
                customer=DEMO_CUSTOMER,
                notes={"source": "recoup-seed", "case_id": "case_seed_001"},
            )
            link_id = link.data["id"]
            created.append(("payment_link", link_id))
            ledger.record(
                session, actor="system", event="seed_payment_link_created",
                case_id="case_seed_001",
                payload={"razorpay_id": link_id,
                         "short_url": link.data.get("short_url"),
                         "amount_paise": DEMO_AMOUNT_PAISE,
                         "attempts": link.attempts,
                         "elapsed_ms": link.elapsed_ms},
            )

        except RazorpayError as exc:
            print(f"FAILED after {exc.attempts} attempt(s): {exc}")
            if exc.body:
                print(f"razorpay said: {exc.body}")
            print(f"\ncreated before failing: {created or 'nothing'}")
            return 1

        print("Created in Razorpay test mode:")
        for kind, entity_id in created:
            print(f"  {kind:14} {entity_id}")
        print(f"\npayable link: {link.data.get('short_url')}")

        chain = ledger.verify_chain(session)
        print(f"ledger: {chain.entries} entries, {chain.detail}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
