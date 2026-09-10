from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Type

from .types import TYPE_NAMES

Executor = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class Port:
    name: str
    value_type: Type[Any]
    required: bool = True
    many: bool = False

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "dataType": TYPE_NAMES.get(self.value_type, self.value_type.__name__),
            "required": self.required,
            "many": self.many,
        }


@dataclass(frozen=True)
class NodeSpec:
    type_name: str
    title: str
    category: str
    inputs: List[Port]
    outputs: List[Port]
    executor: Executor
    config_schema: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "type": self.type_name,
            "title": self.title,
            "category": self.category,
            "inputs": [p.describe() for p in self.inputs],
            "outputs": [p.describe() for p in self.outputs],
            "config": self.config_schema,
        }


class Registry:
    def __init__(self) -> None:
        self._specs: Dict[str, NodeSpec] = {}

    def add(self, spec: NodeSpec) -> None:
        if spec.type_name in self._specs:
            raise ValueError("重复节点类型: %s" % spec.type_name)
        self._specs[spec.type_name] = spec

    def get(self, type_name: str) -> NodeSpec:
        try:
            return self._specs[type_name]
        except KeyError:
            raise KeyError("未知节点类型: %s" % type_name)

    def describe(self) -> List[Dict[str, Any]]:
        return [self._specs[key].describe() for key in sorted(self._specs)]
