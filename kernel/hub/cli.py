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
    from hub import mcp, metadb
    # Migrate the metadata DB BEFORE set_workspace builds deps + seeds the catalog, so the seed's
    # catalog_entries write-throughs land instead of failing against a not-yet-created schema (which
    # would drop the seeded datasets on first read). build_server's init_db then no-ops.
    with contextlib.redirect_stdout(sys.stderr):
        metadb.init_db()
        set_workspace(os.environ["DP_WORKSPACE"], os.environ["DP_DATA_DIR"])
        server = mcp.build_server(base_url=args.base_url, user_id=args.user)
    print("Data Playground MCP server ready (stdio).", file=sys.stderr)
    mcp.serve_stdio(server)


def _load_canvas_graph(canvas_ref: str):
    """Resolve a saved canvas by id (exact) or by name (unique match) → (Graph, canvas_id). Exits with a
    clear message if it's missing or the name is ambiguous."""
    import json

    from sqlalchemy import select

    from hub import metadb
    from hub.models import Graph
    with metadb.session() as s:
        c = s.get(metadb.Canvas, canvas_ref)
        if c is None:  # not an id → try a unique name
            matches = s.scalars(select(metadb.Canvas).where(metadb.Canvas.name == canvas_ref)).all()
            if len(matches) > 1:
                raise SystemExit(f"'{canvas_ref}' names {len(matches)} canvases — pass the canvas id instead")
            c = matches[0] if matches else None
        if c is None:
            raise SystemExit(f"no canvas with id or name '{canvas_ref}'")
        return Graph.model_validate(json.loads(c.doc)), c.id


def _apply_params(graph, params: dict, node: str | None = None) -> None:
    """Substitute ${NAME} tokens in the CONFIG values of a canvas with the given params — headless
    parameterization for cron/CI (e.g. a source uri / filter predicate / sql templated per run). Mutates
    the graph in place. ANY ${...} is a token: it's bound to a param or it fails LOUDLY — never left as a
    silent literal (which would e.g. read a path named "${date}"). Only the nodes that actually run are
    considered — the whole canvas, or `node`'s upstream cone when a single node is targeted — so an unbound
    token in an unrelated branch doesn't block a targeted run."""
    import re

    from hub import graph as g
    tok = re.compile(r"\$\{([^}]+)\}")  # [^}]+ so names with -/./space bind too, and are never silently kept
    unbound: set[str] = set()

    def sub(v):
        if isinstance(v, str):  # set.add returns None → the `or` keeps the original token for the error
            return tok.sub(lambda m: params[m.group(1)] if m.group(1) in params
                           else (unbound.add(m.group(1)) or m.group(0)), v)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [sub(x) for x in v]
        return v

    scope = g.upstream_chain(graph, node) if node else graph.nodes  # only the nodes that will actually run
    for n in scope:
        cfg = n.data.get("config") if isinstance(n.data, dict) else None
        if isinstance(cfg, dict):  # substitute only within config (leave title/id/type untouched)
            n.data = {**n.data, "config": sub(cfg)}
    if unbound:
        raise SystemExit(f"unbound canvas parameter(s): {', '.join(sorted(unbound))} — pass --param NAME=value")


_CANCEL_ACK_TIMEOUT_S = 10.0


def _cancel_and_wait(owner, run_id: str, status, metadb, timeout_s: float = _CANCEL_ACK_TIMEOUT_S):
    """Request cancellation and wait a bounded interval for a truthful terminal acknowledgement."""
    import time
    from hub.backends import stop_acknowledged

    cancel_error = None
    try:
        requested = owner.cancel(run_id)
        if requested is not None:
            status = requested
    except Exception as e:  # noqa: BLE001 — still poll durable state; the request may have reached the worker
        cancel_error = f"{type(e).__name__}: {e}"
    deadline = time.monotonic() + max(0.0, timeout_s)
    while not stop_acknowledged(owner, status) and time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            status = owner.status(run_id)
        except KeyError:
            persisted = metadb.get_run_state(run_id)
            if persisted is not None:
                from hub.models import RunStatus
                status = RunStatus(**persisted)
        except Exception as e:  # noqa: BLE001 — a durable row may still acknowledge a remote owner
            cancel_error = cancel_error or f"{type(e).__name__}: {e}"
            persisted = metadb.get_run_state(run_id)
            if persisted is not None:
                from hub.models import RunStatus
                status = RunStatus(**persisted)
    return status, stop_acknowledged(owner, status), cancel_error


def _headless_run(deps, canvas_ref: str, node: str | None, timeout_s: float, as_json: bool,
                  uid: str | None = None, params: dict | None = None) -> int:
    """Run a saved canvas to completion in-process (no browser) and return a shell exit code:
    0=done, 1=failed, 2=cancelled, 124=timeout, 130=SIGINT. Timeout/SIGINT request cancellation and wait
    for bounded terminal acknowledgement. Reuses the exact start_run path the UI + MCP use, so
    placement/gating/ownership are identical; `confirmed=True` because a headless invocation is itself
    the confirmation for a full pass. `node` targets one node (its upstream cone); default = whole canvas.
    Runs the run + poll with stdout diverted to stderr so a node's own print() can't corrupt the summary
    / --json; the summary is printed to real stdout after."""
    import contextlib
    import json
    import time

    from fastapi import HTTPException

    from hub import metadb
    from hub.models import RunStatus
    from hub.routers.runs import start_run
    graph, cid = _load_canvas_graph(canvas_ref)
    _apply_params(graph, params or {}, node)  # ${NAME} → --param values (raises SystemExit on an unbound token)
    uid = uid or metadb.DEFAULT_USER_ID
    abort_reason = None
    abort_code = None
    cancel_acknowledged = False
    cancel_error = None
    with contextlib.redirect_stdout(sys.stderr):  # protect stdout during the run (node print() → stderr)
        try:
            status, owner = start_run(deps, graph, node, uid, confirmed=True)
        except HTTPException as e:  # invalid/cyclic graph, size-gate, auth 404 (in-process path)
            raise SystemExit(f"cannot run canvas '{cid}': {e.detail}")
        except (RuntimeError, OSError) as e:  # kernel failed to start / became unreachable (default backend)
            raise SystemExit(f"cannot run canvas '{cid}': {e}")
        run_id = status.run_id
        deadline = time.monotonic() + timeout_s
        last_reap = time.monotonic()
        try:
            while status.status in ("queued", "running"):
                if time.monotonic() > deadline:
                    abort_reason = f"timeout after {timeout_s:g}s"
                    abort_code = 124
                    break
                time.sleep(0.25)
                # No hub process runs the periodic reaper here, so a run whose kernel died would otherwise poll
                # until --timeout. Reap dead-kernel runs ourselves (only_kernel_runs=True leaves a live kernel's
                # run — and any in-process/subprocess run — untouched) so a crashed kernel fails fast.
                if time.monotonic() - last_reap > 5:
                    try:
                        metadb.reap_orphaned_runs(only_kernel_runs=True)
                    except Exception:  # noqa: BLE001 — reaping is best-effort; never crash the run on it
                        pass
                    last_reap = time.monotonic()
                try:
                    status = owner.status(run_id)
                except KeyError:  # evicted from the owner's in-memory ring → read the durable state
                    persisted = metadb.get_run_state(run_id)
                    if persisted is None:
                        break
                    status = RunStatus(**persisted)
        except KeyboardInterrupt:
            abort_reason = "interrupted by SIGINT"
            abort_code = 130
        if abort_code is not None:
            status, cancel_acknowledged, cancel_error = _cancel_and_wait(owner, run_id, status, metadb)
    if abort_code is not None:
        state = status.status
        if cancel_acknowledged:
            detail = f"stop acknowledged with terminal status {state}"
        else:
            detail = (f"cancellation not acknowledged within {_CANCEL_ACK_TIMEOUT_S:g}s; "
                      f"last status {state}")
        if cancel_error:
            detail += f"; cancel error: {cancel_error}"
        print(f"{abort_reason}: run {run_id}; {detail}", file=sys.stderr)
        if as_json:
            payload = status.model_dump()
            payload.update({"exit_reason": abort_reason, "cancel_acknowledged": cancel_acknowledged})
            print(json.dumps(payload, default=str))
        return abort_code
    if as_json:
        print(json.dumps(status.model_dump(), default=str))
    else:
        head = f"canvas {cid}  run {run_id}  →  {status.status.upper()}"
        if status.total_rows is not None or status.ms is not None:
            head += f"  ({status.total_rows if status.total_rows is not None else '?'} rows"
            head += f", {status.ms} ms)" if status.ms is not None else ")"
        if status.output_table:
            head += f"  →  {status.output_table}"
        print(head)
        for p in (status.per_node or []):
            line = f"  {p.node_id}: {p.status}"
            if p.rows is not None:
                line += f" ({p.rows} rows)"
            if p.error:
                line += f" — {p.error}"
            print(line)
        if status.error:
            print(f"error: {status.error}", file=sys.stderr)
    return {"done": 0, "failed": 1, "cancelled": 2}.get(status.status, 1)


def _run_canvas(argv: list[str]) -> None:
    """`dataplay run <canvas>` — run a saved canvas to completion headless (cron / CI / scripting), print
    a summary, and exit non-zero on failure. Shares the workspace/DB with the web app + MCP server."""
    import contextlib
    import logging

    p = argparse.ArgumentParser(prog="dataplay run", description="Run a saved canvas to completion (headless).")
    p.add_argument("canvas", help="canvas id, or a unique canvas name, to run")
    p.add_argument("--node", default=None, help="run only this node id + its upstream (default: the whole canvas)")
    p.add_argument("--workspace", default=None, help="working dir (canvases/outputs/plugins); default CWD")
    p.add_argument("--data-dir", default=None, help="dataset folder (default: <workspace>/data)")
    p.add_argument("--no-seed", dest="seed", action="store_false", default=True)
    p.add_argument("--user", default=None, help="run as this user id (default: the local user). In auth "
                                                "mode this must own/share the canvas (mirrors `dataplay mcp`).")
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="bind a ${NAME} token in the canvas's configs (repeatable) — e.g. --param date=2026-07-12")
    p.add_argument("--timeout", type=float, default=3600.0,
                   help="max seconds to wait; cancels the run and exits 124 on timeout (default 3600)")
    p.add_argument("--json", dest="as_json", action="store_true", help="print the final RunStatus as JSON")
    args = p.parse_args(argv)

    params: dict[str, str] = {}
    for kv in args.param:
        k, sep, v = kv.partition("=")
        if not sep or not k.strip():
            raise SystemExit(f"--param must be NAME=VALUE, got '{kv}'")
        params[k.strip()] = v

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,  # keep stdout clean for the summary / --json
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # divert set_workspace's plugin-discovery prints to stderr so stdout is only the run summary (parseable)
    with contextlib.redirect_stdout(sys.stderr):
        _prepare_workspace(args.workspace, args.data_dir, args.seed)
        from hub import metadb
        metadb.init_db()  # schema to head before Deps builds + registers the catalog (fresh-DB first run)
        from hub.deps import set_workspace
        deps = set_workspace(os.environ["DP_WORKSPACE"], os.environ["DP_DATA_DIR"])
    raise SystemExit(_headless_run(deps, args.canvas, args.node, args.timeout, args.as_json, args.user, params))


def _run_seed_catalog(argv: list[str]) -> None:
    """`dataplay seed-catalog --count N` — register N synthetic datasets across a folder/tag/owner
    space, so you can see the catalog's browse/search/facet UX at scale without real files. Writes to
    the same workspace DB the server reads, so they show up live in the Tables view.
    `--remove` deletes everything a previous seed with the same --prefix created."""
    p = argparse.ArgumentParser(prog="dataplay seed-catalog", description="Seed synthetic catalog entries (demo/scale).")
    p.add_argument("--count", type=int, default=1000, help="how many synthetic datasets to register")
    p.add_argument("--workspace", default=None, help="working dir (default: CWD)")
    p.add_argument("--data-dir", default=None)
    p.add_argument("--prefix", default="demo", help="uri + name prefix (also the top-level folder)")
    p.add_argument("--remove", action="store_true", help="remove previously-seeded entries for --prefix instead of seeding")
    args = p.parse_args(argv)
    _prepare_workspace(args.workspace, args.data_dir, seed=False)

    from hub import metadb
    metadb.init_db()
    if args.remove:
        n = metadb.catalog_delete_prefix(f"mem://{args.prefix}/")
        print(f"removed {n} seeded catalog entries (prefix '{args.prefix}').", file=sys.stderr)
        return
    owners = ["data-platform", "growth", "ml-research", "finance", "ops"]
    modalities = ["images", "video", "audio", "text", "tabular"]
    entries = []
    for i in range(max(0, args.count)):
        modality = modalities[i % len(modalities)]
        folder = f"{args.prefix}/{modality}/{'raw' if i % 3 == 0 else 'curated'}"
        tags = [modality, "gold" if i % 4 == 0 else "silver"] + (["pii"] if i % 11 == 0 else [])
        name = f"{args.prefix}_{modality}_{i:05d}"
        cols = ["id", "created_at", f"{modality}_ref"] + (["embedding"] if i % 2 == 0 else [])
        entries.append({"uri": f"mem://{args.prefix}/{i}", "name": name, "doc": {
            "id": f"tbl_{name}", "name": name, "uri": f"mem://{args.prefix}/{i}", "folder": folder,
            "tags": tags, "owner": owners[i % len(owners)], "rowCount": (i + 1) * 137,
            "description": f"synthetic {modality} dataset #{i} for catalog demo",
            "columns": [{"name": c, "type": "VARCHAR"} for c in cols],
        }})
    n = metadb.catalog_bulk_seed(entries)
    print(f"seeded {n} synthetic catalog entries (prefix '{args.prefix}'). Remove with: "
          f"dataplay seed-catalog --remove --prefix {args.prefix}", file=sys.stderr)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "mcp":
        return _run_mcp(argv[1:])
    if argv and argv[0] == "run":
        return _run_canvas(argv[1:])
    if argv and argv[0] == "seed-catalog":
        return _run_seed_catalog(argv[1:])
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

    # Migrate the metadata DB BEFORE building deps. set_workspace eagerly constructs Deps, which seeds
    # the catalog and write-throughs each seeded dataset to catalog_entries; if the schema isn't there
    # yet those writes fail and the seed is silently dropped on the first catalog read (hub.main's own
    # init_db at import runs too late — after the seed). Idempotent: hub.main re-runs it harmlessly.
    from hub import metadb
    metadb.init_db()
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
    from hub.settings import settings
    uvicorn.run("hub.main:app", host=args.host, port=args.port, log_level=level,
                ws_max_size=settings.max_body_bytes)  # SEC-10: explicit/tunable WebSocket frame cap


if __name__ == "__main__":
    main()
