# ci-builds

Central store for CI-generated JSON snippets and their metadata, with automated package index generation and publishing via GitHub Pages.

## How it works

1. Satellite repos pack their JSON snippets in an artifact and include this repo as a composite action at the end of their CI run
2. The action passes the JSON snippets and the OIDC-certified repo/branch to `store_snippets.yml` via `workflow_dispatch` on this repo
3. JSON snippets are committed to `snippets/{owner}/{repo}/{branch}/{version}/` in this repo
4. The `generate-index.yml` workflow triggers automatically, builds and publishes to GitHub Pages

## Repository layout

```
snippets/
  {owner}/
    {repo}/
      {branch}/
        tagged-versions               ← list of released version_shorts (one per line)
        {version_short}/              ← e.g. 1.2.3
          platforms/
            {version}-<snippet>.json  ← e.g. 1.2.3-rc1-build-info.json
            ...
          tools/
            <snippet>.json
            ...
          metadata/
            {version}-<file>.json
            ...
action.yml                           ← composite action used by satellites
.github/workflows/
  store-snippets.yml  ← reusable workflow called by the composite action
  generate-index.yml ← report generation + Pages deploy
```

## Versioning

Each CI run stores files under `snippets/{owner}/{repo}/{branch}/{version_short}/`.

**`version_short`** is always `maj.min.rev` (e.g. `1.2.3`), stripped from the full build version. The full version (including any pre-release suffix, e.g. `1.2.3-rc1`) appears in filenames:

- **`platforms/` and `metadata/`** files get a `{version}-` prefix (e.g. `1.2.3-rc1-build-info.json`). Multiple pre-release and release builds for the same `version_short` coexist in the same directory. When a new push arrives for the same prefix (e.g. a tag update), older files with that prefix are removed first to avoid duplicates.

- **`tools/`** files are stored without a version prefix and are **overwritten** on each push. They represent the current toolchain state for that branch, not a per-version snapshot.

### Release tracking

When a satellite triggers from a tag ref and the full version equals `version_short` (no pre-release suffix), the version is appended to `tagged-versions`:

```
snippets/{owner}/{repo}/{branch}/tagged-versions
```

This one-line-per-version file is read by `generate-index.yml` to drive the tool selection logic below.

### Tool selection

Tools accumulate across version directories, but the generated index only includes a subset to avoid duplicating tools already present in the public `package_index.json`:

| Run type | Condition | Tools included |
|---|---|---|
| **Pre-release** | highest version dir > last tagged release | tools added **after** the last tagged release |
| **Release** | highest version dir == last tagged release | tools added **since the previous** tagged release |

The reasoning: by the time a release tag is cut, the public index has not yet been updated — so tools from the new release must be included. But tools from earlier releases are already in the public index and should not be duplicated.

### Sunsetting and quarterly archives

Each version directory is dated by the last commit touching any of its files, and is never split across indexes:

- **Current index** (`package_{owner}_{repo}_{branch}_ci_index.json`): only version directories modified in the last 90 calendar days, and only if the branch still exists in the source repo. Otherwise no current index is generated and the branch disappears from the main list.
- **Quarterly archives** (`package_{owner}_{repo}_{branch}_ci_{YYYY}q{N}_index.json`): every version directory is also included in the archive of the quarter it was last modified in. Archives are listed in a separate section of the page.

Tool selection for an archive follows the rules above, evaluated as of the highest version directory in that archive.

## Setup

### 1. Enable GitHub Pages

In **ci-builds → Settings → Pages**, set source to **GitHub Actions**.

### 2. Create a fine-grained PAT for each satellite repo

In **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**:

- **Repository access**: `arduino/core-ci-builds` only
- **Permissions**: `Actions: write` — nothing else

This token can only trigger workflows on ci-builds. It **cannot push code**, read secrets, or access any other repo.

> No credential is stored in ci-builds itself. The commit is made using ci-builds's built-in `GITHUB_TOKEN`, which is auto-generated per run and never leaves the workflow.

### 3. Add the secret to each satellite repo

In each satellite repo → **Settings → Secrets → Actions**, add:

| Name | Value |
|---|---|
| `CI_BUILDS_ACTIONS_TOKEN` | the fine-grained PAT from step 2 |

### 4. Call the workflow from satellite CIs

The satellite job needs `id-token: write` permission to be able to acquire the OIDC token.

Upload the snippet files as a GitHub Actions artifact. The artifact must contain JSON files laid out under `platforms/`, `tools/`, and/or `metadata/`:

```
platforms/
  build-info.json
tools/
  gcc.json
metadata/
  ci.json
```

Then call this repo as a composite action. Pass the artifact name and the PAT for triggering the dispatch:

```yaml
jobs:
  build:
    permissions:
      id-token: write   # required — inherited by the composite action
    steps:
      # ... your build steps that produce JSON files ...

      - name: Upload snippets artifact
        uses: actions/upload-artifact@v4
        with:
          name: ci-builds-snippets
          path: snippets/   # directory containing platforms/, tools/, metadata/

      - uses: arduino/core-ci-builds@main
        with:
          token: ${{ secrets.CI_BUILDS_ACTIONS_TOKEN }}
          version: ${{ env.VERSION }}
          artifact: ci-builds-snippets
          # base-branch is required only when triggering from a tag ref:
          # base-branch: ${{ github.ref_name }}
```

The `store-snippets.yml` workflow downloads the artifact, which is expected to contain JSON files under `platforms/`, `tools/`, and/or `metadata/`.

All entries are stored under `{branch}/{version_short}/`, with a `{version}-` prefix added to `platforms/` and `metadata/` filenames. `tools/` filenames are stored as-is because they are only stored on full tags and are not strictly related to the core version.

The calling satellite's repository and branch are **not passed as inputs**. They are encoded in the OIDC token (a JWT signed by GitHub's own key) that is generated in the caller's context and extracted inside ci-builds. If the token is missing, expired, or was issued for a different audience, the workflow fails before any data is written.

### Why `workflow_dispatch` instead of a reusable workflow?

With `workflow_call`, the workflow runs on the **satellite's runner** — so any write credential must live in the satellite's secrets.

With `workflow_dispatch`, the workflow runs **inside ci-builds** — ci-builds uses its own auto-generated `GITHUB_TOKEN` to commit. The satellite credential is downgraded to `Actions: write` only:

| | `workflow_call` + App key | `workflow_dispatch` + Actions token + OIDC |
|---|---|---|
| Secret stored in satellite | App private key | Actions-only token |
| Stolen token can push code | ✅ yes (via app key) | ❌ no |
| Stolen token can read ci-builds secrets | ❌ | ❌ |
| ci-builds commit credential | App token (external) | `GITHUB_TOKEN` (built-in, ephemeral) |
| Source repo/branch verified | ❌ caller-provided | ✅ GitHub-signed OIDC token |
| Validation before commit | possible | ✅ enforced in ci-builds workflow |

## Customising the output

`generate-index.yml` clones the private package-index repo (via `PACKAGE_INDEX_DEPLOY_KEY` + `PACKAGE_INDEX_REPO` secrets) and for each `snippets/{owner}/{repo}/{branch}/` folder:

1. Derives the index name: strips `ArduinoCore-` from repo, concatenates owner, short repo, branch and `_ci`
   e.g. `arduino / ArduinoCore-zephyr / main` → `arduino_zephyr_main_ci`
2. Copies platform JSONs from `snippets/{owner}/{repo}/{branch}/*/platforms/` into `<indexname>/{owner}/platforms/`
3. Copies tool JSONs (subject to tool selection rules above) into `<indexname>/{owner}/tools/`
4. Copies additional tools from `<indexname>_staging/*/tools/` in the private repo (if present)
5. Runs `meld.py --ref-index prod.json pages/<indexname>.json <indexname>/`

Output: one `<indexname>.json` per repo/branch, published to GitHub Pages at:
```
https://{owner}.github.io/{repo}/<indexname>.json
```

### index.html

In addition to the JSON indexes, `generate-index.yml` generates `pages/index.html` — a browsable summary page published at `https://{owner}.github.io/{repo}/`.

The page lists every CI index as a collapsible row:

| Column | Content |
|---|---|
| Name | `{owner}/{repo} @ {branch}` — links to the JSON index |
| Latest version | Latest platform version across all builds |
| Last modified | Timestamp of the most recently committed snippet file |

Each row expands to show sub-groups per platform and tool, each with all available versions and their individual timestamps. A search bar filters across all entries. The production `package_index.json` is shown as a reference row (labelled "official") when a search is active.

### Required secrets (in ci-builds)

| Name | Value |
|---|---|
| `PACKAGE_INDEX_DEPLOY_KEY` | SSH read-only key to the package index repo |
| `PACKAGE_INDEX_REPO` | Github package index repository to use |

