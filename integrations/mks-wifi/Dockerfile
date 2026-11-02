FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --extra web --no-install-project

COPY src ./src

RUN uv sync --locked --extra web --no-editable \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /nonexistent app \
    && mkdir -p /data \
    && chown app:app /data

ENV PATH="/opt/venv/bin:${PATH}"

USER app

EXPOSE 8000

CMD ["neptune-observatory", "--host", "0.0.0.0", "--port", "8000"]
