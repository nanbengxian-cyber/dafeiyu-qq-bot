from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .model import NodeSpec, Port, Registry
from .types import TraceRecord, to_jsonable

MAX_NODES = 200
MAX_EDGES = 1000
MAX_ID_LENGTH = 80
MAX_STRING_LENGTH = 16384
MAX_JSON_DEPTH = 20
TRACE_SENSITIVE_KEYS = {
    "text", "content", "context", "user_text", "sender_id", "sender_name", "group_id",
    "message", "memory_text", "relationship_text",
}


class GraphError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_GRAPH", node_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.node_id = node_id

    def describe(self) -> Dict[str, Any]:
        return {"code": self.code, "message": str(self), "nodeId": self.node_id}


def load_graph(path: str) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(data, dict):
        raise GraphError("图文件必须是对象")
    return data


def _reject_json_constant(value: str) -> None:
    raise ValueError("JSON 不允许非有限数值: %s" % value)


def _validate_json_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise GraphError("图配置嵌套过深", "CONFIG_LIMIT")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise GraphError("图中不允许 NaN 或 Infinity", "INVALID_NUMBER")
        return
    if type(value) is str:
        if len(value) > MAX_STRING_LENGTH:
            raise GraphError("图中字符串超过长度限制", "CONFIG_LIMIT")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or len(key) > MAX_ID_LENGTH:
                raise GraphError("配置键必须是短字符串", "INVALID_CONFIG")
            _validate_json_value(item, depth + 1)
        return
    raise GraphError("图配置包含不支持的数据类型", "INVALID_CONFIG")


def _digest_summary(value: Any) -> Dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
    return {
        "redacted": True,
        "type": "text" if isinstance(value, str) else type(value).__name__,
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }


def trace_jsonable(value: Any, key: Optional[str] = None) -> Any:
    """Return a debugging summary that never exposes message/context identity text."""
    if key in TRACE_SENSITIVE_KEYS:
        return _digest_summary(value)
    if hasattr(value, "__dataclass_fields__"):
        return {name: trace_jsonable(item, name) for name, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(name): trace_jsonable(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [trace_jsonable(item) for item in value]
    if isinstance(value, str):
        return value[:200] if len(value) <= 200 else _digest_summary(value)
    if type(value) in (int, float, bool) or value is None:
        return value
    return {"type": type(value).__name__}


class Engine:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def validate(self, graph: Mapping[str, Any]) -> List[str]:
        _validate_json_value(graph)
        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if graph.get("version") != 1 or not isinstance(nodes, list) or not isinstance(edges, list):
            raise GraphError("图必须包含 version=1、nodes 数组和 edges 数组")
        if not nodes:
            raise GraphError("图不能为空", "EMPTY_GRAPH")
        if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
            raise GraphError("图超过节点或连线数量限制", "GRAPH_TOO_LARGE")

        by_id: Dict[str, Mapping[str, Any]] = {}
        specs: Dict[str, NodeSpec] = {}
        input_ids: List[str] = []
        preview_ids: List[str] = []
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not isinstance(node.get("type"), str):
                raise GraphError("节点必须有字符串 id 和 type")
            unknown_node_fields = set(node) - {"id", "type", "position", "config"}
            if unknown_node_fields:
                raise GraphError("节点包含未知字段: %s" % sorted(unknown_node_fields), "UNKNOWN_FIELD", node.get("id"))
            node_id = node["id"]
            if not node_id or len(node_id) > MAX_ID_LENGTH or node_id in by_id:
                raise GraphError("节点 id 为空、过长或重复: %s" % node_id, "DUPLICATE_NODE", node_id)
            try:
                spec = self.registry.get(node["type"])
            except KeyError:
                raise GraphError("未知节点类型: %s" % node["type"], "UNKNOWN_NODE", node_id)
            config = node.get("config", {})
            if not isinstance(config, dict):
                raise GraphError("节点 config 必须是对象", node_id=node_id)
            self._validate_config(spec, config, node_id)
            self._validate_position(node.get("position"), node_id)
            by_id[node_id], specs[node_id] = node, spec
            if node["type"] == "input.message":
                input_ids.append(node_id)
            if node["type"] == "output.preview":
                preview_ids.append(node_id)

        if len(input_ids) != 1:
            raise GraphError("图必须恰好有一个消息输入节点", "INPUT_COUNT")
        if not preview_ids:
            raise GraphError("图必须至少有一个预览输出节点", "MISSING_PREVIEW")

        incoming: Dict[str, List[Mapping[str, Any]]] = {key: [] for key in by_id}
        outgoing: Dict[str, List[Mapping[str, Any]]] = {key: [] for key in by_id}
        seen_targets: Set[Tuple[str, str]] = set()
        seen_edges: Set[Tuple[str, str, str, str]] = set()
        edge_ids: Set[str] = set()
        for edge in edges:
            if not isinstance(edge, dict):
                raise GraphError("连线必须是对象")
            if set(edge) != {"id", "from", "to"}:
                raise GraphError("连线必须且只能包含 id/from/to", "UNKNOWN_FIELD")
            edge_id = edge.get("id")
            if not isinstance(edge_id, str) or not edge_id or len(edge_id) > MAX_ID_LENGTH or edge_id in edge_ids:
                raise GraphError("连线 id 为空、过长或重复", "DUPLICATE_EDGE")
            edge_ids.add(edge_id)
            source, target = edge.get("from"), edge.get("to")
            if not isinstance(source, dict) or not isinstance(target, dict) or set(source) != {"node", "port"} or set(target) != {"node", "port"}:
                raise GraphError("连线端点格式无效", "INVALID_ENDPOINT")
            sid, sport = source.get("node"), source.get("port")
            tid, tport = target.get("node"), target.get("port")
            if sid not in by_id or tid not in by_id:
                raise GraphError("连线引用不存在的节点", "UNKNOWN_ENDPOINT")
            if sid == tid:
                raise GraphError("节点不能连接自身", "SELF_LOOP", tid)
            out_port = self._port(specs[sid].outputs, sport, sid)
            in_port = self._port(specs[tid].inputs, tport, tid)
            if out_port.value_type is not in_port.value_type:
                raise GraphError("端口类型不兼容: %s.%s → %s.%s" % (sid, sport, tid, tport), "TYPE_MISMATCH", tid)
            edge_key = (sid, sport, tid, tport)
            if edge_key in seen_edges:
                raise GraphError("重复连线: %s" % edge_id, "DUPLICATE_EDGE", tid)
            seen_edges.add(edge_key)
            target_key = (tid, tport)
            if target_key in seen_targets and not in_port.many:
                raise GraphError("单值输入端口只能连接一次: %s.%s" % target_key, "MULTIPLE_INPUTS", tid)
            seen_targets.add(target_key)
            incoming[tid].append(edge)
            outgoing[sid].append(edge)

        for node_id, spec in specs.items():
            connected = {edge["to"]["port"] for edge in incoming[node_id]}
            for port in spec.inputs:
                if port.required and port.name not in connected:
                    raise GraphError("缺少必需输入: %s.%s" % (node_id, port.name), "MISSING_INPUT", node_id)

        order = self._topological_order(by_id, incoming, outgoing)
        reachable = self._reachable(input_ids[0], outgoing)
        if reachable != set(by_id):
            missing = sorted(set(by_id) - reachable)
            raise GraphError("存在无法从消息输入到达的节点: %s" % missing, "UNREACHABLE_NODE")

        sinks = {node_id for node_id in by_id if not outgoing[node_id]}
        if not sinks or any(by_id[node_id]["type"] != "output.preview" for node_id in sinks):
            raise GraphError("所有终点都必须是预览输出节点", "UNSAFE_SINK")
        for preview_id in preview_ids:
            if outgoing[preview_id]:
                raise GraphError("预览输出必须是终点", "UNSAFE_PREVIEW", preview_id)
            source_id = incoming[preview_id][0]["from"]["node"]
            if by_id[source_id]["type"] != "transform.humanize":
                raise GraphError("预览输出必须直接来自人味处理节点", "UNSAFE_OUTPUT_PATH", preview_id)
            human_incoming = [e for e in incoming[source_id] if e["to"]["port"] == "plan"]
            if len(human_incoming) != 1 or by_id[human_incoming[0]["from"]["node"]]["type"] != "safety.output_moderation":
                raise GraphError("人味处理必须直接接在出口审核之后", "UNSAFE_OUTPUT_PATH", source_id)
        return order

    @staticmethod
    def _validate_position(position: Any, node_id: str) -> None:
        if position is None:
            return
        if not isinstance(position, dict) or set(position) != {"x", "y"}:
            raise GraphError("节点 position 必须包含 x/y", "INVALID_POSITION", node_id)
        for value in position.values():
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 100000:
                raise GraphError("节点坐标无效", "INVALID_POSITION", node_id)

    @staticmethod
    def _validate_config(spec: NodeSpec, config: Mapping[str, Any], node_id: str) -> None:
        unknown = set(config) - set(spec.config_schema)
        if unknown:
            raise GraphError("节点配置包含未知字段: %s" % sorted(unknown), "UNKNOWN_CONFIG", node_id)
        for name, value in config.items():
            schema = spec.config_schema[name]
            kind = schema.get("kind")
            valid = ((kind == "string" and type(value) is str) or
                     (kind == "boolean" and type(value) is bool) or
                     (kind == "integer" and type(value) is int) or
                     (kind == "array" and type(value) is list and all(type(item) is str for item in value)) or
                     (kind == "object" and type(value) is dict) or
                     (kind == "message" and type(value) is dict))
            if not valid:
                raise GraphError("节点配置 %s 类型不正确" % name, "INVALID_CONFIG", node_id)
            if kind == "string" and len(value) > int(schema.get("maxLength", MAX_STRING_LENGTH)):
                raise GraphError("节点配置 %s 超长" % name, "INVALID_CONFIG", node_id)
            if kind == "integer" and (value < int(schema.get("min", -1000000)) or value > int(schema.get("max", 1000000))):
                raise GraphError("节点配置 %s 超出范围" % name, "INVALID_CONFIG", node_id)
            if kind == "array" and (len(value) > 100 or any(not item.strip() or len(item) > 200 for item in value)):
                raise GraphError("节点配置 %s 数组无效" % name, "INVALID_CONFIG", node_id)
            if kind == "object" and schema.get("format") == "message":
                allowed = {"text", "message_type", "sender_id", "sender_name", "group_id", "mentioned"}
                if set(value) - allowed or "text" not in value:
                    raise GraphError("脱敏消息字段无效或缺少 text", "INVALID_CONFIG", node_id)
                defaults = {"message_type": "group", "sender_id": "member-demo", "sender_name": "群友A", "group_id": "group-demo"}
                string_limits = {"text": 4096, "message_type": 16, "sender_id": 100, "sender_name": 80, "group_id": 100}
                for field, limit in string_limits.items():
                    item = value.get(field, defaults.get(field, ""))
                    if type(item) is not str or len(item) > limit:
                        raise GraphError("脱敏消息字段 %s 无效" % field, "INVALID_CONFIG", node_id)
                if "mentioned" in value and type(value["mentioned"]) is not bool:
                    raise GraphError("脱敏消息字段 mentioned 无效", "INVALID_CONFIG", node_id)

    @staticmethod
    def _topological_order(by_id: Mapping[str, Any], incoming: Mapping[str, List[Mapping[str, Any]]], outgoing: Mapping[str, List[Mapping[str, Any]]]) -> List[str]:
        indegree = {node_id: len({e["from"]["node"] for e in incoming[node_id]}) for node_id in by_id}
        queue = [node_id for node_id in by_id if indegree[node_id] == 0]
        order: List[str] = []
        while queue:
            node_id = queue.pop(0)
            order.append(node_id)
            next_ids = []
            for edge in outgoing[node_id]:
                next_id = edge["to"]["node"]
                if next_id not in next_ids:
                    next_ids.append(next_id)
            for next_id in next_ids:
                indegree[next_id] -= 1
                if indegree[next_id] == 0:
                    queue.append(next_id)
        if len(order) != len(by_id):
            raise GraphError("节点图包含环，第一版只接受 DAG", "CYCLE")
        return order

    @staticmethod
    def _reachable(start: str, outgoing: Mapping[str, List[Mapping[str, Any]]]) -> Set[str]:
        seen: Set[str] = set()
        pending = [start]
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            pending.extend(edge["to"]["node"] for edge in outgoing[node_id])
        return seen

    @staticmethod
    def _port(ports: List[Port], name: Any, node_id: str) -> Port:
        for port in ports:
            if port.name == name:
                return port
        raise GraphError("节点 %s 不存在端口 %s" % (node_id, name), "UNKNOWN_PORT", node_id)

    def execute(self, graph: Mapping[str, Any]) -> Dict[str, Any]:
        order = self.validate(graph)
        nodes = {node["id"]: node for node in graph["nodes"]}
        incoming: Dict[str, List[Mapping[str, Any]]] = {key: [] for key in nodes}
        for edge in graph["edges"]:
            incoming[edge["to"]["node"]].append(edge)
        values: Dict[Tuple[str, str], Any] = {}
        trace: List[TraceRecord] = []
        for sequence, node_id in enumerate(order, 1):
            node = nodes[node_id]
            spec = self.registry.get(node["type"])
            node_inputs: Dict[str, Any] = {}
            for edge in incoming[node_id]:
                port_name = edge["to"]["port"]
                value = values[(edge["from"]["node"], edge["from"]["port"])]
                port = self._port(spec.inputs, port_name, node_id)
                if port.many:
                    node_inputs.setdefault(port_name, []).append(value)
                else:
                    node_inputs[port_name] = value
            started = time.perf_counter()
            try:
                outputs = dict(spec.executor(node.get("config", {}), node_inputs))
                expected = {port.name: port for port in spec.outputs}
                if set(outputs) != set(expected):
                    raise RuntimeError("输出端口不完整，预期 %s，得到 %s" % (sorted(expected), sorted(outputs)))
                for name, value in outputs.items():
                    if type(value) is not expected[name].value_type:
                        raise TypeError("输出 %s 类型错误" % name)
                    values[(node_id, name)] = value
                duration = (time.perf_counter() - started) * 1000
                trace.append(TraceRecord(sequence, node_id, node["type"], "success", duration,
                                         trace_jsonable(node_inputs), trace_jsonable(outputs)))
            except Exception as exc:
                duration = (time.perf_counter() - started) * 1000
                trace.append(TraceRecord(sequence, node_id, node["type"], "error", duration,
                                         trace_jsonable(node_inputs), {}, "节点执行失败"))
                return {"status": "error", "result": None, "trace": [to_jsonable(x) for x in trace], "error": "节点执行失败: %s" % node_id}
        sinks = sorted(set(nodes) - {edge["from"]["node"] for edge in graph["edges"]})
        results = [{
            "node": sink_id,
            "outputs": {name: to_jsonable(value) for (node_id, name), value in values.items() if node_id == sink_id},
        } for sink_id in sinks]
        return {"status": "success", "result": results, "trace": [to_jsonable(x) for x in trace], "error": None}
