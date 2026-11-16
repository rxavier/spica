FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
# The analysis extra is installed by default so the published image can be turned on
# at runtime with SPICA_ANALYSIS=1; nothing imports it unless that flag is set.
# Build with --build-arg SPICA_ANALYSIS=0 for the smaller image.
ARG SPICA_ANALYSIS=1
RUN if [ "$SPICA_ANALYSIS" = "1" ]; then \
        uv sync --frozen --no-dev --no-install-project --extra analysis; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi

COPY spica ./spica

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8030

CMD ["python", "-m", "spica.app"]
