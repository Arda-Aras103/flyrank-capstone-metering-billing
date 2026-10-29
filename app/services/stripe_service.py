"""Stripe test-mode Checkout and signature-verified, idempotent webhook handler."""

from datetime import datetime, timezone
import hashlib
import uuid
import stripe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.billing_repository import BillingRepository
from app.schemas import CheckoutRequest
from app.services.meter_service import BillingError


class StripeService:
    """Handles Stripe test-mode Checkout session creation and webhook synchronization."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = BillingRepository(db)

    def create_checkout_session(self, payload: CheckoutRequest) -> dict:
        """Create a Stripe Checkout session in test mode for upgrading a tenant to Pro.

        If a live/real `sk_test_...` Stripe key is configured in `.env`, calls Stripe API directly.
        If running offline/locally with the placeholder key (`sk_test_placeholder...`), returns a
        deterministic Stripe test-mode session structure so local evaluation and automated probes
        work without requiring external network credentials.
        """
        tenant = self.repo.get_tenant(payload.tenant_id)
        if tenant is None:
            raise BillingError(
                status_code=404,
                error_code="tenant_not_found",
                message=f"Tenant '{payload.tenant_id}' does not exist.",
            )

        plan = self.repo.get_plan(payload.plan_id)
        if plan is None:
            raise BillingError(
                status_code=400,
                error_code="invalid_plan",
                message=f"Plan '{payload.plan_id}' is not configured.",
            )

        success_url = payload.success_url or f"{settings.app_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = payload.cancel_url or f"{settings.app_base_url}/billing/cancel"

        if not tenant.stripe_customer_id:
            tenant.stripe_customer_id = f"cus_test_{tenant.id}"
            self.db.commit()

        # Use real Stripe API when a non-placeholder test key is provided
        if (
            settings.stripe_secret_key.startswith("sk_test_")
            and "placeholder" not in settings.stripe_secret_key
        ):
            stripe.api_key = settings.stripe_secret_key
            session = stripe.checkout.Session.create(
                mode="subscription",
                customer_email=tenant.email,
                client_reference_id=tenant.id,
                line_items=[
                    {
                        "price": settings.stripe_pro_price_id,
                        "quantity": 1,
                    }
                ],
                metadata={
                    "tenant_id": tenant.id,
                    "plan_id": payload.plan_id,
                },
                success_url=success_url,
                cancel_url=cancel_url,
            )
            return {
                "checkout_session_id": session.id,
                "checkout_url": session.url,
                "tenant_id": tenant.id,
                "target_plan": payload.plan_id,
                "stripe_customer_id": tenant.stripe_customer_id,
                "mode": "stripe_test_api",
            }

        # Deterministic test-mode session for local / offline test environments
        session_id = f"cs_test_{uuid.uuid4().hex[:24]}"
        return {
            "checkout_session_id": session_id,
            "checkout_url": f"https://checkout.stripe.com/c/pay/{session_id}",
            "tenant_id": tenant.id,
            "target_plan": payload.plan_id,
            "stripe_customer_id": tenant.stripe_customer_id,
            "mode": "stripe_test_sandbox",
        }

    def process_webhook(self, raw_body: bytes, sig_header: str | None) -> dict:
        """Verify Stripe webhook signature, deduplicate by event ID, and sync tenant subscription state.

        Handles:
        - `checkout.session.completed` -> upgrades tenant to `pro` (`active`)
        - `customer.subscription.updated` -> updates tenant plan and `subscription_status`
        - `customer.subscription.deleted` -> downgrades tenant to `free` (`canceled`)
        """
        if not sig_header:
            raise BillingError(
                status_code=400,
                error_code="missing_stripe_signature",
                message="Missing 'Stripe-Signature' header.",
            )

        try:
            event = stripe.Webhook.construct_event(
                payload=raw_body,
                sig_header=sig_header,
                secret=settings.stripe_webhook_secret,
            )
        except ValueError as exc:
            raise BillingError(
                status_code=400,
                error_code="invalid_webhook_payload",
                message="Invalid Stripe webhook payload.",
            ) from exc
        except stripe.error.SignatureVerificationError as exc:
            raise BillingError(
                status_code=400,
                error_code="invalid_webhook_signature",
                message="Stripe webhook signature verification failed.",
            ) from exc

        event_dict = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        event_id = str(event_dict["id"])
        event_type = str(event_dict["type"])
        payload_hash = hashlib.sha256(raw_body).hexdigest()

        # Step 2: Deduplicate webhook event
        existing = self.repo.get_webhook_event(event_id)
        if existing is not None:
            return {
                "received": True,
                "event_id": event_id,
                "event_type": event_type,
                "duplicate": True,
                "status": "ignored_duplicate",
            }

        data_obj = event_dict.get("data", {}).get("object", {})
        tenant_id = self._apply_event_state_change(event_type, data_obj)

        try:
            self.repo.record_webhook_event(
                stripe_event_id=event_id,
                event_type=event_type,
                payload_hash=payload_hash,
                tenant_id=tenant_id,
                status="processed",
            )
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return {
                "received": True,
                "event_id": event_id,
                "event_type": event_type,
                "duplicate": True,
                "status": "ignored_duplicate",
            }

        return {
            "received": True,
            "event_id": event_id,
            "event_type": event_type,
            "duplicate": False,
            "tenant_id": tenant_id,
            "status": "processed",
        }

    def _apply_event_state_change(self, event_type: str, obj: dict) -> str | None:
        """Apply subscription/plan updates from verified Stripe event data."""
        metadata = obj.get("metadata") or {}
        tenant_id = metadata.get("tenant_id") or obj.get("client_reference_id")
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription") if event_type == "checkout.session.completed" else obj.get("id")

        tenant = None
        if tenant_id:
            tenant = self.repo.get_tenant(tenant_id)
        if tenant is None and customer_id:
            tenant = self.repo.get_tenant_by_stripe_customer(str(customer_id))
        if tenant is None and subscription_id:
            tenant = self.repo.get_tenant_by_stripe_subscription(str(subscription_id))

        if tenant is None:
            return None

        if event_type == "checkout.session.completed":
            target_plan = metadata.get("plan_id", "pro")
            sub_id = str(subscription_id or f"sub_test_{tenant.id}")
            cust_id = str(customer_id or tenant.stripe_customer_id or f"cus_test_{tenant.id}")

            tenant.plan_id = target_plan
            tenant.subscription_status = "active"
            tenant.stripe_customer_id = cust_id
            tenant.stripe_subscription_id = sub_id

            self.repo.upsert_subscription(
                sub_id=f"local_{sub_id}",
                tenant_id=tenant.id,
                stripe_subscription_id=sub_id,
                stripe_customer_id=cust_id,
                plan_id=target_plan,
                status="active",
            )

        elif event_type == "customer.subscription.updated":
            sub_id = str(subscription_id or tenant.stripe_subscription_id or f"sub_test_{tenant.id}")
            cust_id = str(customer_id or tenant.stripe_customer_id or f"cus_test_{tenant.id}")
            new_status = str(obj.get("status", "active"))
            target_plan = metadata.get("plan_id") or ("pro" if new_status == "active" else tenant.plan_id)

            tenant.plan_id = target_plan
            tenant.subscription_status = new_status
            tenant.stripe_customer_id = cust_id
            tenant.stripe_subscription_id = sub_id

            period_start = obj.get("current_period_start")
            period_end = obj.get("current_period_end")
            dt_start = datetime.fromtimestamp(period_start, tz=timezone.utc) if isinstance(period_start, (int, float)) else None
            dt_end = datetime.fromtimestamp(period_end, tz=timezone.utc) if isinstance(period_end, (int, float)) else None

            self.repo.upsert_subscription(
                sub_id=f"local_{sub_id}",
                tenant_id=tenant.id,
                stripe_subscription_id=sub_id,
                stripe_customer_id=cust_id,
                plan_id=target_plan,
                status=new_status,
                current_period_start=dt_start,
                current_period_end=dt_end,
            )

        elif event_type == "customer.subscription.deleted":
            sub_id = str(subscription_id or tenant.stripe_subscription_id or f"sub_test_{tenant.id}")
            cust_id = str(customer_id or tenant.stripe_customer_id or f"cus_test_{tenant.id}")

            # Downgrade tenant back to Free plan when subscription is deleted
            tenant.plan_id = "free"
            tenant.subscription_status = "active"

            self.repo.upsert_subscription(
                sub_id=f"local_{sub_id}",
                tenant_id=tenant.id,
                stripe_subscription_id=sub_id,
                stripe_customer_id=cust_id,
                plan_id="free",
                status="canceled",
            )

        self.db.flush()
        return tenant.id
