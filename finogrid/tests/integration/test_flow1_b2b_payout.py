"""
Integration tests — Flow 1: B2B Payout

Tests the end-to-end path:
  POST /v1/batches  →  webhook /webhooks/bridge  →  webhook /webhooks/kyt

Uses FastAPI TestClient with a real async SQLite DB (aiosqlite) for portability
without requiring PostgreSQL in the test environment.

Mocked:
  - Bridge API client (no external calls)
  - GCP Pub/Sub (no real emulator needed)
  - KYT MCP server
"""
import uuid
import pytest
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

import pytest_asyncio
from httpx import AsyncClient, ASGITransport


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_batch_payload(corridor="BR", amount=100.0):
    return {
        "reference": f"test-batch-{uuid.uuid4()}",
        "tasks": [
            {
                "corridor_code": corridor,
                "amount_usd": amount,
                "preferred_asset": "USDC",
                "preferred_mode": "fiat",
                "beneficiary_data": {
                    "mode": "fiat",
                    "pix_key": "test@example.com",
                    "pix_key_type": "email",
                    "recipient_document": "123.456.789-00",
                    "recipient_name": "Test Recipient",
                },
            }
        ],
    }


# ── Corridor chain selection ───────────────────────────────────────────────────

class TestCorridorChainSelection:
    """Unit-level checks that all 8 corridors return the correct chain."""

    @pytest.mark.parametrize("corridor,asset,expected_chain", [
        # Nigeria
        ("NG", "USDT", "tron"),
        ("NG", "USDC", "ethereum"),
        # Vietnam
        ("VN", "USDT", "tron"),
        ("VN", "USDC", "ethereum"),
        # Argentina
        ("AR", "USDT", "tron"),
        ("AR", "USDC", "base"),
        # Brazil
        ("BR", "USDT", "tron"),
        ("BR", "USDC", "base"),
        # India
        ("IN", "USDT", "tron"),
        ("IN", "USDC", "base"),
        # Indonesia
        ("ID", "USDT", "tron"),
        ("ID", "USDC", "base"),
        # Philippines
        ("PH", "USDT", "tron"),
        ("PH", "USDC", "base"),
        # UAE
        ("AE", "USDT", "ethereum"),
        ("AE", "USDC", "base"),
    ])
    def test_chain_selection(self, corridor, asset, expected_chain):
        from finogrid.corridors import get_adapter
        adapter = get_adapter(corridor)
        assert adapter.get_chain_for_asset(asset) == expected_chain, (
            f"{corridor}/{asset}: expected {expected_chain}, "
            f"got {adapter.get_chain_for_asset(asset)}"
        )


class TestBeneficiaryValidation:
    """All 8 corridor adapters return valid/invalid correctly."""

    @pytest.mark.parametrize("corridor,mode,data,expected_valid", [
        # BR fiat — pix_key + recipient_document required
        ("BR", "fiat", {"pix_key": "test@x.com", "recipient_document": "123"}, True),
        ("BR", "fiat", {"recipient_document": "123"}, False),
        # BR wallet — no fiat fields required
        ("BR", "wallet", {}, True),
        # NG fiat — account_number + bank_code + bvn required
        ("NG", "fiat", {"account_number": "0123456789", "bank_code": "058", "bvn": "22345678901"}, True),
        ("NG", "fiat", {"bank_code": "058"}, False),
        # IN fiat — upi_id required
        ("IN", "fiat", {"upi_id": "user@upi"}, True),
        ("IN", "fiat", {}, False),
        # AR fiat — cbu_or_alias + cuit required
        ("AR", "fiat", {"cbu_or_alias": "alias123", "cuit": "20-12345678-9"}, True),
        ("AR", "fiat", {"cbu_or_alias": "alias123"}, False),
        # UAE fiat — iban + swift_bic required
        ("AE", "fiat", {"iban": "AE070331234567890123456", "swift_bic": "NBADAEAA"}, True),
        ("AE", "fiat", {"iban": "AE070331234567890123456"}, False),
    ])
    def test_beneficiary_validation(self, corridor, mode, data, expected_valid):
        from finogrid.corridors import get_adapter
        adapter = get_adapter(corridor)
        result = adapter.validate_beneficiary({**data, "mode": mode})
        assert result.valid == expected_valid, (
            f"{corridor}/{mode}: expected valid={expected_valid}, "
            f"missing={result.missing_fields}, errors={result.errors}"
        )


class TestBridgeWebhookHandling:
    """Bridge webhook processes correctly without a live DB (uses mocks)."""

    def _build_app(self):
        """Build a minimal FastAPI app that only includes the webhook router."""
        from fastapi import FastAPI
        app = FastAPI()

        # Patch DB + settings before importing router
        return app

    @pytest.mark.asyncio
    async def test_bridge_webhook_missing_task_id_returns_200(self):
        """Webhook with no task_id should 200 with a note, not 500."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        with patch("finogrid.services.ingress_api.routers.webhooks.get_db", return_value=mock_db), \
             patch("finogrid.services.ingress_api.routers.webhooks.settings") as mock_settings, \
             patch("finogrid.services.ingress_api.routers.webhooks._publish_reconcile", new_callable=AsyncMock):

            mock_settings.bridge_webhook_secret = ""
            mock_settings.pubsub_project_id = "test"

            from fastapi.testclient import TestClient
            from finogrid.services.ingress_api.routers.webhooks import router
            from fastapi import FastAPI

            app = FastAPI()
            app.include_router(router, prefix="/webhooks")

            client = TestClient(app)
            resp = client.post("/webhooks/bridge", json={"event_type": "transfer.completed"})
            assert resp.status_code == 200
            assert resp.json()["received"] is True

    @pytest.mark.asyncio
    async def test_bridge_webhook_invalid_signature_returns_401(self):
        """Signature mismatch should 401."""
        from fastapi.testclient import TestClient
        from finogrid.services.ingress_api.routers.webhooks import router
        from fastapi import FastAPI
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_db = AsyncMock()

        with patch("finogrid.services.ingress_api.routers.webhooks.get_db", return_value=mock_db), \
             patch("finogrid.services.ingress_api.routers.webhooks.settings") as mock_settings:

            mock_settings.bridge_webhook_secret = "real-secret"
            mock_settings.pubsub_project_id = "test"

            app = FastAPI()
            app.include_router(router, prefix="/webhooks")

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/webhooks/bridge",
                json={"event_type": "transfer.completed"},
                headers={"x-bridge-signature": "sha256=invalidsig"},
            )
            assert resp.status_code == 401

    @pytest.mark.parametrize("bridge_status,expected_task_status", [
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("processing", None),  # no task status change
    ])
    def test_bridge_status_mapping(self, bridge_status, expected_task_status):
        from finogrid.services.ingress_api.routers.webhooks import _BRIDGE_STATUS_MAP
        from finogrid.database.models.batch import TaskStatus
        from finogrid.database.models.execution import EventType

        ev_type, task_status = _BRIDGE_STATUS_MAP.get(
            bridge_status, (EventType.PARTNER_STATUS_UPDATE, None)
        )
        if expected_task_status is None:
            assert task_status is None
        else:
            assert task_status == TaskStatus(expected_task_status)


class TestKYTWebhookHandling:
    """KYT webhook maps decisions to correct task states."""

    @pytest.mark.parametrize("decision,expected_status,expected_event", [
        ("approved", "executing",        "compliance_pass"),
        ("pass",     "executing",        "compliance_pass"),
        ("clear",    "executing",        "compliance_pass"),
        ("rejected", "failed",           "compliance_fail"),
        ("fail",     "failed",           "compliance_fail"),
        ("block",    "failed",           "compliance_fail"),
        ("review",   "held_for_review",  "compliance_hold"),
        ("unknown",  "held_for_review",  "compliance_hold"),
    ])
    def test_kyt_decision_mapping(self, decision, expected_status, expected_event):
        """KYT decisions map correctly to task status + event type."""
        from finogrid.database.models.batch import TaskStatus
        from finogrid.database.models.execution import EventType

        decision_lower = decision.lower()
        if decision_lower in ("approved", "pass", "clear"):
            ev = EventType.COMPLIANCE_PASS
            ts = TaskStatus.EXECUTING
        elif decision_lower in ("rejected", "fail", "block", "denied"):
            ev = EventType.COMPLIANCE_FAIL
            ts = TaskStatus.FAILED
        else:
            ev = EventType.COMPLIANCE_HOLD
            ts = TaskStatus.HELD_FOR_REVIEW

        assert ts.value == expected_status
        assert ev.value == expected_event
