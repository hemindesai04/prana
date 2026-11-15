FROM rust:1-bookworm AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends libdbus-1-dev pkg-config git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 https://github.com/Th0rgal/open_oura.git . \
    && cargo build --release --bin oura

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends libdbus-1-3 bluez ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/target/release/oura /usr/local/bin/oura

WORKDIR /opt/oura-ble
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py ./

CMD ["python", "-u", "sync.py"]
