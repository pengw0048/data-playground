"""`dataplay` — the one-command launcher (PRD §1.1, FR-1.2, P6).

Starts a single server that serves the prebuilt SPA + the API + the engine, and opens the
browser. Zero config: it uses (or creates) a workspace and seeds sample data on first run.
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser


def main() -> None:
    p = argparse.ArgumentParser(prog="dataplay", description="Data Playground — a node canvas for data.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8471)
    p.add_argument("--workspace", default=None, help="working dir for canvases/outputs/plugins (default: CWD)")
    p.add_argument("--data-dir", default=None, help="dataset folder to seed/scan (default: <workspace>/data)")
    p.add_argument("--no-open", dest="open", action="store_false", default=True)
    p.add_argument("--no-seed", dest="seed", action="store_false", default=True)
    args = p.parse_args()

    workspace = os.path.abspath(args.workspace or os.getcwd())
    data_dir = os.path.abspath(args.data_dir or os.path.join(workspace, "data"))
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    # Export BEFORE anything imports kernel.settings (which freezes these from the env at import), so
    # --workspace actually isolates the metadata DB / catalog / dataset-confinement roots — not just
    # the Deps singleton. setdefault: an explicit DP_WORKSPACE/DP_DATA_DIR (or DP_DATABASE_URL) wins.
    os.environ.setdefault("DP_WORKSPACE", workspace)
    os.environ.setdefault("DP_DATA_DIR", data_dir)

    # Refuse a non-loopback bind in open (no-auth) mode unless explicitly allowed — an unauthenticated
    # bind on the LAN is arbitrary code/file access. Set DP_AUTH_SECRET (multi-user auth) or
    # DP_ALLOW_INSECURE_BIND=1 (trusted private network).
    from kernel import auth
    loopback = args.host in ("127.0.0.1", "::1", "localhost", "")
    if not loopback and not auth.auth_enabled() and os.environ.get("DP_ALLOW_INSECURE_BIND") != "1":
        raise SystemExit(
            f"refusing to bind {args.host} with no auth — anyone on the network would get full "
            "code/file access. Set DP_AUTH_SECRET (multi-user auth) or DP_ALLOW_INSECURE_BIND=1 "
            "(trusted private network).")
    if not loopback and not auth.auth_enabled():
        print(f"\n  ⚠  WARNING: serving {args.host} in OPEN mode (no auth) — anyone who can reach this "
              "host has full access. Trust the network/firewall.\n")

    if args.seed:
        from kernel.seed import seed_if_empty
        if seed_if_empty(data_dir):
            print(f"seeded sample datasets → {data_dir}")

    # configure the workspace BEFORE the app imports/builds deps (get_deps is lazy)
    from kernel.deps import set_workspace
    set_workspace(workspace, data_dir)

    import logging
    import uvicorn
    # Emit logs by default (was silent at 'warning') so a failing/dying server leaves a trace —
    # startup, requests, and tracebacks. Level is DP_LOG_LEVEL (info default; debug/warning/error).
    level = os.environ.get("DP_LOG_LEVEL", "info").lower()
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}"
    print(f"\n  Data Playground  →  {url}\n  workspace: {workspace}\n  data:      {data_dir}\n")
    if args.open:
        threading.Timer(1.3, lambda: webbrowser.open(url)).start()
    uvicorn.run("kernel.main:app", host=args.host, port=args.port, log_level=level)


if __name__ == "__main__":
    main()
