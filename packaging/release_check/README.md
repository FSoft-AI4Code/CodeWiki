# Release check (real model, by hand)

One end-to-end run against a real LLM. Run it once per OS (Linux, Windows, macOS), each with a
different Python version, before or after publishing to PyPI. CI's `test_wheel.sh` already
covers each compiled wheel without a key.

Needs Python 3.12+ and git. Each run makes one full `generate` and one `--update` on the repo,
so use a small repo or a cheap model.

```bash
cp config.example.env config.env      # fill in api-key and models

# pre-publish: a wheel file, or a folder of wheels (e.g. an unzipped CI artifact)
python release_check.py --repo https://github.com/you/project.git --config config.env --wheel ./wheels

# post-publish: from PyPI
python release_check.py --repo https://github.com/you/project.git --config config.env --package nc-codewiki==2.0.2

# no --wheel/--package: asks which one
python release_check.py --repo https://github.com/you/project.git --config config.env
```

Windows: `py -3.12 release_check.py ...`. The Python you run it with is the one tested.

Options: `--branch main`, `--behind 10`, `--question "..."`, `--out DIR`.

Steps: venv, uv, install, `codewiki --version`, `config set` from the file, `config validate`,
clone, `generate --verbose --github-pages` at `<branch>~10`, `generate --update` at `<branch>`,
then `codewiki_status`, `codewiki_search` and `codewiki_ask` through the MCP server, and `codewiki ask`
from the CLI.

Config is written to `<out>/home`, never to your real `~/.codewiki` or keyring.

Output folder `release-check-<os>-py<ver>-<time>/`:

| File | What |
|---|---|
| `summary.txt` | PASS/FAIL per step, Traceback/ERROR counts per log |
| `setup-logs.txt` | install, config, clone |
| `logs.txt`, `docs.zip` | first generate |
| `update-logs.txt`, `docs-updated.zip` | `--update` run |
| `mcp-logs.txt` | MCP tool calls and the CLI `ask` answer |

The exit code is 0 only if every step passed.
