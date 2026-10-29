"""Background jobs for usage threshold alerts (80% / 100%) and Stripe subscription reconciliation.

Satisfies Shared Requirement #3 (≥1 background job — slow/bulk work off the request path, retries + failure alert)
and Stretch Goals (Usage alerts at 80%/100% + Reconciliation job).
"""

import json
import logging
import uuid
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import BackgroundJob
from app.repositories.billing_repository import BillingRepository, current_billing_period

logger = logging.getLogger(__name__)


class BackgroundJobService:
    """Executes background jobs with bounded retry logic and failure alerting."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = BillingRepository(db)

    def evaluate_tenant_usage_alerts(self, tenant_id: str) -> dict:
        """Evaluate 80% and 100% quota thresholds for a tenant off the hot request path."""
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        max_retries = int(settings.reconciliation_max_retries)
        attempts = 0
        alerts_emitted: list[dict] = []

        while attempts < max_retries:
            attempts += 1
            try:
                tenant = self.repo.get_tenant(tenant_id)
                if tenant is None:
                    raise ValueError(f"Tenant '{tenant_id}' not found during alert evaluation")
                plan = self.repo.get_plan(tenant.plan_id)
                if plan is None:
                    raise ValueError(f"Plan '{tenant.plan_id}' not found")

                period = current_billing_period()
                buckets = [
                    ("api_call", self.repo.get_monthly_used_quantity(tenant.id, "api_call", period), int(plan.api_calls_limit)),
                    ("ai_tokens", self.repo.get_monthly_used_quantity(tenant.id, "ai_tokens", period), int(plan.ai_tokens_limit)),
                ]
                thresholds = [
                    int(settings.usage_alert_warn_threshold_pct),
                    int(settings.usage_alert_limit_threshold_pct),
                ]

                for usage_type, used, limit in buckets:
                    if limit <= 0:
                        continue
                    pct = (used * 100) // limit
                    for threshold in thresholds:
                        if pct >= threshold:
                            msg = (
                                f"ALERT [{threshold}% threshold]: Tenant '{tenant.id}' has used "
                                f"{used}/{limit} ({pct}%) of monthly '{usage_type}' quota on plan '{tenant.plan_id}'."
                            )
                            created = self.repo.create_alert_if_not_exists(
                                alert_id=f"alrt_{uuid.uuid4().hex[:14]}",
                                tenant_id=tenant.id,
                                billing_period=period,
                                usage_type=usage_type,
                                threshold_pct=threshold,
                                current_used=used,
                                plan_limit=limit,
                                message=msg,
                            )
                            if created is not None:
                                alerts_emitted.append(
                                    {
                                        "usage_type": usage_type,
                                        "threshold_pct": threshold,
                                        "used": used,
                                        "limit": limit,
                                        "message": msg,
                                    }
                                )

                job = BackgroundJob(
                    id=job_id,
                    job_type="usage_alert_evaluation",
                    tenant_id=tenant_id,
                    status="completed",
                    attempts=attempts,
                    max_retries=max_retries,
                    details=json.dumps({"alerts_emitted": alerts_emitted}),
                )
                self.repo.record_background_job(job)
                self.db.commit()
                return {
                    "job_id": job_id,
                    "job_type": "usage_alert_evaluation",
                    "status": "completed",
                    "attempts": attempts,
                    "alerts_emitted": alerts_emitted,
                }
            except Exception as exc:
                self.db.rollback()
                if attempts >= max_retries:
                    alert_msg = (
                        f"CRITICAL BACKGROUND JOB FAILURE: 'usage_alert_evaluation' for tenant "
                        f"'{tenant_id}' failed after {attempts}/{max_retries} attempts: {exc}"
                    )
                    logger.error(alert_msg)
                    failed_job = BackgroundJob(
                        id=job_id,
                        job_type="usage_alert_evaluation",
                        tenant_id=tenant_id,
                        status="failed",
                        attempts=attempts,
                        max_retries=max_retries,
                        details=json.dumps({"error": str(exc)}),
                        failure_alert=alert_msg,
                    )
                    self.repo.record_background_job(failed_job)
                    self.db.commit()
                    return {
                        "job_id": job_id,
                        "job_type": "usage_alert_evaluation",
                        "status": "failed",
                        "attempts": attempts,
                        "failure_alert": alert_msg,
                    }
        return {"job_id": job_id, "status": "failed"}

    def run_reconciliation_job(
        self,
        stripe_snapshot: list[dict] | None = None,
        simulate_transient_failures: int = 0,
    ) -> dict:
        """Compare local tenant/subscription records against Stripe subscription state and repair drift.

        Supports retry handling (`attempts` up to `max_retries`) and emits a `failure_alert` if all
        retries are exhausted.
        """
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        max_retries = int(settings.reconciliation_max_retries)
        attempts = 0

        while attempts < max_retries:
            attempts += 1
            try:
                if attempts <= simulate_transient_failures:
                    raise RuntimeError(
                        f"Simulated transient Stripe API timeout on attempt {attempts}/{max_retries}"
                    )

                reconciled: list[dict] = []
                # Reconcile from provided Stripe snapshot or local Subscription mirror vs Tenant row
                if stripe_snapshot:
                    for item in stripe_snapshot:
                        t_id = item.get("tenant_id")
                        expected_plan = item.get("plan_id", "pro")
                        expected_status = item.get("status", "active")
                        tenant = self.repo.get_tenant(t_id) if t_id else None
                        if tenant and (
                            tenant.plan_id != expected_plan
                            or tenant.subscription_status != expected_status
                        ):
                            old_plan = tenant.plan_id
                            old_status = tenant.subscription_status
                            tenant.plan_id = expected_plan
                            tenant.subscription_status = expected_status
                            reconciled.append(
                                {
                                    "tenant_id": tenant.id,
                                    "previous_plan": old_plan,
                                    "reconciled_plan": expected_plan,
                                    "previous_status": old_status,
                                    "reconciled_status": expected_status,
                                }
                            )
                else:
                    # Verify every tenant's active Subscription row matches Tenant.plan_id
                    for tenant in self.repo.list_tenants():
                        subs = self.repo.list_subscriptions_for_tenant(tenant.id)
                        for sub in subs:
                            if sub.status == "active" and tenant.plan_id != sub.plan_id:
                                old_plan = tenant.plan_id
                                tenant.plan_id = sub.plan_id
                                reconciled.append(
                                    {
                                        "tenant_id": tenant.id,
                                        "previous_plan": old_plan,
                                        "reconciled_plan": sub.plan_id,
                                    }
                                )
                        # Also evaluate usage alerts during reconciliation pass
                        self.evaluate_tenant_usage_alerts(tenant.id)

                job = BackgroundJob(
                    id=job_id,
                    job_type="stripe_reconciliation",
                    status="completed",
                    attempts=attempts,
                    max_retries=max_retries,
                    details=json.dumps({"reconciled_tenants": reconciled}),
                )
                self.repo.record_background_job(job)
                self.db.commit()
                return {
                    "job_id": job_id,
                    "job_type": "stripe_reconciliation",
                    "status": "completed",
                    "attempts": attempts,
                    "max_retries": max_retries,
                    "reconciled_tenants": reconciled,
                    "failure_alert": None,
                }
            except Exception as exc:
                self.db.rollback()
                if attempts >= max_retries:
                    alert_msg = (
                        f"CRITICAL BACKGROUND JOB FAILURE: 'stripe_reconciliation' exhausted all "
                        f"{attempts}/{max_retries} retries. Last error: {exc}"
                    )
                    logger.error(alert_msg)
                    failed_job = BackgroundJob(
                        id=job_id,
                        job_type="stripe_reconciliation",
                        status="failed",
                        attempts=attempts,
                        max_retries=max_retries,
                        details=json.dumps({"error": str(exc)}),
                        failure_alert=alert_msg,
                    )
                    self.repo.record_background_job(failed_job)
                    self.db.commit()
                    return {
                        "job_id": job_id,
                        "job_type": "stripe_reconciliation",
                        "status": "failed",
                        "attempts": attempts,
                        "max_retries": max_retries,
                        "reconciled_tenants": [],
                        "failure_alert": alert_msg,
                    }
        return {"job_id": job_id, "status": "failed"}
