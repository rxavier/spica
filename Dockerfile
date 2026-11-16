# The picker runs on Alpine (numpy and the pure-Python stack have musl wheels); the
# analyser needs glibc, because onnxruntime and essentia publish manylinux wheels only.
# Build the analyser with BASE=python:3.14-slim SPICA_EXTRAS=analysis.
ARG BASE=python:3.14-alpine
FROM ${BASE} AS builder

# The picker's half is numpy (to read vectors); the analyser's half adds
# onnxruntime and essentia. Build the worker with SPICA_EXTRAS=analysis.
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
ARG SPICA_EXTRAS=vectors
RUN uv sync --frozen --no-dev --no-install-project --extra "$SPICA_EXTRAS"

# The runtime image keeps the venv and the code, not uv or its wheel cache.
FROM ${BASE}

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv /app/.venv
COPY spica ./spica

EXPOSE 8030
CMD ["python", "-m", "spica.app"]
