FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl time ca-certificates bash procps \
    && rm -rf /var/lib/apt/lists/*

# Keep the container alive so you can exec in via Zeabur's terminal
# and run the install + test manually.
CMD ["sleep", "infinity"]
