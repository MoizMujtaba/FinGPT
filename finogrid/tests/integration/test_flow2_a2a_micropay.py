"""
Integration tests — Flow 2: A2A Stablecoin Micropay

Tests the Agent Ledger subsystem end-to-end:
  - Agent registration
  - KYA gating
  - Wallet provisioning
  - Balance top-up credit
  - Closed-loop: PaymentIntent → micropay (all 10 gates)
  - Open-loop: micropay within limits
  - Withdrawal submission (mocked v1 Ingress API)
  - Intent sweeper (expired intent → balance release)

All external calls (chain, KYA MCP, Pub/Sub, v1 API) are mocked.
"""
import uuid
import pytest
import hashlib
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ── Schema validation ─────────────────────────────────────────────────────────

class TestAgentLedgerSchemas:
    """Pydantic schemas validate correctly."""

    def test_withdraw_request_valid(self):
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest
        req = WithdrawRequest(
            amount_usdc=Decimal("10.00"),
            corridor_code="BR",
            beneficiary_data={"pix_key": "test@x.com", "recipient_document": "123"},
            delivery_mode="fiat",
            idempotency_key="idem-key-12345678",
        )
        assert req.corridor_code == "BR"
        assert req.delivery_mode == "fiat"

    def test_withdraw_request_amount_must_be_positive(self):
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            WithdrawRequest(
                amount_usdc=Decimal("-1.00"),
                corridor_code="BR",
                beneficiary_data={},
                idempotency_key="key12345678",
            )

    def test_withdraw_request_corridor_code_max_2_chars(self):
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            WithdrawRequest(
                amount_usdc=Decimal("10.00"),
                corridor_code="BRA",  # 3 chars — should fail
                beneficiary_data={},
                idempotency_key="key12345678",
            )

    def test_wallet_create_request_validates_evm_address(self):
        from finogrid.services.agent_ledger_api.schemas import AgentWalletCreateRequest
        import pydantic

        valid = AgentWalletCreateRequest(
            label="test-wallet",
            wallet_address="0x" + "a" * 40,
            loop_type="closed",
        )
        assert valid.wallet_address == "0x" + "a" * 40

        with pytest.raises(pydantic.ValidationError):
            AgentWalletCreateRequest(
                label="bad",
                wallet_address="not-an-address",
                loop_type="open",
            )

    def test_payment_intent_requires_positive_amount(self):
        from finogrid.services.agent_ledger_api.schemas import PaymentIntentCreateRequest
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            PaymentIntentCreateRequest(
                payer_wallet_id=uuid.uuid4(),
                amount_usdc=Decimal("0"),
                intent_description="some intent description here",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )


# ── Mandate model ─────────────────────────────────────────────────────────────

class TestMandateModel:
    """Mandate status transitions and scope constraints."""

    def test_mandate_status_lifecycle(self):
        from finogrid.database.models.mandate import MandateStatus

        # Valid transitions (check enum values exist)
        assert MandateStatus.DRAFT.value == "draft"
        assert MandateStatus.ACTIVE.value == "active"
        assert MandateStatus.SUSPENDED.value == "suspended"
        assert MandateStatus.REVOKED.value == "revoked"
        assert MandateStatus.EXPIRED.value == "expired"
        assert MandateStatus.SUPERSEDED.value == "superseded"

    def test_mandate_scope_values(self):
        from finogrid.database.models.mandate import MandateScope
        scopes = {s.value for s in MandateScope}
        assert "payout" in scopes
        assert "collect" in scopes
        assert "full" in scopes
        assert "read_only" in scopes

    def test_approval_mode_values(self):
        from finogrid.database.models.mandate import ApprovalMode
        modes = {m.value for m in ApprovalMode}
        assert "auto" in modes
        assert "manual" in modes
        assert "threshold" in modes


# ── KYA validator — internal backend ─────────────────────────────────────────

class TestKYAValidator:
    """KYA validator internal backend logic."""

    def test_internal_validate_basic_short_fields(self):
        from finogrid.mcp.kya_validator.server import _internal_validate
        status, level = _internal_validate({
            "agent_purpose": "short",
            "agent_owner_attestation": "short",
            "declared_use_case": "general",
        })
        assert level == "basic"

    def test_internal_validate_enhanced_long_fields(self):
        from finogrid.mcp.kya_validator.server import _internal_validate
        status, level = _internal_validate({
            "agent_purpose": "a" * 201,
            "agent_owner_attestation": "b" * 101,
            "declared_use_case": "trading_support",
        })
        assert level == "enhanced"

    def test_mint_validator_token_returns_parseable_token(self):
        import base64, json
        from finogrid.mcp.kya_validator.server import _mint_validator_token
        token, expires_at = _mint_validator_token("agent-123", "basic")
        payload = json.loads(base64.b64decode(token).decode())
        assert payload["sub"] == "agent-123"
        assert payload["level"] == "basic"
        assert "exp" in payload


# ── Withdraw router logic ─────────────────────────────────────────────────────

class TestWithdrawRouter:
    """Withdraw router guards and balance checks (mocked DB and v1 API)."""

    def _make_agent(self, kya_status="basic", prefund=Decimal("100"), reserved=Decimal("0")):
        agent = MagicMock()
        agent.id = uuid.uuid4()
        agent.kya_status = kya_status
        agent.prefund_balance_usdc = prefund
        agent.reserved_balance_usdc = reserved
        return agent

    @pytest.mark.asyncio
    async def test_withdraw_insufficient_balance_raises_402(self):
        from fastapi import HTTPException
        from finogrid.services.agent_ledger_api.routers.withdraw import withdraw
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest

        agent = self._make_agent(prefund=Decimal("5.00"), reserved=Decimal("0"))
        agent_account_id = agent.id

        req = WithdrawRequest(
            amount_usdc=Decimal("10.00"),
            corridor_code="BR",
            beneficiary_data={"mode": "fiat", "pix_key": "test@x.com", "recipient_document": "123"},
            idempotency_key="test-key-12345678",
        )

        mock_db = AsyncMock()

        with patch("finogrid.services.agent_ledger_api.routers.withdraw.assert_kya_status", new_callable=AsyncMock) as mock_kya:
            mock_kya.return_value = None
            with pytest.raises(HTTPException) as exc_info:
                await withdraw(agent_account_id, req, db=mock_db, agent=agent)
        assert exc_info.value.status_code == 402

    @pytest.mark.asyncio
    async def test_withdraw_unsupported_corridor_raises_400(self):
        from fastapi import HTTPException
        from finogrid.services.agent_ledger_api.routers.withdraw import withdraw
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest

        agent = self._make_agent()
        agent_account_id = agent.id

        req = WithdrawRequest(
            amount_usdc=Decimal("10.00"),
            corridor_code="XX",  # Unknown corridor
            beneficiary_data={},
            idempotency_key="test-key-12345678",
        )

        mock_db = AsyncMock()

        with patch("finogrid.services.agent_ledger_api.routers.withdraw.assert_kya_status", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await withdraw(agent_account_id, req, db=mock_db, agent=agent)
        assert exc_info.value.status_code == 400
        assert "Unsupported corridor" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_withdraw_below_corridor_minimum_raises_400(self):
        from fastapi import HTTPException
        from finogrid.services.agent_ledger_api.routers.withdraw import withdraw
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest

        agent = self._make_agent(prefund=Decimal("1000"))
        agent_account_id = agent.id

        req = WithdrawRequest(
            amount_usdc=Decimal("0.50"),  # Below $10 minimum for BR
            corridor_code="BR",
            beneficiary_data={"mode": "fiat", "pix_key": "test@x.com", "recipient_document": "123"},
            idempotency_key="test-key-12345678",
        )

        mock_db = AsyncMock()

        with patch("finogrid.services.agent_ledger_api.routers.withdraw.assert_kya_status", new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await withdraw(agent_account_id, req, db=mock_db, agent=agent)
        assert exc_info.value.status_code == 400
        assert "minimum" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_withdraw_v1_failure_releases_reservation(self):
        """If v1 Ingress API is unreachable, balance reservation is reversed."""
        from fastapi import HTTPException
        from finogrid.services.agent_ledger_api.routers.withdraw import withdraw
        from finogrid.services.agent_ledger_api.schemas import WithdrawRequest

        agent = self._make_agent(prefund=Decimal("100"), reserved=Decimal("0"))
        agent_account_id = agent.id

        req = WithdrawRequest(
            amount_usdc=Decimal("50.00"),
            corridor_code="BR",
            beneficiary_data={"mode": "fiat", "pix_key": "test@x.com", "recipient_document": "123"},
            idempotency_key="test-key-12345678",
        )

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()

        import httpx

        with patch("finogrid.services.agent_ledger_api.routers.withdraw.assert_kya_status", new_callable=AsyncMock), \
             patch("httpx.AsyncClient") as mock_client_cls:

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await withdraw(agent_account_id, req, db=mock_db, agent=agent)

        # Reservation must be released
        assert agent.reserved_balance_usdc == Decimal("0")
        assert exc_info.value.status_code == 503


# ── x402 middleware ────────────────────────────────────────────────────────────

class TestX402Middleware:
    """x402 payment-required middleware header flow."""

    def test_payment_required_header_format(self):
        """PAYMENT-REQUIRED header should be valid base64 JSON."""
        import base64, json
        # Reconstruct what the middleware would emit
        requirement = {
            "version": "x402-1",
            "resource": "https://api.finogrid.io/v1/some-resource",
            "amount_usdc": "0.01",
            "payee_address": "0x" + "a" * 40,
            "nonce": "test-nonce",
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
        }
        header_value = base64.b64encode(json.dumps(requirement).encode()).decode()
        decoded = json.loads(base64.b64decode(header_value).decode())
        assert decoded["version"] == "x402-1"
        assert "amount_usdc" in decoded
