# The picker and worker run on Alpine (numpy and the pure-Python stack have musl
# wheels); the analyser needs glibc, because onnxruntime and essentia publish
# manylinux wheels only. Build the analyser with BASE=python:3.14-slim
# SPICA_EXTRAS=analysis.
ARG BASE=python:3.14-alpine
FROM ${BASE} AS builder

# The picker's half is numpy (to read vectors); the analyser's half adds
# onnxruntime and essentia. `vectors` is for the lean, worker and init images.
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
ARG SPICA_EXTRAS=vectors
RUN uv sync --frozen --no-dev --no-install-project --extra "$SPICA_EXTRAS"

# The runtime image keeps the venv and the code, not uv or its wheel cache.
FROM ${BASE} AS runtime

# The analyser's essentia wheel links libatomic; the Debian slim base does not ship it.
# The picker (Alpine) passes nothing here.
ARG SPICA_RUNTIME_DEPS=""
RUN if [ -n "$SPICA_RUNTIME_DEPS" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends $SPICA_RUNTIME_DEPS && \
        rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv /app/.venv
COPY spica ./spica

EXPOSE 8030
CMD ["python", "-m", "spica.app"]

# The worker's extra: ffmpeg, for the loudness job's EBU R128 measurement. It is a
# separate target so the picker image stays lean; `docker build --target worker`.
# The analysis image (Debian) installs ffmpeg through SPICA_RUNTIME_DEPS instead.
FROM runtime AS worker
RUN apk add --no-cache ffmpeg

# The default target is the picker/lean image. It comes last on purpose.
FROM runtime AS final
