# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build

RUN pip install --upgrade pip
COPY requirements/base.txt requirements/ops_console.txt requirements/
RUN pip install --no-cache-dir -r requirements/ops_console.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

RUN addgroup --system finogrid && adduser --system --ingroup finogrid appuser

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY database/ database/
COPY services/ops_console/ services/ops_console/

USER appuser

ENV PORT=8200
EXPOSE 8200

CMD ["sh", "-c", "uvicorn services.ops_console.main:app --host 0.0.0.0 --port ${PORT}"]
