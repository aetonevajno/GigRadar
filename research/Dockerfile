FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY research/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY research/catalog.py research/config.py research/normalize.py research/probe.py research/store.py research/app_store.py research/curation.py research/outbox.py research/notify.py research/schema.sql ./
COPY research/migrations ./migrations
COPY app/telemetry.py app/__init__.py ./app/
COPY monitoring/exporter.py ./exporter.py

USER 65532:65532

CMD ["python", "catalog.py", "watch"]
