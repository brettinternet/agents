# syntax=docker/dockerfile:1.7
FROM node:24-trixie-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000
ARG TARGETARCH
ARG PI_VERSION=0.87.0
ARG HERDR_VERSION=0.9.1
ARG SOPS_VERSION=3.13.3
ARG AGE_VERSION=1.3.1
ARG JEGREP_VERSION=0.1.0
ARG MISE_VERSION=2026.9.11
ARG TASK_VERSION=3.52.0
ARG SUPERCRONIC_VERSION=0.2.49
ARG WORKLEASE_VERSION=1.7.3

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash ca-certificates curl fd-find fzf git gh jq less nano procps ripgrep tini tzdata util-linux wget \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      arm64) herdr_arch=aarch64; herdr_sha=f4ccf4de745f2cb9a39a983e9ba3703dad50ec2a58dea83026ceab721bbd8d9e ;; \
      amd64) herdr_arch=x86_64; herdr_sha=2a02fed16beb651ef006e1d43f048f652ca4dc58ad053cd2d44450563d5c54b7 ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/herdr "https://github.com/herdrdev/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-${herdr_arch}"; \
    echo "${herdr_sha}  /usr/local/bin/herdr" | sha256sum -c -; \
    chmod 0555 /usr/local/bin/herdr

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      arm64) \
        age_arch=arm64; \
        age_sha=c6878a324421b69e3e20b00ba17c04bc5c6dab0030cfe55bf8f68fa8d9e9093a; \
        sops_arch=arm64; \
        sops_sha=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77; \
        jegrep_target=aarch64-unknown-linux-gnu; \
        jegrep_sha=3712bd5ffd079f46d7d1aff6d62bf3f6acda88f9a0234c8ab77b04d21a358bc4; \
        mise_arch=arm64; \
        mise_sha=80ad6e9589d3c4483fd67a4a97f07e28330964dde64ca8cad15d5cb56134a54d; \
        task_arch=arm64; \
        task_sha=7e0044108830cec0534577b289564e3b7c83e6df276feb631a1edc63d04e4ebe; \
        supercronic_arch=arm64; \
        supercronic_sha=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5; \
        worklease_arch=arm64; \
        worklease_sha=59211ffe2e3ecae08b74e252c3cf4557a24406afa492a78a62fa82f7fc92d88d ;; \
      amd64) \
        age_arch=amd64; \
        age_sha=bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377; \
        sops_arch=amd64; \
        sops_sha=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b; \
        jegrep_target=x86_64-unknown-linux-gnu; \
        jegrep_sha=c860c1d5d7f657e5222136b2211b398d4183dae7feccda29d19f7439818ca64d; \
        mise_arch=x64; \
        mise_sha=6e39ee1ffa926a1f64814efbf1d26c3f617255e2a6ba6152e3c8a5135fa5a35f; \
        task_arch=amd64; \
        task_sha=02c679ffae53dca791804847d78b31731615894e292948397c971c87ac9e95bd; \
        supercronic_arch=amd64; \
        supercronic_sha=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1; \
        worklease_arch=x64; \
        worklease_sha=3119408910509de41f61a6012513dc416982c3cc76380a18a4487eb0ea0aa2d4 ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/age.tar.gz "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/age-v${AGE_VERSION}-linux-${age_arch}.tar.gz"; \
    echo "${age_sha}  /tmp/age.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/age.tar.gz -C /tmp; \
    install -m 0555 /tmp/age/age /tmp/age/age-keygen /usr/local/bin/; \
    curl -fsSLo /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${sops_arch}"; \
    echo "${sops_sha}  /usr/local/bin/sops" | sha256sum -c -; \
    chmod 0555 /usr/local/bin/sops; \
    curl -fsSLo /tmp/jegrep.tar.gz "https://github.com/can1357/jegrep/releases/download/v${JEGREP_VERSION}/jegrep-v${JEGREP_VERSION}-${jegrep_target}.tar.gz"; \
    echo "${jegrep_sha}  /tmp/jegrep.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/jegrep.tar.gz -C /tmp; \
    install -m 0555 "/tmp/jegrep-v${JEGREP_VERSION}-${jegrep_target}/jegrep" /usr/local/bin/jegrep; \
    curl -fsSLo /usr/local/bin/mise "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-${mise_arch}"; \
    echo "${mise_sha}  /usr/local/bin/mise" | sha256sum -c -; \
    chmod 0555 /usr/local/bin/mise; \
    curl -fsSLo /tmp/task.tar.gz "https://github.com/go-task/task/releases/download/v${TASK_VERSION}/task_linux_${task_arch}.tar.gz"; \
    echo "${task_sha}  /tmp/task.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/task.tar.gz -C /tmp task; \
    install -m 0555 /tmp/task /usr/local/bin/task; \
    curl -fsSLo /usr/local/bin/supercronic "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${supercronic_arch}"; \
    echo "${supercronic_sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0555 /usr/local/bin/supercronic; \
    curl -fsSLo /tmp/worklease.tar.gz "https://github.com/brettinternet/worklease/releases/download/v${WORKLEASE_VERSION}/worklease-v${WORKLEASE_VERSION}-linux-${worklease_arch}.tar.gz"; \
    echo "${worklease_sha}  /tmp/worklease.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/worklease.tar.gz -C /tmp; \
    install -m 0555 /tmp/bin/worklease /usr/local/bin/worklease; \
    rm -rf /tmp/age /tmp/age.tar.gz "/tmp/jegrep-v${JEGREP_VERSION}-${jegrep_target}" /tmp/jegrep.tar.gz /tmp/task /tmp/task.tar.gz /tmp/bin /tmp/share /tmp/worklease.tar.gz

RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"

RUN if ! getent group "${AGENT_GID}" >/dev/null; then groupadd --gid "${AGENT_GID}" agent; fi \
    && existing_user="$(getent passwd "${AGENT_UID}" | cut -d: -f1 || true)" \
    && if [ -z "${existing_user}" ]; then \
         useradd --uid "${AGENT_UID}" --gid "${AGENT_GID}" --create-home --home-dir /home/agent agent; \
       elif [ "${existing_user}" != agent ]; then \
         usermod --login agent --home /home/agent --move-home --gid "${AGENT_GID}" "${existing_user}"; \
       fi \
    && install -d -o "${AGENT_UID}" -g "${AGENT_GID}" /home/agent/.pi/agent

ENV HOME=/home/agent \
    PI_CODING_AGENT_DIR=/home/agent/.pi/agent \
    MISE_CONFIG_FILE=/workspace/.pi/mise.toml \
    MISE_IGNORED_CONFIG_PATHS=/workspace/mise.toml \
    MISE_DATA_DIR=/workspace/.tools/mise \
    PATH=/workspace/.tools/mise/shims:/usr/local/bin:/usr/bin:/bin \
    SOPS_AGE_KEY_FILE=/workspace/.env.sops-age \
    SOPS_DECRYPTION_ORDER=age \
    WORKLEASE_HOME=/workspace/.pi-data/worklease \
    TZ=UTC

WORKDIR /workspace
USER agent
ENTRYPOINT ["/usr/bin/tini", "--", "supercronic", "-no-reap", "-inotify", "/workspace/jobs/crontab"]
