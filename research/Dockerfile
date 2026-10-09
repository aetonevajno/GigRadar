FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY catalog.py normalize.py probe.py store.py schema.sql ./

USER 65532:65532

CMD ["python", "catalog.py", "watch"]
