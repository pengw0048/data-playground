# Security policy

## Report a security-related bug

For a reproducible security-related bug, open a GitHub issue with the affected version or commit, a
minimal sanitized reproduction, impact, and relevant deployment details. Do not post credentials,
tokens, private data, or a reproduction that exposes a live system.

Data Playground does not currently operate a private vulnerability-reporting intake or publish an
acknowledgement, fix, disclosure, or backport SLA. Maintainers assess reports through the ordinary issue
workflow and may ask for a safer reproduction when needed.

## Supported trust boundary

Data Playground supports a local workstation and a shared service operated by a trusted team. It does
not support mutually distrusting tenants or provide a hostile-code sandbox. The canonical deployment,
trust, and operator boundary is [Supported deployments and trust model](../docs/SUPPORT.md).

In particular, users who can run arbitrary Python or section code, installed plugins and their
dependencies, execution workers, and workspace/storage administrators are trusted with workspace data
and process capabilities. A plugin, subprocess, container, dataset-root policy, or PodSpawner does not
make that code a tenant-isolation boundary.

Ordinary application controls within the supported profiles remain in scope. Please report, for example,
an authentication or authorization bypass, session or revocation failure, cross-origin or collaboration
WebSocket exposure, a supported declarative path/SQL-policy bypass, plaintext-secret disclosure,
selected-credential fallback, or redaction failure. If you are unsure whether behavior crosses the
boundary, open a sanitized issue that describes the observed behavior and affected path.

The checked-in Compose file is a local, loopback HTTP reference. A trusted-team service must supply its
own TLS-terminating reverse proxy and configure only its real trusted-proxy addresses. See the
[transport and deployment boundary](../docs/SUPPORT.md) rather than treating Compose as a shared-service
production manifest.

## Current automated evidence

The workflow definitions are the current source of truth; [CI and release gates](../docs/CI.md) owns the
complete trigger and release contract.

| Evidence | Current workflow boundary |
| --- | --- |
| Dependency changes | `dependency-review.yml` compares the Python and web lockfiles with the pull-request base. Its checked-in policy decides whether a newly introduced finding blocks the change; this document does not keep an advisory allowlist or count. |
| Static analysis and secret detection | `codeql.yml` and `secret-scan.yml` run for pull requests and integrated `main`; CodeQL also has a weekly health run. Both can be run manually. |
| Application-image findings | `image-scan.yml` is path-gated for relevant pull requests and also runs weekly or manually. It is separate from the tag-triggered release workflow. |
| Release candidate | A `v*` tag starts `release.yml`. It records the immutable candidate SHA and reruns core CI, CodeQL, and Gitleaks on that exact commit before publication, alongside the documented release gates. A later passing `main` run is not substituted for tag evidence. |

Repository-native secret scanning and push protection are GitHub administration settings, not repository
YAML. They are useful additional controls when enabled, but this policy does not claim their current
setting state.

## Versions and fixes

Use the [latest published release](https://github.com/pengw0048/data-playground/releases/latest) when
reporting an affected released build. Current maintenance is a single `main` line: security fixes land on
`main` and are available in a later tagged release when maintainers publish one. There are no maintained
patch or release branches, so older tags have no promised security backports. Test against current `main`
when possible and include the released version in a report when it is affected.
