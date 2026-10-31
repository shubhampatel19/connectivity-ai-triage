"""Runbooks: markdown files with YAML front matter, split into one chunk per
"## " section for retrieval. A chunk carries its runbook's title, so a
section called "Fix" still says what it fixes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    runbook_id: str
    title: str
    section: str
    content: str
    alert_codes: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """What gets embedded and shown to the model."""
        return f"{self.title}: {self.section}\n{self.content}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    alert_codes: tuple[str, ...]
    categories: tuple[str, ...]
    chunks: list[Chunk] = field(default_factory=list)


FRONT = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse(path: Path) -> Runbook:
    text = path.read_text()
    m = FRONT.match(text)
    if not m:
        raise ValueError(f"{path}: no front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    rid = meta.get("id") or path.stem
    if rid != path.stem:
        raise ValueError(f"{path}: id {rid!r} must match the file name")
    title = meta.get("title") or rid
    codes = tuple(meta.get("alert_codes") or ())
    body = text[m.end() :]
    rb = Runbook(rid, title, codes, tuple(meta.get("categories") or ()))

    section, buf = "Overview", []
    for line in body.splitlines():
        if line.startswith("## "):
            _add(rb, section, buf)
            section, buf = line[3:].strip(), []
        elif line.startswith("# "):
            continue  # the title, already in every chunk
        else:
            buf.append(line)
    _add(rb, section, buf)
    if not rb.chunks:
        raise ValueError(f"{path}: no content")
    return rb


def _add(rb: Runbook, section: str, buf: list[str]) -> None:
    content = "\n".join(buf).strip()
    if not content:
        return
    slug = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
    rb.chunks.append(Chunk(f"{rb.id}#{slug}", rb.id, rb.title, section, content, rb.alert_codes))


def load(directory: str | Path) -> list[Runbook]:
    paths = sorted(Path(directory).glob("*.md"))
    if not paths:
        raise ValueError(f"no runbooks in {directory}")
    books = [parse(p) for p in paths]
    ids = [b.id for b in books]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate runbook ids")
    return books
