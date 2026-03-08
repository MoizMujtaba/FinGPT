# ── Builder ────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build

RUN pip install --upgrade pip
COPY requirements/base.txt requirements/ingress_api.txt requirements/
RUN pip install --no-cache-dir -r requirements/ingress_api.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

# Non-root user
RUN addgroup --system finogrid && adduser --system --ingroup finogrid appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code (shared modules + this service)
COPY database/ database/
COPY services/ingress_api/ services/ingress_api/

USER appuser

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn services.ingress_api.main:app --host 0.0.0.0 --port ${PORT}"]
