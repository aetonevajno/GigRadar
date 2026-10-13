FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /srv/gigradar
COPY app/requirements.txt ./requirements.txt
COPY requirements.constraints.txt ./requirements.constraints.txt
RUN pip install --no-cache-dir -c requirements.constraints.txt -r requirements.txt
COPY app ./app
COPY research/schema.sql ./research/schema.sql
COPY research/migrations ./research/migrations

USER 65532:65532
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
