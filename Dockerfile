FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
# The default image carries the picker's half (numpy, for reading vectors). The
# analyser's half (onnxruntime + essentia) is a separate build so the always-on web
# process does not carry ~400 MB it never imports:
#   docker build --build-arg SPICA_EXTRAS=analysis -t …-analysis .
ARG SPICA_EXTRAS=vectors
RUN uv sync --frozen --no-dev --no-install-project --extra "$SPICA_EXTRAS"

COPY spica ./spica

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8030

CMD ["python", "-m", "spica.app"]
