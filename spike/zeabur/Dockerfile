FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl time ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (standalone, no Node required)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Installer drops binaries here; surface them on PATH
ENV PATH="/root/.local/bin:/root/.claude/local:${PATH}"

COPY spike.sh /spike.sh
RUN chmod +x /spike.sh

CMD ["/spike.sh"]
