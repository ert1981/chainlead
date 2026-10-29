# ChainLead: paid x402 tool endpoint for agents (OpenSea Tools / ERC-8257). Fits Render's free web service.
# Standard library only: no third-party runtime dependencies.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -m -u 10001 chainlead && mkdir -p /data && chown chainlead /data
USER chainlead
# Public, non-secret defaults. Secrets (GITHUB_TOKEN, EXPLORER_API_KEY) come from the host environment only.
ENV CHAINLEAD_DB=/data/chainlead.db \
    CHAINLEAD_RPC_8453=https://base-rpc.publicnode.com \
    PORT=8000
EXPOSE 8000
# Render injects PORT. The REST API with tenant keys is `chainlead serve` (see compose.yaml).
CMD ["sh", "-c", "exec chainlead tool-serve --host 0.0.0.0 --port ${PORT}"]
