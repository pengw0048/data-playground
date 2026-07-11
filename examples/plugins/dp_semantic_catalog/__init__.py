"""Reference plugin — **semantic + hybrid catalog search** via a local embedding model.

Turns the catalog's search box from substring matching into meaning: type "who bought what" and the
`purchases` / `orders` tables surface even if they don't contain those words. It embeds each dataset's
name + folder + description + tags + column names and, at search time, ranks by cosine similarity;
`hybrid` mode fuses that with the lexical results (reciprocal-rank fusion) so exact-name matches still
win when you type an exact name.

It demonstrates the `reg.add_embedder` seam: register a callable `embed(list[str]) -> list[list[float]]`
and the built-in catalog handles storage (a `catalog_embeddings` row per dataset), background reindexing
of existing tables, and the cosine ranking + fusion. Core ships NO embedding model (it's a heavy,
opinionated dependency); this plugin supplies one.

The model is `sentence-transformers` (imported lazily, like the Lance/HF adapters), so the plugin loads
fine without it installed and only errors when it actually tries to embed — at which point search
transparently falls back to lexical. Runs fully locally: the model is downloaded once and executed
on-device, no API key, no per-query egress.

Install: `uv pip install -e 'kernel[semantic]'` (adds the `sentence-transformers` optional extra).
Drop this folder into `<workspace>/plugins/` or install it as a `dataplay.plugins` entry point.
"""

from __future__ import annotations

import logging

log = logging.getLogger("hub")


def _make_embedder(model_id: str):
    """Return `embed(texts) -> list[list[float]]` backed by a lazily-loaded sentence-transformers model.
    The model is loaded once on first call and reused (embedding thousands of short strings is cheap)."""
    state: dict = {}

    def embed(texts: list[str]) -> list[list[float]]:
        model = state.get("model")
        if model is None:
            try:
                from sentence_transformers import SentenceTransformer  # lazy — only when embedding runs
            except ModuleNotFoundError as e:
                raise ModuleNotFoundError(
                    "semantic search needs sentence-transformers — run: uv pip install -e 'kernel[semantic]'"
                ) from e
            model = state["model"] = SentenceTransformer(model_id)
        return [list(map(float, v)) for v in model.encode(list(texts), normalize_embeddings=True)]

    return embed


def register(reg) -> None:
    # reg.config reads dataplay.toml [[config]]: a UI value wins, else the env var, else the default.
    enabled = reg.config("enabled", True)
    if enabled in (False, "false", "0", "no", "off"):
        return  # disabled → no-op; the built-in lexical + faceted search stands
    model_id = reg.config("model", "sentence-transformers/all-MiniLM-L6-v2")
    reg.add_embedder(_make_embedder(model_id), model=model_id)
    log.info("dp_semantic_catalog: semantic search enabled (model=%s)", model_id)
