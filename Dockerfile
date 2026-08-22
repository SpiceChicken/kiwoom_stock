# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.14
ARG PYTHON_LOCK=py314

FROM python:${PYTHON_VERSION}-slim AS base

ARG PYTHON_LOCK

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 kiwoom \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin kiwoom \
    && mkdir -p /var/lib/kiwoom/output /var/log/kiwoom \
    && chown -R 10001:10001 /var/lib/kiwoom /var/log/kiwoom

FROM base AS builder

ARG PYTHON_LOCK

COPY pyproject.toml README.MD ./
COPY requirements/locks/dev-${PYTHON_LOCK}.txt /tmp/dev-lock.txt
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r /tmp/dev-lock.txt \
    && python -m build --no-isolation

FROM base AS test

ARG PYTHON_LOCK

ENV CONTAINER_TEST_STAGE=1

COPY pyproject.toml README.MD ./
COPY requirements/locks/dev-${PYTHON_LOCK}.txt /tmp/dev-lock.txt
COPY requirements/locks ./requirements/locks
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
COPY compose.yaml compose.dev.yaml compose.mock.yaml compose.prod.yaml compose.shadow.yaml ./
COPY .github/workflows/ci.yml .github/workflows/cd-production-check.yml .github/workflows/cd-production-promotion.yml .github/workflows/cd-shadow-schedule-audit.yml .github/workflows/cd-shadow-worker-activation.yml .github/workflows/cd-shadow-worker-rollout.yml .github/workflows/cd-shadow-rollout-document-migration.yml ./.github/workflows/

RUN mkdir /app/.git

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r /tmp/dev-lock.txt \
    && python -m pip install --no-deps --no-build-isolation -e .

CMD ["python", "-m", "pytest", "tests", "-q", "--basetemp=/tmp/pytest"]

FROM base AS runtime

ARG PYTHON_LOCK

COPY --from=builder /app/dist/*.whl /tmp/
COPY requirements/locks/runtime-${PYTHON_LOCK}.txt /tmp/runtime-lock.txt
COPY docker/runtime_entrypoint.py /usr/local/bin/kiwoom-runtime-entrypoint.py

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --require-hashes -r /tmp/runtime-lock.txt \
    && python -m pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -f /tmp/*.whl

USER 10001:10001

VOLUME ["/var/lib/kiwoom"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py", "--healthcheck"]

CMD ["python", "-m", "kiwoom_stock", "--check-config"]
