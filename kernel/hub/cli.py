"""`dataplay` — the one-command launcher.

Starts a single server that serves the prebuilt SPA + the API + the engine, and opens the
browser. Zero config: it uses (or creates) a workspace and seeds sample data on first run.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser


def _prepare_workspace(workspace: str | None, data_dir: str | None, seed: bool) -> tuple[str, str]:
    """Resolve + create the workspace/data dirs and freeze them into the env BEFORE hub.settings is
    imported (it reads these at import), optionally seeding sample data. Shared by the server and the
    `mcp` subcommand so both isolate the same metadata DB / catalog / dataset roots."""
    workspace = os.path.abspath(workspace or os.getcwd())
    data_dir = os.path.abspath(data_dir or os.path.join(workspace, "data"))
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("DP_WORKSPACE", workspace)
    os.environ.setdefault("DP_DATA_DIR", data_dir)
    if seed:
        from hub.seed import seed_if_empty
        if seed_if_empty(data_dir):
            print(f"seeded sample datasets → {data_dir}", file=sys.stderr)
    return workspace, data_dir


def _run_mcp(argv: list[str]) -> None:
    """`dataplay mcp` — expose the workspace to an MCP client (e.g. a user's own Claude Code) over
    stdio. Talks the same catalog/canvas/engine as the web app (shared workspace DB), so a pipeline
    the client builds shows up in the browser. stdout is the JSON-RPC channel and must stay clean, so
    everything noisy (seeding notice, plugin-load prints, logs) is forced to stderr here + in the
    serve loop."""
    import contextlib
    import logging

    p = argparse.ArgumentParser(prog="dataplay mcp", description="Serve the workspace over MCP (stdio).")
    p.add_argument("--workspace", default=None, help="working dir (canvases/outputs/plugins); default CWD")
    p.add_argument("--data-dir", default=None, help="dataset folder to scan (default: <workspace>/data)")
    p.add_argument("--base-url", default=None, help="URL the web app is served at, for canvas links "
                                                    "(default $DP_BASE_URL or http://127.0.0.1:8471)")
    p.add_argument("--user", default=None, help="act as this user id (default: the local user)")
    p.add_argument("--no-seed", dest="seed", action="store_false", default=True)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # logs to stderr, never the protocol stream
    _prepare_workspace(args.workspace, args.data_dir, args.seed)

    from hub.deps import set_workspace
    from hub import mcp
    # Build deps (plugin discovery prints to stdout) BEFORE the stdio loop, with stdout diverted to
    # stderr so none of it lands on the protocol channel.
    with contextlib.redirect_stdout(sys.stderr):
        set_workspace(os.environ["DP_WORKSPACE"], os.environ["DP_DATA_DIR"])
        server = mcp.build_server(base_url=args.base_url, user_id=args.user)
    print("Data Playground MCP server ready (stdio).", file=sys.stderr)
    mcp.serve_stdio(server)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "mcp":
        return _run_mcp(argv[1:])
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
    # Export BEFORE anything imports hub.settings (which freezes these from the env at import), so
    # --workspace actually isolates the metadata DB / catalog / dataset-confinement roots — not just
    # the Deps singleton. setdefault: an explicit DP_WORKSPACE/DP_DATA_DIR (or DP_DATABASE_URL) wins.
    os.environ.setdefault("DP_WORKSPACE", workspace)
    os.environ.setdefault("DP_DATA_DIR", data_dir)

    # Refuse a non-loopback bind in open (no-auth) mode unless explicitly allowed — an unauthenticated
    # bind on the LAN is arbitrary code/file access. Set DP_AUTH_SECRET (multi-user auth) or
    # DP_ALLOW_INSECURE_BIND=1 (trusted private network).
    from hub import auth
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
        from hub.seed import seed_if_empty
        if seed_if_empty(data_dir):
            print(f"seeded sample datasets → {data_dir}")

    # configure the workspace BEFORE the app imports/builds deps (get_deps is lazy)
    from hub.deps import set_workspace
    set_workspace(workspace, data_dir)

    import logging
    import uvicorn
    # Emit logs by default (was silent at 'warning') so a failing/dying server leaves a trace —
    # startup, requests, and tracebacks. Level is DP_LOG_LEVEL (info default; debug/warning/error).
    level = os.environ.get("DP_LOG_LEVEL", "info").lower()
    if level == "warn":
        level = "warning"  # Python logging accepts 'warn'; uvicorn does not
    if level not in ("critical", "error", "warning", "info", "debug", "trace"):
        level = "info"  # unknown value → don't crash uvicorn.run(log_level=...)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}"
    print(f"\n  Data Playground  →  {url}\n  workspace: {workspace}\n  data:      {data_dir}\n")
    if args.open:
        threading.Timer(1.3, lambda: webbrowser.open(url)).start()
    uvicorn.run("hub.main:app", host=args.host, port=args.port, log_level=level)


if __name__ == "__main__":
    main()
