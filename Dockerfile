# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=ghcr.io/astral-sh/uv:python3.14-bookworm-slim
ARG BUN_IMAGE=oven/bun:1.3.14

FROM alpine:3.23 AS task
ARG TASK_VERSION=3.52.0
ARG TASK_SHA256=7e0044108830cec0534577b289564e3b7c83e6df276feb631a1edc63d04e4ebe
RUN wget -q "https://github.com/go-task/task/releases/download/v${TASK_VERSION}/task_linux_arm64.tar.gz" -O /tmp/task.tgz \
    && echo "${TASK_SHA256}  /tmp/task.tgz" | sha256sum -c - \
    && tar -xzf /tmp/task.tgz -C /usr/local/bin task \
    && chmod 0555 /usr/local/bin/task
FROM ${BUN_IMAGE} AS bun

FROM ${PYTHON_IMAGE} AS agent-base
ARG AGENTS_UID=1000
ARG AGENTS_GID=1000
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git tini util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-$(uname -m)}" in \
         arm64|aarch64) herdr_arch=aarch64; herdr_sha=f55610658e1c2e0d2aaef730b4b2ab885f7f8ba00285ab372bfb14f2e3d5b40d ;; \
         amd64|x86_64) herdr_arch=x86_64; herdr_sha=976150a14d490c94b243ea2e1a7eb2dfb67f12e36b182db90936f6728e6aecf4 ;; \
         *) echo "unsupported target architecture: ${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/herdrdev/herdr/releases/download/v0.8.2/herdr-linux-${herdr_arch}" \
         -o /usr/local/bin/herdr \
    && echo "${herdr_sha}  /usr/local/bin/herdr" | sha256sum -c - \
    && chmod 0555 /usr/local/bin/herdr \
    && if ! getent group "${AGENTS_GID}" >/dev/null; then groupadd --gid "${AGENTS_GID}" agents; fi \
    && existing_user="$(getent passwd "${AGENTS_UID}" | cut -d: -f1 || true)" \
    && if [ -z "${existing_user}" ]; then \
         useradd --uid "${AGENTS_UID}" --gid "${AGENTS_GID}" --home-dir /home/agents --create-home agents; \
       elif [ "${existing_user}" != agents ]; then \
         usermod --login agents --home /home/agents --move-home --gid "${AGENTS_GID}" "${existing_user}"; \
       fi \
    && install -d -o "${AGENTS_UID}" -g "${AGENTS_GID}" /home/agents
COPY --from=task /usr/local/bin/task /usr/local/bin/task
WORKDIR /opt/agents
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY agents ./agents
COPY agents.toml .env.schema Taskfile.dist.yaml ./
COPY tests ./tests
RUN uv sync --frozen --no-dev
ENV PATH=/opt/agents/.venv/bin:/opt/agents/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY container/ /opt/agents/bin/
RUN chmod 0555 /opt/agents/bin/*
USER ${AGENTS_UID}:${AGENTS_GID}
ENTRYPOINT ["/usr/bin/tini","--"]

FROM agent-base AS agent-opencode
USER root
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
RUN BUN_INSTALL=/usr/local bun install --global opencode-ai@1.18.21
USER agents
CMD ["opencode"]

FROM agent-base AS agent-claude
USER root
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
RUN BUN_INSTALL=/usr/local bun install --global @anthropic-ai/claude-code@2.1.229
USER agents
CMD ["claude"]

FROM agent-base AS agent-mock
USER root
COPY tests/fixtures/bin/mock_cli /usr/local/bin/mock_cli
RUN chmod 0555 /usr/local/bin/mock_cli
USER agents
CMD ["mock_cli"]

FROM agent-opencode AS system-opencode
WORKDIR /workspace
CMD ["agents","service","foreground"]

FROM agent-claude AS system-claude
WORKDIR /workspace
CMD ["agents","service","foreground"]

FROM agent-mock AS system-mock
WORKDIR /workspace
CMD ["agents","service","foreground"]

FROM agent-base AS secrets
USER root
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["agents-secrets-broker"]
