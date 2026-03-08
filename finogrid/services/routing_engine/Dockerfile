# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build

RUN pip install --upgrade pip
COPY requirements/base.txt requirements/routing_engine.txt requirements/
RUN pip install --no-cache-dir -r requirements/routing_engine.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

RUN addgroup --system finogrid && adduser --system --ingroup finogrid appuser

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY database/ database/
COPY corridors/ corridors/
COPY services/routing_engine/ services/routing_engine/

USER appuser

# Routing engine is a Pub/Sub worker — no HTTP port needed
CMD ["python", "-m", "services.routing_engine.worker"]
