"""Shield, from any Python AI.

Shield is the app on his phone that holds what all his AIs know and make:
folders, notes, files, links, the same tree he browses by hand. This is the
whole surface an AI needs:

    from shield_client import Shield

    shield = Shield("http://100.x.y.z:8791", token="…", agent="my-ai")
    shield.search("passport")                       # [Item, …]
    shield.save_note("/Research/Goa", "Hotels", "Taj is ₹8,400 a night")
    shield.upload("/Research/Goa", "map.png", png_bytes)
    shield.read(item_id)                            # the text of a note or text file
    shield.download(item_id)                        # a file's bytes
    shield.tag(item_id, "travel")
    shield.move(item_id, "/Archive")

`agent` is who you are. Everything you add or change is marked with it, so he
can see in Shield exactly what each of his AIs did. Pick one name per AI and
keep it.

Nothing is ever deleted outright through here: `trash` moves to the Trash,
which he empties himself.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import requests

__all__ = ["Shield", "Item", "ShieldError"]


class ShieldError(RuntimeError):
    """Shield said no; `status` is the HTTP status, the message says why."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Shield {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class Item:
    id: str
    kind: str                  # folder | note | file | link
    name: str
    parent: str | None
    mime: str = ""
    size: int = 0
    tags: list[str] = field(default_factory=list)
    starred: bool = False
    created_by: str = ""
    updated_by: str = ""
    created: int = 0
    updated: int = 0
    text: str | None = None    # a note's text, a link's address, a text file's contents
    preview: str | None = None
    about: str | None = None   # what an AI found out it is, and what matters in it
    trashed: int | None = None  # when it went to the Trash; None while it is not there

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Item:
        return cls(
            id=data["id"], kind=data["kind"], name=data["name"], parent=data.get("parent"),
            mime=data.get("mime", ""), size=int(data.get("size", 0)),
            tags=list(data.get("tags", [])), starred=bool(data.get("starred")),
            created_by=data.get("created_by", ""), updated_by=data.get("updated_by", ""),
            created=int(data.get("created", 0)), updated=int(data.get("updated", 0)),
            text=data.get("text"), preview=data.get("preview"), about=data.get("about"),
            trashed=data.get("trashed"))


class Shield:
    def __init__(self, url: str, token: str, agent: str, timeout: float = 20.0,
                 session: requests.Session | None = None):
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._http = session or requests.Session()
        self._http.headers.update({"Authorization": f"Bearer {token}",
                                   "X-Shield-Agent": agent})

    # ── reading ─────────────────────────────────────────────────────────────
    def health(self) -> dict:
        return self._call("GET", "/v1/health")

    def list(self, folder: str = "/") -> list[Item]:
        """What is in a folder, by path ("/Research/Goa") or id."""
        params = {"path": folder} if folder.startswith("/") else {"parent": folder}
        if folder == "/":
            params = {}
        return [Item.from_json(i) for i in self._call("GET", "/v1/items", params=params)["items"]]

    def get(self, item: str) -> Item:
        """One item by id or path, with its text."""
        if item.startswith("/"):
            return Item.from_json(self._call("GET", "/v1/resolve", params={"path": item}))
        return Item.from_json(self._call("GET", f"/v1/items/{item}"))

    def read(self, item: str) -> str:
        """The text of a note, a link's address, or a text file's contents."""
        return self.get(item).text or ""

    def download(self, item: str) -> bytes:
        found = self.get(item)
        response = self._http.get(f"{self._url}/v1/items/{found.id}/content", timeout=self._timeout)
        self._check(response)
        return response.content

    def search(self, query: str = "", *, kind: str | None = None, tag: str | None = None,
               starred: bool = False, by: str | None = None, limit: int = 50) -> list[Item]:
        """Names, note text, text inside files and tags; every word must match
        (as a prefix). `by="ai"` for what any AI made, or one AI's name."""
        params: dict[str, Any] = {"q": query, "limit": limit}
        if kind:
            params["kind"] = kind
        if tag:
            params["tag"] = tag
        if starred:
            params["starred"] = 1
        if by:
            params["by"] = by
        return [Item.from_json(i) for i in self._call("GET", "/v1/search", params=params)["items"]]

    def history(self, item: str) -> list[dict]:
        return self._call("GET", f"/v1/items/{self.get(item).id}/history")["history"]

    def activity(self, limit: int = 100, by: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if by:
            params["by"] = by
        return self._call("GET", "/v1/activity", params=params)["activity"]

    def tags(self) -> dict[str, int]:
        return {t["tag"]: t["count"] for t in self._call("GET", "/v1/tags")["tags"]}

    # ── writing ─────────────────────────────────────────────────────────────
    def folder(self, path: str) -> Item:
        """The folder at a path, made (with its parents) if it is not there."""
        if self._exists(path):
            return self.get(path)
        parent, _, name = path.rstrip("/").rpartition("/")
        return self._create({"path": parent or "/", "kind": "folder", "name": name})

    def save_note(self, folder: str, name: str, text: str, tags: list[str] | None = None) -> Item:
        """A note in a folder (by path; made if missing). A taken name gets " (2)"."""
        return self._create({"path": folder, "kind": "note", "name": name, "text": text,
                             "tags": tags or []})

    def save_link(self, folder: str, name: str, url: str, tags: list[str] | None = None) -> Item:
        return self._create({"path": folder, "kind": "link", "name": name, "text": url,
                             "tags": tags or []})

    def upload(self, folder: str, name: str, data: bytes, mime: str = "",
               tags: list[str] | None = None) -> Item:
        """A file into a folder (by path). Big files go up raw, small ones inline."""
        if len(data) > 4 * 1024 * 1024:
            params = {"path": folder, "name": name, "mime": mime}
            if tags:
                params["tags"] = ",".join(tags)
            return Item.from_json(self._call("POST", "/v1/upload", params=params, data=data))
        return self._create({"path": folder, "kind": "file", "name": name, "mime": mime,
                             "data_b64": base64.b64encode(data).decode(), "tags": tags or []})

    def update(self, item: str, **changes: Any) -> Item:
        """name=, text=, about=, starred=, tags=[...], add_tags=[...], remove_tags=[...],
        path= (move into that folder) or parent= (an id; None for the top)."""
        found = self.get(item)
        return Item.from_json(self._call("POST", f"/v1/items/{found.id}", json=changes))

    def rename(self, item: str, name: str) -> Item:
        return self.update(item, name=name)

    def move(self, item: str, folder: str) -> Item:
        return self.update(item, path=folder) if folder != "/" else self.update(item, parent=None)

    def tag(self, item: str, *tags: str) -> Item:
        return self.update(item, add_tags=list(tags))

    def untag(self, item: str, *tags: str) -> Item:
        return self.update(item, remove_tags=list(tags))

    def star(self, item: str, on: bool = True) -> Item:
        return self.update(item, starred=on)

    def link(self, a: str, b: str, label: str = "") -> None:
        self._call("POST", "/v1/links", json={"a": self.get(a).id, "b": self.get(b).id,
                                               "label": label})

    def trash(self, item: str) -> None:
        """To the Trash, where he can restore it. Never gone for good from here."""
        self._call("DELETE", f"/v1/items/{self.get(item).id}")

    # ── the details ─────────────────────────────────────────────────────────
    def _exists(self, path: str) -> bool:
        try:
            self.get(path)
            return True
        except ShieldError as e:
            if e.status == 404:
                return False
            raise

    def _create(self, body: dict) -> Item:
        return Item.from_json(self._call("POST", "/v1/items", json=body))

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, f"{self._url}{path}", timeout=self._timeout, **kwargs)
        self._check(response)
        return response.json()

    @staticmethod
    def _check(response: requests.Response) -> None:
        if response.status_code >= 400:
            try:
                message = response.json().get("error", response.text)
            except ValueError:
                message = response.text
            raise ShieldError(response.status_code, message)
