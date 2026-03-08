# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build

RUN pip install --upgrade pip
COPY requirements/mcp_base.txt requirements/mcp_wallet_factory.txt requirements/
RUN pip install --no-cache-dir -r requirements/mcp_wallet_factory.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

RUN addgroup --system finogrid && adduser --system --ingroup finogrid appuser

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY mcp/wallet_factory/ mcp/wallet_factory/

USER appuser

ENV PORT=9004
EXPOSE 9004

CMD ["sh", "-c", "uvicorn mcp.wallet_factory.server:app --host 0.0.0.0 --port ${PORT}"]
