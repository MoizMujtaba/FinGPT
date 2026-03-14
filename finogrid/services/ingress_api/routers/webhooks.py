"""Webhook receiver — accepts inbound partner status callbacks."""
import hashlib
import hmac
import uuid
import structlog
from fastapi import APIRouter, Request, HTTPException, Header, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from google.cloud import pubsub_v1

from ..dependencies import get_db
from ..config import settings
from ....database.models.batch import PayoutTask, TaskStatus
from ....database.models.execution import ExecutionEvent, EventType

log = structlog.get_logger()
router = APIRouter()


def _verify_bridge_signature(body: bytes, signature: str) -> bool:
    """Verify Bridge HMAC-SHA256 webhook signature (header: X-Bridge-Signature: sha256=<hex>)."""
    if not settings.bridge_webhook_secret:
        return True  # disabled in local/dev
    expected = hmac.new(
        settings.bridge_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    sig_value = signature.split("=", 1)[-1] if "=" in signature else signature
    return hmac.compare_digest(expected, sig_value)


def _verify_kyt_signature(body: bytes, signature: str) -> bool:
    """Verify KYT provider HMAC-SHA256 webhook signature."""
    if not settings.kyt_webhook_secret:
        return True
    expected = hmac.new(
        settings.kyt_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    sig_value = signature.split("=", 1)[-1] if "=" in signature else signature
    return hmac.compare_digest(expected, sig_value)


async def _publish_reconcile(task_id: str) -> None:
    """Publish a reconciliation trigger so the reconciler picks up the task."""
    try:
        publisher = pubsub_v1.PublisherClient()
        topic = f"projects/{settings.pubsub_project_id}/topics/reconciliation-trigger"
        publisher.publish(topic, data=task_id.encode())
    except Exception as exc:  # noqa: BLE001
        log.warning("reconciliation_trigger_failed", task_id=task_id, error=str(exc))


# Bridge status → (ExecutionEvent type, TaskStatus or None)
_BRIDGE_STATUS_MAP: dict[str, tuple[EventType, TaskStatus | None]] = {
    "completed":  (EventType.PARTNER_COMPLETED, TaskStatus.COMPLETED),
    "failed":     (EventType.PARTNER_FAILED,    TaskStatus.FAILED),
    "cancelled":  (EventType.TASK_CANCELLED,    TaskStatus.CANCELLED),
    "processing": (EventType.PARTNER_STATUS_UPDATE, None),
    "pending":    (EventType.PARTNER_STATUS_UPDATE, None),
}


@router.post("/bridge")
async def bridge_webhook(
    request: Request,
    x_bridge_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive status updates from Bridge; update execution_events and trigger reconciliation."""
    raw_body = await request.body()
    body = await request.json()

    # 1. Verify HMAC signature
    if x_bridge_signature and not _verify_bridge_signature(raw_body, x_bridge_signature):
        log.warning("bridge_webhook_invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = body.get("event_type", "")
    transfer_data = body.get("transfer", body)
    task_id_str = (
        transfer_data.get("metadata", {}).get("finogrid_task_id")
        or body.get("finogrid_task_id")
    )
    bridge_transfer_id = transfer_data.get("id") or body.get("transfer_id")
    bridge_status = (transfer_data.get("status") or body.get("status", "")).lower()

    log.info(
        "bridge_webhook_received",
        event_type=event_type,
        task_id=task_id_str,
        bridge_id=bridge_transfer_id,
        bridge_status=bridge_status,
    )

    if not task_id_str:
        return {"received": True, "note": "no task_id in payload"}

    try:
        task_id = uuid.UUID(task_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid task_id: {task_id_str}")

    # 2. Look up task
    result = await db.execute(select(PayoutTask).where(PayoutTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        log.warning("bridge_webhook_task_not_found", task_id=task_id_str)
        return {"received": True, "note": "task not found"}

    ev_type, new_status = _BRIDGE_STATUS_MAP.get(
        bridge_status, (EventType.PARTNER_STATUS_UPDATE, None)
    )

    # 3. Append execution event
    db.add(ExecutionEvent(
        task_id=task_id,
        event_type=ev_type,
        partner="bridge",
        partner_ref=bridge_transfer_id,
        payload=body,
    ))

    # 4. Update task
    if bridge_transfer_id and not task.partner_tx_id:
        task.partner_tx_id = bridge_transfer_id
    if new_status:
        task.status = new_status
        if new_status == TaskStatus.FAILED:
            task.failure_reason = (
                transfer_data.get("failure_reason")
                or body.get("failure_reason")
                or f"Bridge transfer failed (status={bridge_status})"
            )

    await db.commit()
    log.info("bridge_webhook_processed", task_id=task_id_str, ev_type=ev_type, new_status=new_status)

    # 5. Trigger reconciliation
    await _publish_reconcile(task_id_str)

    return {"received": True}


@router.post("/kyt")
async def kyt_webhook(
    request: Request,
    x_kyt_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive KYT/AML screening callbacks; update compliance_result and release or hold task."""
    raw_body = await request.body()
    body = await request.json()

    if x_kyt_signature and not _verify_kyt_signature(raw_body, x_kyt_signature):
        log.warning("kyt_webhook_invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    ref = body.get("ref")
    task_id_str = body.get("task_id") or body.get("finogrid_task_id")
    decision = (body.get("decision") or body.get("result") or "").lower()

    log.info("kyt_webhook_received", ref=ref, task_id=task_id_str, decision=decision)

    if not task_id_str:
        return {"received": True, "note": "no task_id in payload"}

    try:
        task_id = uuid.UUID(task_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid task_id: {task_id_str}")

    result = await db.execute(select(PayoutTask).where(PayoutTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        log.warning("kyt_webhook_task_not_found", task_id=task_id_str)
        return {"received": True, "note": "task not found"}

    # Persist raw compliance result
    task.compliance_result = body

    # Map decision → task transition
    if decision in ("approved", "pass", "clear"):
        ev_type = EventType.COMPLIANCE_PASS
        task.status = TaskStatus.EXECUTING
    elif decision in ("rejected", "fail", "block", "denied"):
        ev_type = EventType.COMPLIANCE_FAIL
        task.status = TaskStatus.FAILED
        task.failure_reason = f"KYT block — ref: {ref}, decision: {decision}"
    else:
        # "review", unknown → hold for manual ops triage
        ev_type = EventType.COMPLIANCE_HOLD
        task.status = TaskStatus.HELD_FOR_REVIEW

    db.add(ExecutionEvent(
        task_id=task_id,
        event_type=ev_type,
        partner="kyt_aml",
        partner_ref=ref,
        payload=body,
    ))
    await db.commit()

    log.info(
        "kyt_webhook_processed",
        task_id=task_id_str,
        decision=decision,
        ev_type=ev_type,
        new_status=task.status,
    )

    return {"received": True}
