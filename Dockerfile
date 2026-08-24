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
    && apt-get install -y --no-install-recommends ca-certificates curl git procps tini util-linux \
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
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH:-$(uname -m)}" in \
      arm64|aarch64) age_arch=arm64; age_sha=c6878a324421b69e3e20b00ba17c04bc5c6dab0030cfe55bf8f68fa8d9e9093a; sops_arch=arm64; sops_sha=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77; varlock_arch=arm64; varlock_sha=08ea40fdca2ffeb7bb0afe6f47813bab43298c1ce897f07e013aae2649bfc01a ;; \
      amd64|x86_64) age_arch=amd64; age_sha=bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377; sops_arch=amd64; sops_sha=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b; varlock_arch=x64; varlock_sha=9b0ee1a7d42469c27dbfa284fa4337eb02c39c259198f36c5b127d5c3fb7a89d ;; \
      *) echo "unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/age.tar.gz "https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-linux-${age_arch}.tar.gz"; \
    echo "${age_sha}  /tmp/age.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/age.tar.gz -C /tmp; \
    install -m 0555 /tmp/age/age /tmp/age/age-keygen /usr/local/bin/; \
    curl -fsSLo /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/v3.13.3/sops-v3.13.3.linux.${sops_arch}"; \
    echo "${sops_sha}  /usr/local/bin/sops" | sha256sum -c -; \
    chmod 0555 /usr/local/bin/sops; \
    curl -fsSLo /tmp/varlock.tar.gz "https://github.com/dmno-dev/varlock/releases/download/varlock%401.17.0/varlock-linux-${varlock_arch}.tar.gz"; \
    echo "${varlock_sha}  /tmp/varlock.tar.gz" | sha256sum -c -; \
    mkdir /tmp/varlock-dist; \
    tar -xzf /tmp/varlock.tar.gz -C /tmp/varlock-dist; \
    install -m 0555 /tmp/varlock-dist/varlock /tmp/varlock-dist/varlock-local-encrypt /usr/local/bin/; \
    rm -rf /tmp/age /tmp/age.tar.gz /tmp/varlock-dist /tmp/varlock.tar.gz
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["agents-secrets-broker"]
