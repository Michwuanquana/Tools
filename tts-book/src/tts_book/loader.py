"""Načtení souboru knihy: YAML → validovaný model `Book`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .model import Book


class BookError(Exception):
    """Soubor knihy nejde načíst; `problems` obsahuje lidsky čitelné chyby."""

    def __init__(self, path: Path, problems: list[str]):
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: " + "; ".join(problems))


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader, který odmítne duplicitní klíče (PyYAML je jinak tiše přepíše)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    seen: dict[Any, yaml.Node] = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "při čtení mapy",
                node.start_mark,
                f"duplicitní klíč {key!r}",
                key_node.start_mark,
            )
        seen[key] = key_node
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def format_loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "(kořen)"


def _is_union_tag(part: Any) -> bool:
    """Pydantic vkládá do cesty názvy větví unionu (`str`, `function-after[…]`)."""
    return isinstance(part, str) and (part in ("str", "Paragraph") or part.startswith(("function-", "model[")))


def format_validation_error(err: ValidationError) -> list[str]:
    """Zkrátí chyby pydanticu; u odstavců odfiltruje šum z větve `str | Paragraph`."""
    problems: list[str] = []
    for e in err.errors():
        loc = tuple(e["loc"])
        if "str" in loc and e["type"] == "string_type":
            # Odstavec zapsaný jako mapa – chyba větve "prostý řetězec" je jen šum.
            continue
        loc = tuple(p for p in loc if not _is_union_tag(p))
        msg = e["msg"]
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        if e["type"] == "extra_forbidden":
            msg = "neznámý klíč (překlep?)"
        elif e["type"] == "missing":
            msg = "chybí povinný klíč"
        problems.append(f"{format_loc(loc)}: {msg}")
    return problems or [str(err)]


def load_book(path: str | Path) -> Book:
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.load(f, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader podtřída
    except OSError as exc:
        raise BookError(path, [f"nelze přečíst soubor: {exc}"]) from exc
    except yaml.YAMLError as exc:
        raise BookError(path, [f"chyba YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise BookError(path, ["soubor musí obsahovat mapu (klíče version, meta, cast, chapters …)"])
    try:
        return Book.model_validate(data)
    except ValidationError as exc:
        raise BookError(path, format_validation_error(exc)) from exc
