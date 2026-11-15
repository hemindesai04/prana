FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 prana \
    && useradd -u 1000 -g 1000 -m -s /bin/sh prana

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prana ./prana
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN mkdir -p /backups && chown -R 1000:1000 /app /backups

USER 1000:1000

EXPOSE 9000

CMD ["uvicorn", "prana.main:app", "--host", "0.0.0.0", "--port", "9000", "--no-access-log"]
