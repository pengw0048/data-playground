# Security Policy

## Reporting a vulnerability

Please report security issues privately — **do not open a public issue** for an exploitable bug.

- Use GitHub's **"Report a vulnerability"** (Security → Advisories) on this repository, or
- email the maintainers listed in the repository's GitHub profile.

Include a description, affected version/commit, and a minimal reproduction if possible. We aim to
acknowledge within a few days and will coordinate a fix and disclosure timeline with you.

## Scope & threat model

Data Playground has two modes, with **different** security expectations — please frame reports
against the right one:

- **Open single-user mode** (default; no `DP_AUTH_SECRET`) is a *trusted local tool*. It runs your
  own code and reads your own files with no authentication or path confinement by design. Running it
  bound to a non-loopback interface without auth is out of scope (the CLI refuses this unless you
  opt in).
- **Multi-user mode** (`DP_AUTH_SECRET` set, e.g. the Docker Compose deployment) is where auth,
  per-user isolation, and `DP_DATASET_ROOTS` confinement apply. Bypasses of those controls are
  in scope. Note the transform/section code sandbox is a *soft* guard (crash/DoS isolation), not a
  multi-tenant jail — real tenant isolation needs OS-level sandboxing.

## Supported versions

This project is pre-1.0; only the latest `main` is supported. Fixes land on `main`.
