# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 kiwoom \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin kiwoom \
    && mkdir -p /var/lib/kiwoom/output /var/log/kiwoom \
    && chown -R 10001:10001 /var/lib/kiwoom /var/log/kiwoom

FROM base AS builder

COPY pyproject.toml README.MD ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip build \
    && python -m build

FROM base AS test

ENV CONTAINER_TEST_STAGE=1

COPY pyproject.toml README.MD ./
COPY src ./src
COPY tests ./tests
COPY main.py ./
COPY tools ./tools
COPY docker ./docker
COPY deploy ./deploy
COPY prompt ./prompt
COPY .env.example ./
COPY Dockerfile .dockerignore .gitleaks.toml ./
COPY docs/configuration.md ./docs/configuration.md
COPY docs/operations ./docs/operations
COPY compose.yaml compose.dev.yaml compose.mock.yaml compose.prod.yaml ./
COPY .github/workflows/ci.yml .github/workflows/cd-production-check.yml ./.github/workflows/

RUN mkdir /app/.git

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools \
    && python -m pip install -e ".[dev]"

CMD ["python", "-m", "pytest", "tests", "-q", "--basetemp=/tmp/pytest"]

FROM base AS runtime

COPY --from=builder /app/dist/*.whl /tmp/
COPY docker/runtime_entrypoint.py /usr/local/bin/kiwoom-runtime-entrypoint.py

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl

USER 10001:10001

VOLUME ["/var/lib/kiwoom"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py", "--healthcheck"]

CMD ["python", "-m", "kiwoom_stock", "--check-config"]
