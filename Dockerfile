# syntax=docker/dockerfile:1

# ---- build: dependencies, the package, and the embedding model ----
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first, in their own layer: they change far less often than the code.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# Bake the embedding model in, so a running container never downloads
# anything: no Hugging Face outage, rate limit or network policy can stop it
# starting, and every replica embeds with byte-identical weights.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}', cache_dir='/opt/fastembed')" \
 && chown -R 65532:65532 /opt/fastembed

# ---- runtime ----
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/fastembed /opt/fastembed
COPY runbooks ./runbooks
COPY evals/cases ./evals/cases
COPY evals/thresholds.yaml ./evals/
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    HF_HUB_OFFLINE=1 \
    HF_HOME=/tmp/hf \
    RUNBOOKS_DIR=/app/runbooks \
    EVALS_DIR=/app/evals \
    EVALS_OUT=/tmp/triage-evals
USER 65532:65532
EXPOSE 8086
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8086/readyz', timeout=2)"]
ENTRYPOINT ["python", "-m", "triage"]
