"""The tablet's library as the MCP tools see it: every document and folder with its path, lookup
by UUID, path or Ink Bridge ID, and an optional scope of folders the tools are confined to.

Built from `CloudTransport.entries()` (metadata and .content of every entry, no payloads). Paths
are the folder names from the top level down, "/Work/Meeting notes"; trashed items live under
"/Trash". A scope is a list of folder paths: what lies outside them (the trash included) cannot
be found, listed, or written to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .transport.base import LibraryEntry

TRASH = "trash"
TRASH_PATH = "/Trash"


class NotFound(LookupError):
    pass


class OutOfScope(PermissionError):
    pass


@dataclass
class Item:
    uuid: str
    version: str
    metadata: dict
    content: dict
    path: str = ""

    @property
    def name(self) -> str:
        return self.metadata.get("visibleName") or self.uuid

    @property
    def is_folder(self) -> bool:
        return self.metadata.get("type") == "CollectionType"

    @property
    def parent(self) -> str:
        return self.metadata.get("parent") or ""

    @property
    def trashed(self) -> bool:
        return self.path == TRASH_PATH or self.path.startswith(TRASH_PATH + "/")

    @property
    def file_type(self) -> str | None:
        return None if self.is_folder else (self.content.get("fileType") or None)

    @property
    def tags(self) -> list[str]:
        return [t.get("name") if isinstance(t, dict) else str(t) for t in self.content.get("tags") or []]

    @property
    def page_count(self) -> int | None:
        pages = (self.content.get("cPages") or {}).get("pages")
        if pages:
            return sum(1 for p in pages if not p.get("deleted"))
        if self.content.get("pages"):
            return len(self.content["pages"])
        return self.content.get("pageCount") or None

    @property
    def last_modified(self) -> float | None:
        return _seconds(self.metadata.get("lastModified"))


def normalize(path: str) -> str:
    """"Work/Notes/" -> "/Work/Notes"; "" and "/" -> "/"."""
    parts = [p for p in path.strip().split("/") if p]
    return "/" + "/".join(parts)


def split(path: str) -> tuple[str, str]:
    """"/Work/Notes" -> ("/Work", "Notes")."""
    path = normalize(path)
    head, _, name = path.rpartition("/")
    return head or "/", name


def within(path: str, folder: str) -> bool:
    return folder == "/" or path == folder or path.startswith(folder.rstrip("/") + "/")


class Library:
    def __init__(self, entries: Iterable[LibraryEntry], scope: Iterable[str] = (),
                 tracked: dict[str, dict[str, Any]] | None = None):
        self.items = {e.uuid: Item(e.uuid, e.version, e.metadata or {}, e.content or {}) for e in entries}
        self.scope = [normalize(s) for s in scope if normalize(s) != "/"]
        self.tracked = tracked or {}  # rm UUID -> Ink Bridge doc summary
        for item in self.items.values():
            item.path = self._path(item)
        self.by_path: dict[str, list[Item]] = {}
        self.kids: dict[str, list[Item]] = {}
        for item in self.items.values():
            self.by_path.setdefault(item.path, []).append(item)
            self.kids.setdefault(item.parent, []).append(item)

    def _path(self, item: Item) -> str:
        names, seen, node = [item.name], {item.uuid}, item
        while True:
            parent = node.parent
            if parent == TRASH:
                return TRASH_PATH + "/" + "/".join(reversed(names))
            node = self.items.get(parent)
            if not parent or node is None or node.uuid in seen:  # top level, orphan, or a cycle
                return "/" + "/".join(reversed(names))
            seen.add(node.uuid)
            names.append(node.name)

    # --- scope -------------------------------------------------------------------------------

    def path_in_scope(self, path: str) -> bool:
        path = normalize(path)
        if not self.scope:
            return True
        return any(within(path, s) for s in self.scope)

    def in_scope(self, item: Item) -> bool:
        if not self.scope:
            return True
        return not item.trashed and self.path_in_scope(item.path)

    def check_path(self, path: str) -> str:
        path = normalize(path)
        if not self.path_in_scope(path):
            raise OutOfScope(f"{path} is outside the folders this connector may use: {', '.join(self.scope)}")
        return path

    # --- lookup ------------------------------------------------------------------------------

    def resolve(self, ref: str) -> Item:
        """A document or folder by UUID, path ("/Work/Notes"), Ink Bridge ID (rm-xxxxxx) or a unique name."""
        ref = ref.strip()
        if not ref:
            raise NotFound("empty document reference")
        candidates: list[Item] = []
        if ref in self.items:
            candidates = [self.items[ref]]
        elif ref.startswith("rm-") or ref.startswith("ext-"):
            candidates = [self.items[u] for u, d in self.tracked.items()
                          if u in self.items and (d.get("doc_id") == ref or d.get("doc_id", "").split(".")[0] == ref)]
        if not candidates:
            candidates = self.by_path.get(normalize(ref), [])
        if not candidates and "/" not in ref.strip("/"):
            candidates = [i for i in self.items.values() if i.name == ref]
        candidates = [c for c in candidates if self.in_scope(c)]
        if not candidates:
            raise NotFound(f"nothing found for {ref!r}; list_documents shows what exists")
        if len(candidates) > 1:
            listing = "; ".join(f"{c.path} ({c.uuid})" for c in candidates[:10])
            raise NotFound(f"{ref!r} is ambiguous, use a UUID: {listing}")
        return candidates[0]

    def folder(self, path: str) -> Item | None:
        """The folder at `path`, None for the top level. Raises NotFound when it does not exist."""
        path = normalize(path)
        if path == "/":
            return None
        found = [i for i in self.by_path.get(path, []) if i.is_folder]
        if not found:
            raise NotFound(f"no folder {path}")
        return found[0]

    def children(self, folder: Item | None, recursive: bool = False) -> list[Item]:
        if folder is None:
            pool = [i for i in self.items.values() if not i.trashed]
            if not recursive:
                pool = [i for i in pool if not i.parent or i.parent not in self.items]
        elif recursive:
            pool, stack, seen = [], [folder.uuid], {folder.uuid}
            while stack:
                for kid in self.kids.get(stack.pop(), []):
                    if kid.uuid not in seen:
                        seen.add(kid.uuid)
                        pool.append(kid)
                        stack.append(kid.uuid)
        else:
            pool = list(self.kids.get(folder.uuid, []))
        return [i for i in pool if self.in_scope(i)]

    def trash(self) -> list[Item]:
        return [] if self.scope else list(self.kids.get(TRASH, []))

    def roots(self) -> list[Item]:
        """With a scope: the scope folders that exist; the top level otherwise."""
        if not self.scope:
            return self.children(None)
        return [i for s in self.scope for i in self.by_path.get(s, []) if i.is_folder]

    # --- payloads ----------------------------------------------------------------------------

    def summary(self, item: Item) -> dict[str, Any]:
        out: dict[str, Any] = {
            "uuid": item.uuid,
            "name": item.name,
            "path": item.path,
            "type": "folder" if item.is_folder else "document",
        }
        if item.is_folder:
            out["items"] = len(self.kids.get(item.uuid, []))
        else:
            out["file_type"] = item.file_type
            out["pages"] = item.page_count
            out["last_opened"] = _iso(_seconds(item.metadata.get("lastOpened")))
        out["last_modified"] = _iso(item.last_modified)
        if item.metadata.get("pinned"):
            out["pinned"] = True
        if item.tags:
            out["tags"] = item.tags
        if item.uuid in self.tracked:
            out["inkbridge"] = self.tracked[item.uuid]
        return out


def _seconds(value: Any) -> float | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 0 else None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
