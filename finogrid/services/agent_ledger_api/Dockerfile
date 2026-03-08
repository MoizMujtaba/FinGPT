# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build

RUN pip install --upgrade pip
COPY requirements/base.txt requirements/agent_ledger.txt requirements/
RUN pip install --no-cache-dir -r requirements/agent_ledger.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

RUN addgroup --system finogrid && adduser --system --ingroup finogrid appuser

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY database/ database/
COPY services/agent_ledger_api/ services/agent_ledger_api/

USER appuser

ENV PORT=8100
EXPOSE 8100

CMD ["sh", "-c", "uvicorn services.agent_ledger_api.main:app --host 0.0.0.0 --port ${PORT}"]
