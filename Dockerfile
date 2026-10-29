FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -m chainlead && mkdir /data && chown chainlead /data
USER chainlead
ENV CHAINLEAD_DB=/data/chainlead.db PORT=8787
EXPOSE 8787
# Public bind requires CHAINLEAD_API_KEYS (the server refuses otherwise)
CMD ["sh", "-c", "chainlead serve --host 0.0.0.0 --port ${PORT}"]
