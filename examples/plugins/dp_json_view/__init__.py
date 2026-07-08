"""Reference plugin — a **capability that adds a viewer tab**, with NO frontend code.

The full capability seam in one plugin: a `detect(col)` tags matching columns (wired by
`reg.add_capability` → the engine's `tag_columns`), and a declarative `viewer` makes the SPA render a
generic tab for them — the same "declare a schema, the core renders it" idea as `NodeSpec` for node
cards. This capability tags string columns whose NAME looks like a JSON document
(json/meta/payload/attributes/props/spec/config) and declares `viewer = {"kind": "json"}`, so the SPA
shows a **JSON** tab that pretty-prints those cells. No React ships from here: `deps.info()` surfaces the
viewer in `KernelInfo.capability_views`, and the frontend registers a generic tab keyed by `viewer.kind`.

`viewer.kind` is one of the SPA's generic renderers (today: `grid` = media/image grid, `json` =
pretty-printed cell). Drop this folder into `<workspace>/plugins/`.
"""

from __future__ import annotations

import re

_JSON_NAME = re.compile(r"(json|meta|payload|attributes|props|spec|config)", re.I)
_TEXTUAL = {"varchar", "string", "text", "json"}


class JsonViewCapability:
    id = "json-doc"
    label = "JSON"
    viewer = {"kind": "json"}  # → the SPA's generic JSON-cell renderer (no frontend code needed)

    def detect(self, col) -> bool:
        return col.type.lower() in _TEXTUAL and bool(_JSON_NAME.search(col.name))


def register(reg) -> None:
    reg.add_capability(JsonViewCapability())  # detector tags columns + the viewer adds the tab
