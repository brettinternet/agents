# syntax=docker/dockerfile:1.7
FROM node:24-trixie-slim

ARG AGENT_UID=1000
ARG AGENT_GID=1000
ARG TARGETARCH
ARG PI_VERSION=0.86.1
ARG SOPS_VERSION=3.13.3
ARG AGE_VERSION=1.3.1
ARG JEGREP_VERSION=0.1.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash ca-certificates curl fd-find fzf git gh jq less nano procps ripgrep tini wget \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      arm64) \
        age_arch=arm64; \
        age_sha=c6878a324421b69e3e20b00ba17c04bc5c6dab0030cfe55bf8f68fa8d9e9093a; \
        sops_arch=arm64; \
        sops_sha=53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77; \
        jegrep_target=aarch64-unknown-linux-gnu; \
        jegrep_sha=3712bd5ffd079f46d7d1aff6d62bf3f6acda88f9a0234c8ab77b04d21a358bc4 ;; \
      amd64) \
        age_arch=amd64; \
        age_sha=bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377; \
        sops_arch=amd64; \
        sops_sha=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b; \
        jegrep_target=x86_64-unknown-linux-gnu; \
        jegrep_sha=c860c1d5d7f657e5222136b2211b398d4183dae7feccda29d19f7439818ca64d ;; \
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
    rm -rf /tmp/age /tmp/age.tar.gz "/tmp/jegrep-v${JEGREP_VERSION}-${jegrep_target}" /tmp/jegrep.tar.gz

RUN npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"

RUN if ! getent group "${AGENT_GID}" >/dev/null; then groupadd --gid "${AGENT_GID}" agent; fi \
    && useradd --uid "${AGENT_UID}" --gid "${AGENT_GID}" --create-home --home-dir /home/agent agent \
    && install -d -o "${AGENT_UID}" -g "${AGENT_GID}" /home/agent/.pi/agent

ENV HOME=/home/agent \
    PI_CODING_AGENT_DIR=/home/agent/.pi/agent \
    SOPS_AGE_KEY_FILE=/workspace/.env.sops-age \
    SOPS_DECRYPTION_ORDER=age

WORKDIR /workspace
USER agent
ENTRYPOINT ["/usr/bin/tini", "--", "sops", "exec-env", "--same-process", "/workspace/secrets.sops.env", "pi --approve"]
