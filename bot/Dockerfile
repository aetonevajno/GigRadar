FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /srv/gigradar
COPY pyproject.toml ./pyproject.toml
COPY app ./app
COPY bot ./bot
COPY locales ./locales
COPY requirements.constraints.txt ./requirements.constraints.txt
RUN pip install --no-cache-dir -c requirements.constraints.txt -r bot/requirements.txt

USER 65532:65532
CMD ["python", "-m", "bot"]
