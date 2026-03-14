"""
Withdraw router — trigger fiat/wallet withdrawal via v1 payout rails.

Flow:
  1. Assert KYA ≥ basic + token not expired
  2. Validate corridor + beneficiary fields (corridor adapter)
  3. Check available balance ≥ amount
  4. Debit agent balance (reserve → released once v1 batch settles)
  5. Submit single-task batch to v1 Ingress API
  6. Append ledger entry (withdrawal_initiated)
  7. Return withdrawal_id + v1_batch_id for tracking

The Agent Ledger API never moves funds itself — it delegates to the
v1 Ingress API which owns the Bridge + compliance + reconciliation pipeline.
"""
import uuid
import httpx
import structlog
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..schemas import WithdrawRequest, WithdrawResponse
from ..dependencies import get_db, get_current_agent_account, assert_kya_status
from ..config import settings
from .....database.models.agent_ledger import AgentAccount, AgentLedgerEntry

log = structlog.get_logger()
router = APIRouter()


@router.post("/{agent_account_id}/withdraw", response_model=WithdrawResponse, status_code=status.HTTP_202_ACCEPTED)
async def withdraw(
    agent_account_id: uuid.UUID,
    req: WithdrawRequest,
    db: AsyncSession = Depends(get_db),
    agent: AgentAccount = Depends(get_current_agent_account),
):
    """Trigger a fiat or wallet withdrawal via v1 payout rails."""

    # ── Guard: correct agent ───────────────────────────────────────────────
    if agent.id != agent_account_id:
        raise HTTPException(status_code=403, detail="Agent account mismatch")

    # ── Gate 1: KYA ≥ basic ───────────────────────────────────────────────
    await assert_kya_status(agent, required_level="basic")

    # ── Gate 2: corridor + beneficiary validation ──────────────────────────
    from .....corridors import get_adapter
    try:
        adapter = get_adapter(req.corridor_code.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported corridor: {req.corridor_code}",
        )

    beneficiary_with_mode = {**req.beneficiary_data, "mode": req.delivery_mode}
    validation = adapter.validate_beneficiary(beneficiary_with_mode)
    if not validation.valid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Beneficiary validation failed",
                "missing_fields": validation.missing_fields,
                "errors": validation.errors,
            },
        )

    # ── Gate 3: available balance ──────────────────────────────────────────
    available = agent.prefund_balance_usdc - agent.reserved_balance_usdc
    if available < req.amount_usdc:
        raise HTTPException(
            status_code=402,
            detail={
                "message": "Insufficient available balance",
                "available_usdc": str(available),
                "requested_usdc": str(req.amount_usdc),
            },
        )

    # ── Gate 4: corridor amount limits ────────────────────────────────────
    cfg = adapter.config
    if req.amount_usdc < Decimal(str(cfg.min_amount_usd)):
        raise HTTPException(
            status_code=400,
            detail=f"Amount below corridor minimum: ${cfg.min_amount_usd}",
        )
    if req.amount_usdc > Decimal(str(cfg.max_amount_usd)):
        raise HTTPException(
            status_code=400,
            detail=f"Amount exceeds corridor maximum: ${cfg.max_amount_usd}",
        )

    # ── Debit: reserve balance ─────────────────────────────────────────────
    withdrawal_id = uuid.uuid4()
    agent.reserved_balance_usdc += req.amount_usdc

    # ── Submit to v1 Ingress API ───────────────────────────────────────────
    v1_batch_id: uuid.UUID | None = None
    v1_error: str | None = None

    v1_payload = {
        "reference": req.reference or f"agent-withdraw-{withdrawal_id}",
        "idempotency_key": req.idempotency_key,
        "tasks": [
            {
                "corridor_code": req.corridor_code.upper(),
                "amount_usd": float(req.amount_usdc),
                "preferred_asset": "USDC",
                "preferred_mode": req.delivery_mode,
                "beneficiary_data": req.beneficiary_data,
                "metadata": {
                    "source": "agent_ledger",
                    "agent_account_id": str(agent_account_id),
                    "withdrawal_id": str(withdrawal_id),
                },
            }
        ],
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.v1_ingress_api_url,
            headers={
                "X-API-Key": settings.v1_internal_api_key,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        ) as client:
            resp = await client.post("/v1/batches", json=v1_payload)
        if resp.status_code in (200, 201, 202):
            data = resp.json()
            v1_batch_id = uuid.UUID(data["batch_id"]) if data.get("batch_id") else None
        else:
            v1_error = f"v1 API error {resp.status_code}: {resp.text[:200]}"
            log.error("withdraw_v1_submission_failed", withdrawal_id=str(withdrawal_id), detail=v1_error)
    except Exception as exc:  # noqa: BLE001
        v1_error = str(exc)
        log.error("withdraw_v1_unreachable", withdrawal_id=str(withdrawal_id), error=v1_error)

    # If v1 submission failed, release the reservation and surface the error
    if v1_error:
        agent.reserved_balance_usdc -= req.amount_usdc
        await db.commit()
        raise HTTPException(
            status_code=503,
            detail=f"Withdrawal submission failed: {v1_error}. Balance not debited.",
        )

    # ── Append ledger entry ────────────────────────────────────────────────
    db.add(AgentLedgerEntry(
        agent_account_id=agent_account_id,
        entry_type="withdrawal_initiated",
        amount_usdc=-req.amount_usdc,  # negative = outgoing
        balance_after=agent.prefund_balance_usdc - agent.reserved_balance_usdc,
        reference=str(withdrawal_id),
        metadata={
            "corridor_code": req.corridor_code.upper(),
            "delivery_mode": req.delivery_mode,
            "v1_batch_id": str(v1_batch_id) if v1_batch_id else None,
            "idempotency_key": req.idempotency_key,
        },
    ))
    await db.commit()

    available_after = agent.prefund_balance_usdc - agent.reserved_balance_usdc
    log.info(
        "withdraw_submitted",
        withdrawal_id=str(withdrawal_id),
        agent_id=str(agent_account_id),
        amount=str(req.amount_usdc),
        corridor=req.corridor_code,
        v1_batch_id=str(v1_batch_id),
    )

    return WithdrawResponse(
        withdrawal_id=withdrawal_id,
        agent_account_id=agent_account_id,
        amount_usdc=req.amount_usdc,
        corridor_code=req.corridor_code.upper(),
        delivery_mode=req.delivery_mode,
        status="submitted",
        v1_batch_id=v1_batch_id,
        available_balance_after=available_after,
    )
