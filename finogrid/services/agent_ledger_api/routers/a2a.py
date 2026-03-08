"""
Google A2A (Agent-to-Agent) protocol integration — Finogrid Payment Rail.

Spec: https://google.github.io/A2A/specification/

This router makes the Agent Ledger API a first-class A2A agent:

  GET  /.well-known/agent.json           — Platform AgentCard (A2A discovery)
  POST /a2a                              — JSON-RPC 2.0 task handler
  GET  /v1/agent-accounts/{id}/a2a-card  — Per-agent AgentCard

Supported A2A methods:
  tasks/send    — Submit a payment task (maps to POST /v1/micropay gate sequence)
  tasks/get     — Poll task status
  tasks/cancel  — Cancel a pending task (no-op for sync tasks; logged)

Payment task input (DataPart):
  {
    "action":             "micropay",
    "payer_wallet_id":    "<uuid>",
    "payee_address":      "0x...",
    "amount_usdc":        "0.01",
    "idempotency_key":    "<caller-supplied unique string>",
    "payment_intent_id":  "<uuid | null>",   # required for closed-loop wallets
    "x402_payment_header": "<base64 | null>" # optional x402 payment signature
  }

Task state machine:
  submitted → working → completed   (happy path; synchronous)
  submitted → working → input-required  (mandate/KYA missing; caller must act)
  submitted → working → failed      (gate rejection; non-recoverable)

x402 ↔ A2A bridge:
  When a task fails with insufficient balance, the response includes
  an `x402_requirement` artifact so A2A-aware callers can top up and retry.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..dependencies import get_db, get_current_agent_account
from ..config import settings
from .....database.models.agent_ledger import (
    AgentAccount, AgentWallet, AgentKYA, PaymentIntent, MicroTransaction,
    AgentLedgerEntry, IntentStatus, LoopType, MicroTxStatus,
)
from .....database.models.mandate import Mandate, MandateStatus, MandateScope
from .....database.models.audit import AuditLog

log = structlog.get_logger()
router = APIRouter()

# ── A2A Agent Card (platform-level) ──────────────────────────────────────────

PLATFORM_AGENT_CARD = {
    "name": "Finogrid Payment Rail",
    "description": (
        "Stablecoin micropayment infrastructure for AI agents. "
        "Supports USDC on Base L2 with KYA compliance, "
        "closed/open-loop wallets, x402 payment protocol, and Mandate-based delegation."
    ),
    "url": "https://api.finogrid.io",
    "iconUrl": "https://finogrid.io/icon.png",
    "version": "1.0.0",
    "documentationUrl": "https://docs.finogrid.io/a2a",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "authentication": {
        "schemes": ["ApiKey"],
        "credentials": {
            "description": "Pass your Agent API key in the X-Agent-API-Key header",
            "header": "X-Agent-API-Key",
        },
    },
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "skills": [
        {
            "id": "micropay",
            "name": "USDC Micropayment",
            "description": (
                "Execute a USDC micropayment between agent wallets on Base L2. "
                "Requires an active Mandate and completed KYA (Know Your Agent). "
                "Supports open-loop (free within spending rules) and "
                "closed-loop (PaymentIntent required before payment) wallets."
            ),
            "tags": ["payment", "usdc", "stablecoin", "base-l2"],
            "examples": [
                'Pay 0.01 USDC to 0xAbCd... for compute service',
                'Transfer 0.50 USDC using closed-loop intent abc123',
            ],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
        {
            "id": "kya_status",
            "name": "KYA Status Check",
            "description": "Query the Know Your Agent (KYA) compliance status for this agent",
            "tags": ["compliance", "kya"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
        {
            "id": "balance",
            "name": "Balance Enquiry",
            "description": "Query available USDC prefund balance and reserved balance",
            "tags": ["balance", "usdc"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        },
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_result(task_id: str, state: str, message: str | None = None, artifacts: list | None = None) -> dict:
    return {
        "id": task_id,
        "status": {
            "state": state,
            "timestamp": _now_iso(),
            "message": {
                "role": "agent",
                "parts": [{"type": "text", "text": message or ""}],
            } if message else None,
        },
        "artifacts": artifacts or [],
    }


def _rpc_ok(rpc_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_err(rpc_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


# ── A2A task store (in-memory for MVP; replace with Redis/DB for HA) ──────────

_task_store: dict[str, dict] = {}


# ── Platform AgentCard endpoint ───────────────────────────────────────────────

@router.get("/.well-known/agent.json", include_in_schema=False)
async def platform_agent_card():
    """A2A discovery — returns the platform-level AgentCard."""
    return JSONResponse(content=PLATFORM_AGENT_CARD)


# ── Per-agent AgentCard ───────────────────────────────────────────────────────

@router.get("/v1/agent-accounts/{agent_id}/a2a-card")
async def agent_a2a_card(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Return an A2A AgentCard scoped to a specific registered AgentAccount.
    Caller does not need to be authenticated — this is a public discovery endpoint.
    The card reflects the agent's KYA level and active mandate scope.
    """
    result = await db.execute(
        select(AgentAccount).where(AgentAccount.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Resolve active mandate
    mandate_result = await db.execute(
        select(Mandate).where(
            Mandate.agent_account_id == agent.id,
            Mandate.status == MandateStatus.ACTIVE,
        ).limit(1)
    )
    mandate = mandate_result.scalar_one_or_none()

    card = {
        "name": agent.name,
        "description": f"Finogrid agent — {agent.kya_status} KYA, {agent.chain} chain",
        "url": f"https://api.finogrid.io/a2a/agents/{agent_id}",
        "version": "1.0.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "authentication": {
            "schemes": ["ApiKey"],
            "credentials": {"header": "X-Agent-API-Key"},
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "metadata": {
            "agent_id": str(agent.id),
            "kya_status": agent.kya_status,
            "chain": agent.chain,
            "mandate_scope": mandate.scope if mandate else None,
            "mandate_active": mandate is not None,
        },
        "skills": PLATFORM_AGENT_CARD["skills"],
    }
    return JSONResponse(content=card)


# ── JSON-RPC 2.0 A2A task handler ─────────────────────────────────────────────

@router.post("/a2a")
async def a2a_handler(
    request: Request,
    db: AsyncSession = Depends(get_db),
    agent: AgentAccount = Depends(get_current_agent_account),
):
    """
    A2A JSON-RPC 2.0 endpoint.
    Routes tasks/send, tasks/get, tasks/cancel to the appropriate handler.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=_rpc_err(None, -32700, "Parse error: request body must be valid JSON"),
            status_code=400,
        )

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})

    if body.get("jsonrpc") != "2.0":
        return JSONResponse(content=_rpc_err(rpc_id, -32600, "Invalid Request: jsonrpc must be '2.0'"))

    if method == "tasks/send":
        return JSONResponse(content=await _handle_tasks_send(rpc_id, params, db, agent))
    elif method == "tasks/get":
        return JSONResponse(content=_handle_tasks_get(rpc_id, params))
    elif method == "tasks/cancel":
        return JSONResponse(content=_handle_tasks_cancel(rpc_id, params, agent))
    else:
        return JSONResponse(content=_rpc_err(rpc_id, -32601, f"Method not found: {method}"))


# ── tasks/send ────────────────────────────────────────────────────────────────

async def _handle_tasks_send(
    rpc_id: Any,
    params: dict,
    db: AsyncSession,
    agent: AgentAccount,
) -> dict:
    task_id = params.get("id") or str(uuid.uuid4())
    message = params.get("message", {})
    parts = message.get("parts", [])

    # Extract DataPart payload
    payload = None
    for part in parts:
        if part.get("type") == "data":
            payload = part.get("data", {})
            break
        elif part.get("type") == "text":
            # Text-only task — not a payment task; return guidance
            return _rpc_ok(rpc_id, _task_result(
                task_id, "input-required",
                message=(
                    "Finogrid A2A accepts structured payment tasks. "
                    "Send a DataPart with: action, payer_wallet_id, payee_address, "
                    "amount_usdc, idempotency_key. See https://docs.finogrid.io/a2a"
                ),
            ))

    if not payload:
        return _rpc_err(rpc_id, -32602, "Invalid params: message must include a DataPart with payment payload")

    action = payload.get("action")

    if action == "micropay":
        return await _handle_micropay_task(rpc_id, task_id, payload, db, agent)
    elif action == "balance":
        return _handle_balance_task(rpc_id, task_id, agent)
    elif action == "kya_status":
        return await _handle_kya_status_task(rpc_id, task_id, db, agent)
    else:
        return _rpc_err(rpc_id, -32602, f"Unknown action '{action}'. Supported: micropay, balance, kya_status")


async def _handle_micropay_task(
    rpc_id: Any,
    task_id: str,
    payload: dict,
    db: AsyncSession,
    agent: AgentAccount,
) -> dict:
    """
    A2A micropayment task — runs the full 10-gate compliance sequence.
    Returns completed task with tx receipt artifact, or input-required/failed on gate block.
    """
    # ── Validate required fields ──────────────────────────────────────────────
    required = ["payer_wallet_id", "payee_address", "amount_usdc", "idempotency_key"]
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return _rpc_err(rpc_id, -32602, f"Missing required fields: {', '.join(missing)}")

    try:
        amount = Decimal(str(payload["amount_usdc"]))
    except Exception:
        return _rpc_err(rpc_id, -32602, "Invalid amount_usdc: must be a numeric string, e.g. '0.01'")

    if amount <= 0:
        return _rpc_err(rpc_id, -32602, "amount_usdc must be positive")

    # Mark task submitted
    _task_store[task_id] = {"state": "working", "started_at": _now_iso()}

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    # ── Gate 0: Mandate ───────────────────────────────────────────────────────
    mandate_result = await db.execute(
        select(Mandate).where(
            Mandate.agent_account_id == agent.id,
            Mandate.status == MandateStatus.ACTIVE,
            Mandate.scope.in_([MandateScope.PAYOUT, MandateScope.FULL]),
        ).order_by(Mandate.activated_at.desc()).limit(1)
    )
    active_mandate = mandate_result.scalar_one_or_none()
    if active_mandate is None:
        _task_store[task_id]["state"] = "input-required"
        return _rpc_ok(rpc_id, _task_result(
            task_id, "input-required",
            message=(
                "No active PAYOUT mandate found. "
                "An AgentOwner (Principal) must grant a Mandate via the Finogrid platform "
                "before outbound payments are permitted. "
                "Contact your platform administrator or visit https://docs.finogrid.io/mandates"
            ),
        ))

    if active_mandate.expires_at and active_mandate.expires_at < now:
        _task_store[task_id]["state"] = "input-required"
        return _rpc_ok(rpc_id, _task_result(
            task_id, "input-required",
            message="Agent mandate has expired. AgentOwner must renew the mandate.",
        ))

    # ── Gate 1: KYA ───────────────────────────────────────────────────────────
    if settings.kya_enabled and agent.kya_status not in ("basic", "enhanced"):
        _task_store[task_id]["state"] = "input-required"
        return _rpc_ok(rpc_id, _task_result(
            task_id, "input-required",
            message=(
                f"KYA status is '{agent.kya_status}'. Must be 'basic' or 'enhanced' to send payments. "
                "Submit KYA at POST /v1/agent-accounts/{id}/kya"
            ),
        ))

    # ── Gate 2: Idempotency ───────────────────────────────────────────────────
    existing_result = await db.execute(
        select(MicroTransaction).where(
            MicroTransaction.idempotency_key == payload["idempotency_key"]
        )
    )
    existing_tx = existing_result.scalar_one_or_none()
    if existing_tx:
        _task_store[task_id]["state"] = "completed"
        return _rpc_ok(rpc_id, _task_result(
            task_id, "completed",
            message="Idempotent replay — original transaction returned.",
            artifacts=[{
                "name": "transaction_receipt",
                "parts": [{"type": "data", "data": {
                    "transaction_id": str(existing_tx.id),
                    "idempotency_key": existing_tx.idempotency_key,
                    "status": existing_tx.status,
                    "amount_usdc": str(existing_tx.amount_usdc),
                    "on_chain_tx_hash": existing_tx.on_chain_tx_hash,
                }}],
            }],
        ))

    # ── Gate 3: Wallet ────────────────────────────────────────────────────────
    wallet_result = await db.execute(
        select(AgentWallet).where(
            AgentWallet.id == payload["payer_wallet_id"],
            AgentWallet.agent_account_id == agent.id,
        )
    )
    wallet = wallet_result.scalar_one_or_none()
    if wallet is None:
        _task_store[task_id]["state"] = "failed"
        return _rpc_ok(rpc_id, _task_result(task_id, "failed", message="Wallet not found or access denied"))
    if wallet.status != "active":
        _task_store[task_id]["state"] = "failed"
        return _rpc_ok(rpc_id, _task_result(task_id, "failed", message=f"Wallet status is '{wallet.status}', not active"))

    # ── Gate 4: Closed-loop intent ────────────────────────────────────────────
    intent = None
    if wallet.loop_type == LoopType.CLOSED:
        if not payload.get("payment_intent_id"):
            _task_store[task_id]["state"] = "input-required"
            return _rpc_ok(rpc_id, _task_result(
                task_id, "input-required",
                message=(
                    "Wallet is closed-loop. Create a PaymentIntent first at "
                    "POST /v1/payment-intents, then retry with payment_intent_id."
                ),
            ))
        intent_result = await db.execute(
            select(PaymentIntent).where(PaymentIntent.id == payload["payment_intent_id"])
        )
        intent = intent_result.scalar_one_or_none()
        if intent is None or intent.payer_wallet_id != wallet.id:
            _task_store[task_id]["state"] = "failed"
            return _rpc_ok(rpc_id, _task_result(task_id, "failed", message="PaymentIntent not found or does not belong to this wallet"))
        if intent.status != IntentStatus.RESERVED:
            _task_store[task_id]["state"] = "failed"
            return _rpc_ok(rpc_id, _task_result(task_id, "failed", message=f"PaymentIntent status is '{intent.status}'. Must be 'reserved'."))

    # ── Gate 5: Per-tx cap ────────────────────────────────────────────────────
    if amount > Decimal(str(wallet.max_per_tx_usdc)):
        _task_store[task_id]["state"] = "failed"
        return _rpc_ok(rpc_id, _task_result(
            task_id, "failed",
            message=f"Amount {amount} USDC exceeds per-tx cap {wallet.max_per_tx_usdc} USDC",
        ))

    # ── Gate 6: Available balance ─────────────────────────────────────────────
    available = Decimal(str(agent.prefund_balance_usdc)) - Decimal(str(agent.reserved_balance_usdc))
    if wallet.loop_type == LoopType.OPEN and amount > available:
        _task_store[task_id]["state"] = "input-required"
        # x402 ↔ A2A bridge: include top-up instruction as structured artifact
        x402_requirement = {
            "asset": "USDC",
            "network": agent.chain,
            "amount_required": str(amount - available),
            "top_up_endpoint": f"POST /v1/agent-accounts/{agent.id}/topup",
            "description": (
                f"Insufficient balance. Available: {float(available):.4f} USDC, "
                f"Required: {float(amount):.4f} USDC. "
                "Top up your agent account and retry."
            ),
        }
        return _rpc_ok(rpc_id, _task_result(
            task_id, "input-required",
            message=f"Insufficient balance. Available: {float(available):.4f} USDC",
            artifacts=[{
                "name": "x402_payment_requirement",
                "parts": [{"type": "data", "data": x402_requirement}],
            }],
        ))

    # ── Gate 7: Settle off-chain ──────────────────────────────────────────────
    settled_at = now
    micro_tx = MicroTransaction(
        idempotency_key=payload["idempotency_key"],
        payer_wallet_id=wallet.id,
        payee_address=payload["payee_address"].lower(),
        amount_usdc=amount,
        chain=agent.chain,
        loop_type=wallet.loop_type,
        payment_intent_id=intent.id if intent else None,
        mandate_id=active_mandate.id,
        x402_payment_header=payload.get("x402_payment_header"),
        status=MicroTxStatus.SETTLED_OFFCHAIN,
        settled_at=settled_at,
        metadata_={"source": "a2a", "task_id": task_id},
    )
    db.add(micro_tx)
    await db.flush()

    # Update balances
    if wallet.loop_type == LoopType.CLOSED and intent:
        agent.reserved_balance_usdc = Decimal(str(agent.reserved_balance_usdc)) - amount
        intent.status = IntentStatus.CONSUMED
        intent.consumed_micro_tx_id = micro_tx.id

    agent.prefund_balance_usdc = Decimal(str(agent.prefund_balance_usdc)) - amount
    wallet.use_count = (wallet.use_count or 0) + 1

    balance_after = Decimal(str(agent.prefund_balance_usdc))
    reserved_after = Decimal(str(agent.reserved_balance_usdc))

    ledger_entry = AgentLedgerEntry(
        agent_account_id=agent.id,
        entry_type="debit",
        amount_usdc=amount,
        balance_after=balance_after,
        reserved_balance_after=reserved_after,
        micro_tx_id=micro_tx.id,
        payment_intent_id=intent.id if intent else None,
        description=f"A2A micropay task {task_id[:8]} → {payload['payee_address'][:10]}",
    )
    db.add(ledger_entry)

    audit = AuditLog(
        actor_type="agent",
        actor_id=str(agent.id),
        action="a2a_micropay_settled",
        resource_type="micro_transaction",
        resource_id=str(micro_tx.id),
        after_state={
            "task_id": task_id,
            "idempotency_key": payload["idempotency_key"],
            "payee_address": payload["payee_address"],
            "amount_usdc": str(amount),
            "loop_type": str(wallet.loop_type),
            "mandate_id": str(active_mandate.id),
            "balance_after": str(balance_after),
        },
    )
    db.add(audit)
    await db.commit()

    _task_store[task_id] = {
        "state": "completed",
        "transaction_id": str(micro_tx.id),
        "completed_at": _now_iso(),
    }

    log.info("a2a_micropay_settled", task_id=task_id, tx_id=str(micro_tx.id), amount=str(amount))

    return _rpc_ok(rpc_id, _task_result(
        task_id, "completed",
        message=f"Payment of {float(amount):.4f} USDC settled off-chain. Awaiting on-chain sweep.",
        artifacts=[{
            "name": "transaction_receipt",
            "parts": [{"type": "data", "data": {
                "transaction_id": str(micro_tx.id),
                "idempotency_key": payload["idempotency_key"],
                "status": "settled_offchain",
                "amount_usdc": str(amount),
                "payee_address": payload["payee_address"],
                "loop_type": str(wallet.loop_type),
                "mandate_id": str(active_mandate.id),
                "settled_at": settled_at.isoformat(),
                "on_chain_tx_hash": None,
                "chain": agent.chain,
                "balance_after_usdc": str(balance_after),
            }}],
        }],
    ))


def _handle_balance_task(rpc_id: Any, task_id: str, agent: AgentAccount) -> dict:
    available = float(agent.prefund_balance_usdc) - float(agent.reserved_balance_usdc)
    return _rpc_ok(rpc_id, _task_result(
        task_id, "completed",
        artifacts=[{
            "name": "balance",
            "parts": [{"type": "data", "data": {
                "prefund_balance_usdc": str(agent.prefund_balance_usdc),
                "reserved_balance_usdc": str(agent.reserved_balance_usdc),
                "available_usdc": str(max(available, 0)),
                "chain": agent.chain,
                "kya_status": agent.kya_status,
            }}],
        }],
    ))


async def _handle_kya_status_task(rpc_id: Any, task_id: str, db: AsyncSession, agent: AgentAccount) -> dict:
    kya_result = await db.execute(
        select(AgentKYA).where(AgentKYA.agent_account_id == agent.id)
    )
    kya = kya_result.scalar_one_or_none()
    return _rpc_ok(rpc_id, _task_result(
        task_id, "completed",
        artifacts=[{
            "name": "kya_status",
            "parts": [{"type": "data", "data": {
                "agent_id": str(agent.id),
                "kya_status": agent.kya_status,
                "validator_name": kya.validator_name if kya else None,
                "validated_at": kya.validated_at.isoformat() if kya and kya.validated_at else None,
                "validator_expires_at": kya.validator_expires_at.isoformat() if kya and kya.validator_expires_at else None,
            }}],
        }],
    ))


# ── tasks/get ─────────────────────────────────────────────────────────────────

def _handle_tasks_get(rpc_id: Any, params: dict) -> dict:
    task_id = params.get("id")
    if not task_id:
        return _rpc_err(rpc_id, -32602, "Missing required param: id")
    task = _task_store.get(task_id)
    if task is None:
        return _rpc_err(rpc_id, -32001, f"Task {task_id} not found")
    return _rpc_ok(rpc_id, _task_result(task_id, task["state"]))


# ── tasks/cancel ──────────────────────────────────────────────────────────────

def _handle_tasks_cancel(rpc_id: Any, params: dict, agent: AgentAccount) -> dict:
    task_id = params.get("id")
    if not task_id:
        return _rpc_err(rpc_id, -32602, "Missing required param: id")
    task = _task_store.get(task_id)
    if task is None:
        return _rpc_err(rpc_id, -32001, f"Task {task_id} not found")
    if task["state"] in ("completed", "failed", "canceled"):
        return _rpc_err(rpc_id, -32002, f"Task is already in terminal state: {task['state']}")
    _task_store[task_id]["state"] = "canceled"
    log.info("a2a_task_canceled", task_id=task_id, agent_id=str(agent.id))
    return _rpc_ok(rpc_id, _task_result(task_id, "canceled", message="Task canceled by caller"))
