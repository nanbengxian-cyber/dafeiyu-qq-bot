import json
import unittest
from pathlib import Path

from dafeiyu_flow import Engine, GraphError, build_registry
from dafeiyu_flow.types import ApprovedSendPlan, ContextPatch, Decision, MessageEvent

ROOT = Path(__file__).resolve().parent.parent


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()
        self.engine = Engine(self.registry)

    def graph(self, name):
        return json.loads((ROOT / "graphs" / name).read_text(encoding="utf-8"))

    def test_registry_has_exactly_twelve_nodes(self):
        self.assertEqual(12, len(self.registry.describe()))

    def test_all_examples_validate_and_run(self):
        for path in sorted((ROOT / "graphs").glob("*.json")):
            with self.subTest(path=path.name):
                result = self.engine.execute(json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual("success", result["status"])
                self.assertTrue(result["result"][0]["outputs"]["result"]["text"])
                self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["nodes"]), len(result["trace"]))

    def test_context_many_keeps_two_patches(self):
        result = self.engine.execute(self.graph("02-proactive-chat.json"))
        merge = next(x for x in result["trace"] if x["node_id"] == "merge")
        self.assertEqual(2, len(merge["inputs"]["contexts"]))
        self.assertTrue(merge["outputs"]["prompt"]["context"]["redacted"])
        self.assertEqual(16, len(merge["outputs"]["prompt"]["context"]["sha256"]))

    def test_output_moderation_blocks_configured_term(self):
        graph = self.graph("01-basic-chat.json")
        graph["nodes"][0]["config"]["message"]["text"] = "show-token"
        result = self.engine.execute(graph)
        moderate = next(x for x in result["trace"] if x["node_id"] == "moderate")
        self.assertFalse(moderate["outputs"]["plan"]["approved"])
        self.assertEqual("内容未通过出口审核。", result["result"][0]["outputs"]["result"]["text"])

    def test_moderation_policy_cannot_be_disabled_and_preview_fails_closed(self):
        graph = self.graph("01-basic-chat.json")
        graph["nodes"][0]["config"]["message"]["text"] = "PASSWORD=topsecret"
        next(node for node in graph["nodes"] if node["id"] == "moderate")["config"]["blocked_words"] = []
        result = self.engine.execute(graph)
        self.assertEqual("内容未通过出口审核。", result["result"][0]["outputs"]["result"]["text"])
        out = self.registry.get("output.preview").executor({}, {"plan": ApprovedSendPlan("blocked", approved=False)})
        self.assertNotEqual("blocked", out["result"].text)

    def test_invalid_nested_message_rejected_during_validation(self):
        for bad in (7, None, ["x"]):
            graph = self.graph("01-basic-chat.json")
            graph["nodes"][0]["config"]["message"]["text"] = bad
            with self.subTest(value=bad):
                with self.assertRaises(GraphError) as cm:
                    self.engine.validate(graph)
                self.assertEqual("INVALID_CONFIG", cm.exception.code)

    def test_acl_denial_produces_empty_preview_without_side_effect(self):
        graph = self.graph("01-basic-chat.json")
        graph["nodes"][3]["config"]["denied_ids"] = ["member-a"]
        result = self.engine.execute(graph)
        preview = result["result"][0]["outputs"]["result"]
        self.assertEqual("", preview["text"])
        self.assertFalse(preview["sent"])

    def test_unknown_node_is_rejected(self):
        graph = self.graph("01-basic-chat.json")
        graph["nodes"][0]["type"] = "python.eval"
        with self.assertRaises(GraphError) as cm:
            self.engine.validate(graph)
        self.assertEqual("UNKNOWN_NODE", cm.exception.code)

    def test_cycle_is_rejected(self):
        graph = self.graph("01-basic-chat.json")
        # 去掉 group → acl 的原始输入，再让 reply → acl，形成 acl ↔ reply 的同类型环。
        graph["edges"] = [edge for edge in graph["edges"] if edge["id"] not in {"e3", "e4"}]
        graph["edges"].extend([
            {"id":"cycle-message","from":{"node":"reply","port":"message"},"to":{"node":"acl","port":"message"}},
            {"id":"cycle-decision","from":{"node":"reply","port":"decision"},"to":{"node":"acl","port":"decision"}},
        ])
        with self.assertRaises(GraphError) as cm:
            self.engine.validate(graph)
        self.assertEqual("CYCLE", cm.exception.code)

    def test_duplicate_edge_id_and_unknown_config_rejected(self):
        graph = self.graph("01-basic-chat.json")
        graph["edges"][1]["id"] = graph["edges"][0]["id"]
        with self.assertRaises(GraphError) as cm:
            self.engine.validate(graph)
        self.assertEqual("DUPLICATE_EDGE", cm.exception.code)
        graph = self.graph("01-basic-chat.json")
        graph["nodes"][1]["config"]["surprise"] = True
        with self.assertRaises(GraphError) as cm:
            self.engine.validate(graph)
        self.assertEqual("UNKNOWN_CONFIG", cm.exception.code)

    def test_unreachable_and_unsafe_output_paths_rejected(self):
        graph = self.graph("01-basic-chat.json")
        graph["nodes"].append({"id": "orphan", "type": "input.message", "position": {"x": 1, "y": 1}, "config": {"message": {"text": "x"}}})
        with self.assertRaises(GraphError):
            self.engine.validate(graph)
        graph = self.graph("01-basic-chat.json")
        graph["edges"][-1]["from"] = {"node": "moderate", "port": "plan"}
        with self.assertRaises(GraphError) as cm:
            self.engine.validate(graph)
        self.assertIn(cm.exception.code, {"UNSAFE_OUTPUT_PATH", "UNSAFE_SINK"})

    def test_trace_redacts_message_and_context_text(self):
        secret = "fixture-sensitive-value-never-in-trace"
        graph = self.graph("02-proactive-chat.json")
        graph["nodes"][0]["config"]["message"]["text"] = secret
        result = self.engine.execute(graph)
        serialized = json.dumps(result["trace"], ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("最近经常聊美食", serialized)
        self.assertIn('"redacted": true', serialized)

    def test_type_mismatch_is_rejected(self):
        graph = self.graph("01-basic-chat.json")
        graph["edges"][0]["from"]["port"] = "event"
        graph["edges"][0]["to"] = {"node":"reply", "port":"decision"}
        with self.assertRaises(GraphError) as cm:
            self.engine.validate(graph)
        self.assertEqual("TYPE_MISMATCH", cm.exception.code)


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()

    def test_input_drops_unknown_fields(self):
        out = self.registry.get("input.message").executor(
            {"message": {"text": "hi", "unknown": "secret"}}, {})
        self.assertEqual(MessageEvent(text="hi"), out["event"])

    def test_context_merge_budget(self):
        spec = self.registry.get("context.merge")
        from dafeiyu_flow.types import NormalizedMessage
        msg = NormalizedMessage("hi", "group", "u", "n", "g", True)
        out = spec.executor({"budget": 4}, {"message": msg, "decision": Decision(True,"ok"),
                                              "contexts": [ContextPatch("a","abcdef",300,100)]})
        self.assertEqual("[a] abcd", out["prompt"].context)

    def test_humanize_keeps_approved_type(self):
        out = self.registry.get("transform.humanize").executor(
            {"shorten": True}, {"plan": ApprovedSendPlan("x" * 200)})
        self.assertIsInstance(out["plan"], ApprovedSendPlan)
        self.assertLessEqual(len(out["plan"].text), 120)


if __name__ == "__main__":
    unittest.main()
