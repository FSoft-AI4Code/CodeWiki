# Backstage TechDocs

CodeWiki can publish its documentation in a
[Backstage](https://backstage.io/docs/features/techdocs/) developer portal.
The export turns the generated `docs/` folder into an MkDocs project that
TechDocs builds as is:

```
./techdocs/
├── mkdocs.yml      # site name, repo link, techdocs-core, nav from the module tree
└── docs/
    ├── index.md    # the repository overview (overview.md)
    ├── <module>.md ...
    ├── <module>/<sub-module>.md ...
    └── *.png, *.svg ...   # images found in the CodeWiki docs
```

Only pages and images are copied. `module_tree.json`, `metadata.json`,
`update_record.json` and `temp/` stay out of the published site. The CodeWiki
docs themselves are never modified.

## Exporting

During generation:

```bash
codewiki generate --techdocs                          # writes ./techdocs
codewiki generate --techdocs --techdocs-dir site/td   # somewhere else
```

From docs you already have, with no LLM calls (for example after
`codewiki generate --update`):

```bash
codewiki techdocs                 # ./docs -> ./techdocs
codewiki techdocs docs techdocs --site-name "Payments API"
```

Every run replaces `techdocs/docs/` completely. A marker file inside it proves
that an earlier export created it, and CodeWiki refuses to replace a
`docs/` folder without that marker. The output directory must be outside
the CodeWiki docs directory.

## What each page gets

- **Front matter:** the page title (its first heading) and a short
  description (its first paragraph).
- **A provenance banner:** an info admonition with the commit, date and model
  the page was written with, linked to the commit on GitHub, GitLab or
  bitbucket.org.
- **A "Documented components" list:** a collapsed block naming the classes and
  functions the module covers, each linked to its file at the documented
  commit (up to 50 entries).
- **Fixed links:** links between pages are rewritten for the new layout
  (links to `overview.md` now point at `index.md`), and a link labelled with a
  module name shows that page's title instead.

The repository URL comes from the git `origin` remote (SSH remotes are
converted to https and credentials are dropped). Override it with `--repo-url`.
For hosts other than GitHub, GitLab and bitbucket.org, such as Bitbucket
Server, give the source link format yourself:

```bash
codewiki techdocs --repo-url https://bb.example.com/projects/P/repos/R \
  --source-url-template 'https://bb.example.com/projects/P/repos/R/browse/{path}?at={ref}'
```

## Publishing in Backstage

Point the component's `catalog-info.yaml` at the exported folder:

```yaml
metadata:
  annotations:
    backstage.io/techdocs-ref: dir:./techdocs
```

Or build it yourself, for example in CI with the "recommended" external
build setup:

```bash
npx @techdocs/cli generate --source-dir ./techdocs --output-dir ./site --etag <commit>
npx @techdocs/cli publish --publisher-type <awsS3|googleGcs|...> --storage-name <bucket> --entity <namespace/kind/name>
```

`techdocs-cli generate` runs MkDocs in a Docker image by default. To build
locally instead, run `pip install mkdocs-techdocs-core` and pass `--no-docker`.

### Mermaid diagrams

CodeWiki pages contain many Mermaid diagrams. The generated `mkdocs.yml` keeps
every ```` ```mermaid ```` block as `<pre class="mermaid">`, but Backstage
renders them only when the Mermaid TechDocs addon
([`backstage-plugin-techdocs-addon-mermaid`](https://www.npmjs.com/package/backstage-plugin-techdocs-addon-mermaid))
is installed in your Backstage app. Without it, diagrams show as their source
text.

## Keeping it current

The export is cheap, so run it after every update. In CI:

```bash
codewiki generate --update --techdocs
```

`--update` reuses the existing docs and `--techdocs` re-exports all of them.
To re-export without running generation, use `codewiki techdocs`.
