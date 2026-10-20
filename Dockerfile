FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# Dependencies first (cached unless pyproject/uv.lock change).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

# Editable install: the CLI finds seed/ and examples/ relative to src/.
COPY . .
RUN uv sync --locked --no-dev && chmod +x docker/entrypoint.sh

EXPOSE 8501
ENTRYPOINT ["/app/docker/entrypoint.sh"]
