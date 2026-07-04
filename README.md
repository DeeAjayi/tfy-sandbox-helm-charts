# tfy-sandbox-helm-charts

Sandbox copy of `truefoundry/helm-charts`' release pipeline for end-to-end
testing outside the real repos. Workflows and `scripts/release/` are verbatim
copies (including in-flight changes under review); the charts, component map,
and registry/repo targets are swapped for sandbox stand-ins via repo
variables — no workflow code differs from the source except this repo's
`chart-test.yaml`, which is a simplified lint-only version with the same
trigger and job name.

## Repo map

| Prod | Sandbox |
|---|---|
| truefoundry/helm-charts | DeeAjayi/tfy-sandbox-helm-charts |
| truefoundry/infra-charts | DeeAjayi/tfy-sandbox-infra-charts |
| service repos (11) | DeeAjayi/tfy-sandbox-service |
| truefoundry/tfy-llm-gateway | DeeAjayi/tfy-sandbox-gateway |
| truefoundry/ubermold-base | DeeAjayi/tfy-sandbox-ubermold-base |
| truefoundry/ubermold-truefoundry | DeeAjayi/tfy-sandbox-ubermold-truefoundry |
| JFrog (tfy.jfrog.io) | GHCR (ghcr.io/deeajayi) |

## Trying a release

```
gh workflow run release-start.yml --repo DeeAjayi/tfy-sandbox-helm-charts \
  -f kind=rc -f repositories='[]'
```

Then promote, hotfix, chart_only, etc. exactly as documented in
`scripts/release/README.md`.
e2e Sat Jul  4 17:35:11 WAT 2026
