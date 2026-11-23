# Run the CLI with Docker

The Docker image runs CodeWiki CLI commands against a mounted Git repository.
The hosted web application and HTTP documentation server have been removed.
Generate documentation with the CLI, then publish its static HTML viewer with
GitHub Pages or export it to Backstage TechDocs.

## Build

From the CodeWiki checkout:

```bash
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml run --rm codewiki
```

The second command prints CLI help. The image includes Python 3.12, Git,
Node.js/npm for dependency installation, the CodeWiki package, and predownloaded
tree-sitter grammars. Its entry point is `python -m codewiki`; arguments passed
to the container are CodeWiki arguments.

## Mount the target repository

Copy the example and set `CODEWIKI_REPO` to the absolute path of the Git
repository you want to document:

```bash
cp docker/env.example .env
# Edit CODEWIKI_REPO in .env, for example:
# CODEWIKI_REPO=/home/me/projects/my-project
```

Without an override, Compose mounts the CodeWiki checkout itself. The target
is mounted read/write at `/workspace`, which is the container's working
directory. Generated `docs/` and other output therefore appear directly in
the target repository on your host.

Compose uses `.env` for path interpolation. It does not inject the old web
app's `LLM_*` settings into CLI configuration.

## Configure a provider

Configure an API-backed provider once, using the same CLI options as a local
installation. Substitute your endpoint, API key, and model names:

```bash
docker compose -f docker/docker-compose.yml run --rm codewiki config set \
  --provider openai-compatible \
  --base-url https://api.example.com/v1 \
  --api-key YOUR_API_KEY \
  --main-model YOUR_MODEL \
  --cluster-model YOUR_MODEL
```

Settings persist in the Compose `codewiki-config` volume, mounted at
`/root/.codewiki`. `CODEWIKI_NO_KEYRING=1` selects file-based credential storage
inside that volume. Treat it as private and do not commit credentials.
See [Providers and models](providers.md) for other API providers.

Claude Code and Codex executables and their subscription authentication are
not bundled in this image. Use a local installation for subscription mode,
or extend the image and provide the corresponding CLI authentication.

## Generate documentation

```bash
docker compose -f docker/docker-compose.yml run --rm codewiki generate
docker compose -f docker/docker-compose.yml run --rm codewiki generate --github-pages
docker compose -f docker/docker-compose.yml run --rm codewiki generate --update
docker compose -f docker/docker-compose.yml run --rm codewiki generate --techdocs
```

Commands exit when the job finishes. There is no listening HTTP port, health
check, daemon restart policy, or external Docker network to create. For private
repositories, mount a local checkout; Git credentials and SSH keys are not
mounted by default.

The equivalent direct Docker workflow is:

```bash
docker build -f docker/Dockerfile -t codewiki:local .
docker run --rm codewiki:local --version
docker run --rm \
  --mount type=bind,src=/absolute/path/to/project,dst=/workspace \
  --mount type=volume,src=codewiki-cli-config,dst=/root/.codewiki \
  codewiki:local config set --provider openai-compatible \
  --base-url https://api.example.com/v1 --api-key YOUR_API_KEY \
  --main-model YOUR_MODEL --cluster-model YOUR_MODEL
docker run --rm \
  --mount type=bind,src=/absolute/path/to/project,dst=/workspace \
  --mount type=volume,src=codewiki-cli-config,dst=/root/.codewiki \
  codewiki:local generate --github-pages
```

Direct Docker uses a separate configuration volume in this example. Reuse
the same volume across configuration and generation commands.

## Updating and troubleshooting

- Rebuild the image after updating CodeWiki: `docker compose -f docker/docker-compose.yml build`.
- A configuration error usually means the provider has not been configured in
  the container's volume. Host keychain credentials are not available there.
- If output is not writable, check ownership of the mounted repository.
  Linux containers run as root by default, so generated files may need their
  ownership adjusted on the host.
- Stop any old web-app containers before switching to this workflow. The
  former browser submission queue and cache are not migrated into CLI state;
  keep any documentation you want from the old output mount.
- `docker compose -f docker/docker-compose.yml down` preserves configuration.
  Adding `--volumes` deletes the saved settings and credentials.

See the [CLI reference](cli-reference.md), [MCP guide](mcp-ide-mode.md), and
[Backstage TechDocs guide](backstage-techdocs.md) for supported workflows.
