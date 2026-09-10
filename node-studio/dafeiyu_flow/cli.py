from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from .engine import Engine, GraphError, load_graph
from .registry import build_registry

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="大肥鱼行为节点离线运行器")
    parser.add_argument("graph", nargs="?", default=str(ROOT / "graphs" / "01-basic-chat.json"))
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    engine = Engine(build_registry())
    try:
        graph = load_graph(args.graph)
        order = engine.validate(graph)
        payload: Dict[str, Any] = {"valid": True, "executionOrder": order}
        if not args.validate:
            payload = engine.execute(graph)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, GraphError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
