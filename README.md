# Agents

A small, containerized [Pi](https://pi.dev) environment for agents that browse the internet, use explicitly provided accounts, and maintain searchable Markdown memory.

There is no dashboard, scheduler, database, message bus, or agent control plane. Pi runs directly in one Docker container with the repository mounted at `/workspace`.

## Included

- Pi with [`brettinternet/pi-extensions`](https://github.com/brettinternet/pi-extensions)
- Internet tools: `curl`, `wget`, Git, GitHub CLI, and `jq`
- Memory search: [`jegrep`](https://github.com/can1357/jegrep), `rg`, `fd`, and `fzf`
- SOPS + age for a committed encrypted environment file
- The durable Markdown memory in [`memory/`](memory/README.md)

## Start

Requires [mise](https://mise.jdx.dev), Docker, and Git.

```sh
task init
task agent
```

`task init` installs the small host toolchain, creates private local directories, verifies the encrypted secret store, and builds the image. The first Pi session asks you to authenticate a model provider unless its credentials are already in the encrypted environment.

The repository is mounted read/write. Pi settings, installed extensions, sessions, and provider logins are kept in the ignored `.pi-data/` directory.

## Secrets and accounts

`secrets.sops.env` is committed ciphertext. `.env.sops-age` is its ignored local age identity and must be backed up separately. The container can read that identity and decrypt the whole environment: SOPS protects secrets in Git and at rest, **not from the agent**. Only put accounts in this store when the agent is allowed to use them.

Add or change values:

```sh
task secrets:edit
task secrets:check
```

Run a host command with the decrypted environment:

```sh
task secrets:run -- gh auth status
task secrets:run -- curl https://api.example.com/me
```

`OPENROUTER_API_KEY` enables `jegrep`. Provider variables such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` are also available to Pi when present. Never commit `.env.sops-age` or plaintext secret files.

## Memory

Memory is ordinary Markdown so agents can read and edit it directly.

```sh
task memory:list
task memory:grep -- 'search terms'
task memory:semantic -- 'where did we decide how credentials work?'
task memory:pick
```

Read [`memory/README.md`](memory/README.md) before adding durable memory. Commit useful, public-safe memories with the repository; do not store credentials, private messages, or raw transcripts.

## Other tasks

```sh
task container:build
task shell
task check
```
