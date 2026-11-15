FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prana ./prana

EXPOSE 9000

CMD ["uvicorn", "prana.main:app", "--host", "0.0.0.0", "--port", "9000"]
