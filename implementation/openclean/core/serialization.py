"""Strict, dependency-free decoding for persisted runtime dataclasses."""
from __future__ import annotations

import math
import types
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints


def decode_value(annotation: Any, value: Any, location: str = "value", *, require_all: bool = False) -> Any:
    return _decode_value(annotation, value, location, require_all, {})


def decode_dataclass(cls: Any, data: Any, location: str = "object", *, require_all: bool = False) -> Any:
    return _decode_dataclass(cls, data, location, require_all, {})


def _decode_value(
    annotation: Any, value: Any, location: str, require_all: bool, schemas: dict,
) -> Any:
    if annotation is Any:
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if isinstance(value, list):
            return [_decode_value(Any, v, location, require_all, schemas) for v in value]
        if isinstance(value, dict) and all(isinstance(k, str) for k in value):
            return {k: _decode_value(Any, v, location, require_all, schemas) for k, v in value.items()}
    elif annotation is type(None):
        if value is None:
            return None
    elif annotation is float:
        if type(value) in (int, float) and math.isfinite(value):
            return value
    elif annotation in (str, int, bool):
        if type(value) is annotation:
            return value
    elif annotation is Path:
        if isinstance(value, str) and value and "\0" not in value:
            path = Path(value)
            if path.is_absolute() and ".." not in path.parts:
                return path
    else:
        origin, args = get_origin(annotation), get_args(annotation)
        if origin in (Union, types.UnionType):
            for option in args:
                try:
                    return _decode_value(option, value, location, require_all, schemas)
                except (ValueError, TypeError):
                    pass
        elif origin in (tuple, list):
            if isinstance(value, (list, tuple)):
                decoded = (_decode_value(args[0], v, location, require_all, schemas) for v in value)
                return tuple(decoded) if origin is tuple else list(decoded)
        elif origin is dict:
            if isinstance(value, dict):
                return {_decode_value(args[0], k, location, require_all, schemas):
                        _decode_value(args[1], v, location, require_all, schemas) for k, v in value.items()}
        elif is_dataclass(annotation):
            return _decode_dataclass(annotation, value, location, require_all, schemas)
    raise ValueError(f"{location}: invalid value for {annotation}")


def _decode_dataclass(
    cls: Any, data: Any, location: str, require_all: bool, schemas: dict,
) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"{location}: expected an object")
    schema = schemas.get(cls)
    definitions = schema[0] if schema is not None else {f.name: f for f in fields(cls)}
    unknown = data.keys() - definitions.keys()
    if unknown:
        raise ValueError(f"{location}: 未知字段 / unknown fields: {', '.join(sorted(unknown))}")
    if schema is None:
        # Scope metadata to one decode operation: repeated objects share it, but
        # a later call must still see changed annotations or forward references.
        hints = get_type_hints(cls)
        schemas[cls] = (definitions, hints)
    else:
        hints = schema[1]
    kwargs = {}
    for name, definition in definitions.items():
        if name not in data:
            if require_all or (definition.default is MISSING and definition.default_factory is MISSING):
                raise ValueError(f"{location}: missing {name}")
            continue
        kwargs[name] = _decode_value(hints[name], data[name], f"{location}.{name}", require_all, schemas)
    return cls(**kwargs)


def strict_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")
