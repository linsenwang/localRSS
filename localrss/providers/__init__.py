"""Provider 注册表。

providers/ 目录下的每个模块都会被自动导入，模块内用 @register 装饰的类
会按 `type` 注册进来。想支持新网站，只要往这个目录里加文件。
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import Provider

_REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    if not cls.type:
        raise ValueError(f"{cls.__name__} 缺少 type")
    _REGISTRY[cls.type] = cls
    return cls


def load_providers() -> None:
    """导入 providers/ 下的所有子模块，触发注册。"""
    for mod in pkgutil.iter_modules(__path__):
        if mod.name == "base" or mod.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod.name}")


def available_types() -> dict[str, type[Provider]]:
    load_providers()
    return dict(_REGISTRY)


def create(type_name: str, options: dict | None = None, common: dict | None = None) -> Provider:
    load_providers()
    if type_name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(无)"
        raise ValueError(f"未知的源类型 {type_name!r}，已注册的类型：{known}")
    return _REGISTRY[type_name](options or {}, common or {})


__all__ = ["Provider", "register", "load_providers", "available_types", "create"]
