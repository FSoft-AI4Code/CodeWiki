# Security Policy

## Reporting a vulnerability

Please do not report security issues through public GitHub issues, pull
requests, or discussions.

Use GitHub's private vulnerability reporting instead:

**https://github.com/FSoft-AI4Code/CodeWiki/security/advisories/new**

This opens a private thread that only the CodeWiki maintainers can see. You do
not need to be a collaborator on the repository to use it.

When reporting, please include as much of the following as you can:

- The version or commit of CodeWiki you tested against
- The command line or configuration you used (redact any API keys)
- Steps to reproduce, or a minimal repository that triggers the issue
- What an attacker can achieve, and under which conditions
- Any suggested fix, if you have one

## What to expect

- We will acknowledge your report within **5 business days**.
- We will keep you informed as we confirm the issue and work on a fix.
- We aim to publish a fix and a GitHub Security Advisory within **90 days** of
  the initial report. If we need more time, we will tell you why and agree on
  a new date with you.
- We will credit you in the advisory unless you ask us not to.

## Supported versions

Security fixes are applied to the `main` branch and to the most recent
release. Older versions are not maintained.

## Scope

CodeWiki clones or reads repositories that you point it at. In subscription
mode it routes LLM calls through the local `claude` or `codex` CLI, and in
IDE-driven mode it runs as an MCP server driven by an AI IDE agent on your
machine. The trust boundary we care about most is the boundary between the
analyzed repository and the rest of your system.

In scope:

- Any way for content inside an analyzed repository (source files, READMEs,
  configuration, file names) to cause CodeWiki or the agent it drives to read
  or write files outside the intended output directory, execute commands, or
  exfiltrate data such as API keys
- Path traversal, command injection, or unsafe deserialization in CodeWiki
  itself
- Leakage of credentials, tokens, or environment variables into generated
  documentation or logs
- Vulnerabilities in the Docker images or the web viewer shipped in this
  repository

Out of scope:

- Inaccurate, incomplete, or hallucinated documentation content
- Vulnerabilities in the underlying LLM providers or agent CLIs (Claude Code,
  Codex CLI, and others). Please report those upstream.
- Issues that require the attacker to already control the machine running
  CodeWiki
- Findings from automated scanners without a demonstrated impact

If you are unsure whether something is in scope, report it privately anyway
and we will work it out together.
