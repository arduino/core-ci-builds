# ci-builds

Central store for CI build-metadata JSON snippets, with automated package index generation and publishing via GitHub Pages.

## How it works

1. Satellite repos call the reusable `store-snippets.yml` workflow at the end of their CI run
2. JSON snippets are committed to `snippets/{owner}/{repo}/{branch}/`
3. The `generate-index.yml` workflow triggers automatically, builds and publishes to GitHub Pages

## Repository layout

```
snippets/
  {owner}/
    {repo}/
      {branch}/
        platforms/
          <core-snippet-vxx-main>.json
          <core-snippet-vxx-contrib>.json
          ...           ← platform snippets from the same repo/branch
        tools/
          <tool-snippet-vxx>.json
          ...           ← tool snippets from the same repo/branch
        metadata/
          *.json      ← per-build metadata files (one per key in metadata input)
.github/workflows/
  store-snippets.yml  ← reusable workflow called by satellites
  generate-index.yml ← report generation + Pages deploy
```

## Setup

### 1. Enable GitHub Pages

In **ci-builds → Settings → Pages**, set source to **GitHub Actions**.

### 2. Create a fine-grained PAT for each satellite repo

In **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**:

- **Repository access**: `arduino/ci-builds` only
- **Permissions**: `Actions: write` — nothing else

This token can only trigger workflows on ci-builds. It **cannot push code**, read secrets, or access any other repo.

> No credential is stored in ci-builds itself. The commit is made using ci-builds's built-in `GITHUB_TOKEN`, which is auto-generated per run and never leaves the workflow.

### 3. Add the secret to each satellite repo

In each satellite repo → **Settings → Secrets → Actions**, add:

| Name | Value |
|---|---|
| `CI_BUILDS_ACTIONS_TOKEN` | the fine-grained PAT from step 2 |

### 4. Call the workflow from satellite CIs

The satellite job needs `id-token: write` permission to request an OIDC token from GitHub. Add this step **after** your JSON-generating steps:

```yaml
jobs:
  build:
    permissions:
      id-token: write   # required to obtain the OIDC token
    steps:
      # ... your build steps ...

      - name: Get OIDC identity token
        id: oidc
        uses: actions/github-script@v9
        with:
          script: |
            const token = await core.getIDToken('arduino/ci-builds');
            core.setOutput('token', token);

      - name: Store snippets in ci-builds
        uses: actions/github-script@v9
        with:
          github-token: ${{ secrets.CI_BUILDS_ACTIONS_TOKEN }}
          script: |
            await github.rest.actions.createWorkflowDispatch({
              owner: 'arduino',
              repo: 'ci-builds',
              workflow_id: 'store-snippets.yml',
              ref: 'main',
              inputs: {
                oidc_token: '${{ steps.oidc.outputs.token }}',
                // Required only when running from a tag ref; ignored for branch refs.
                // Snippets will be stored under this branch's folder.
                base_branch: '${{ github.base_ref || github.ref_name }}',
                // 'platform' (default) or 'tool'
                snippet_type: 'platform',
                // optional: build version
                version: '${{ steps.build.outputs.version }}',
                snippets: JSON.stringify({
                  'build-info.json': ${{ toJSON(steps.build.outputs.metadata) }},
                  'test-results.json': ${{ toJSON(steps.test.outputs.results) }},
                }),
                // optional: per-build metadata stored under metadata/
                metadata: JSON.stringify({
                  'ci.json': {
                    run_id: '${{ github.run_id }}',
                    run_url: '${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}',
                    sha: '${{ github.sha }}',
                  },
                }),
              },
            });
```

`snippets` is a JSON object: **keys** are the filenames to store, **values** are the JSON objects to write.

The satellite's repository and branch are **not passed as inputs**. They are extracted inside ci-builds from the OIDC token — a JWT signed by GitHub's own key. If the token is missing, expired, or was issued for a different audience, the workflow fails before any data is written.

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
2. Copies platform JSONs from `snippets/{owner}/{repo}/{branch}/platforms/` into `<indexname>/{owner}/platforms/`
3. Copies tool JSONs from `snippets/{owner}/{repo}/{branch}/tools/` into `<indexname>/{owner}/tools/`
4. Copies additional tools from `<indexname>_staging/*/tools/` in the private repo (if present)
5. Runs `meld.py --ref-index prod.json pages/<indexname>.json <indexname>/`

Output: `<indexname>.json` per repo/branch, published to GitHub Pages.

### Required secrets (in ci-builds)

| Name | Value |
|---|---|
| `PACKAGE_INDEX_DEPLOY_KEY` | SSH read-only key to the package index repo |
| `PACKAGE_INDEX_REPO` | Github package index repository to use |

