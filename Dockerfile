FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ERP_PROXY_COOKIE_JAR=/data/platform_admin_cookies.txt \
    UNCLAIMED_ORDERS_CRON_ENABLED=1 \
    UNCLAIMED_ORDERS_CRON_TIME=09:00 \
    UNCLAIMED_ORDERS_CRON_TZ=Europe/Moscow

WORKDIR /app

RUN groupadd --gid 65532 app \
    && useradd --uid 65532 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /app /data

COPY pyproject.toml uv.lock README.md ./
COPY unclaimed_orders_service ./unclaimed_orders_service

RUN pip install --no-cache-dir .

USER 65532:65532
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK NONE

CMD ["uvicorn", "unclaimed_orders_service.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
