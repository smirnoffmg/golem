# One image for every container; the role (edge, task service, orchestrator, job runtime)
# is chosen by the command the deployment passes.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN groupadd --system golem && useradd --system --gid golem --no-create-home golem
USER golem

ENV PATH="/app/.venv/bin:$PATH"
