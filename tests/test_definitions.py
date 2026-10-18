import unittest

from func_seq import DefinitionError, InvalidValueError, Ref, Script
from func_seq.definitions import resolve
from func_seq.values import MAX_BYTES, decode_json, encode_json


def identity(value):
    return value


class DefinitionTests(unittest.TestCase):
    def test_references_and_argument_isolation(self):
        script = Script("example")
        data = {"items": [1]}
        source = script.add_function(identity, data, step_id="source")
        script.add_function(identity, {"nested": [source["items"][0]]}, step_id="target")
        fingerprint = script.fingerprint
        data["items"].append(2)
        manifest = script.manifest
        manifest["steps"].clear()
        self.assertEqual(script.fingerprint, fingerprint)
        self.assertEqual(script.step_ids, ("source", "target"))
        results = {"source": {"items": [3]}}
        copied = resolve(source, results)
        copied["items"].append(4)
        self.assertEqual(resolve(source["items"][0], results), 3)
        self.assertEqual(results, {"source": {"items": [3]}})

    def test_fingerprint_covers_definition(self):
        first, same = Script("sample"), Script("sample")
        for script in (first, same):
            script.add_function(identity, 2)
        self.assertEqual(first.fingerprint, same.fingerprint)
        changed = Script("sample", version="2")
        changed.add_function(identity, 2)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        changed = Script("sample")
        changed.add_function(identity, 3)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)

    def test_invalid_definitions(self):
        script = Script("test")
        with self.assertRaises(DefinitionError):
            script.add_function(identity)
        with self.assertRaises(DefinitionError):
            script.add_function(identity, Ref("future"))
        script.add_function(identity, None)
        with self.assertRaises(DefinitionError):
            script.add_function(identity, 1)
        script.add_pause(step_id="boundary")
        with self.assertRaises(DefinitionError):
            script.add_function(identity, Ref("boundary"), step_id="bad")
        for part in (-1, True, object()):
            with self.assertRaises(DefinitionError):
                Ref("identity")[part]
        with self.assertRaises(DefinitionError):
            Script("../bad")

    def test_strict_json_and_reference_ambiguity(self):
        for bad in (object(), (1, 2), {1: "x"}, float("nan"), float("inf"), "\ud800"):
            with self.subTest(kind=type(bad)), self.assertRaises(InvalidValueError):
                Script("test").add_function(identity, bad)
        script = Script("test")
        script.add_function(identity, {"ref": {"step": "ordinary data"}})
        self.assertIn("dict", script.manifest["steps"][0]["args"][0])

    def test_bounded_strict_json_parser(self):
        for payload in (b'{"a":1,"a":2}', b'NaN', b'1e999', b'"\\ud800"', b'[' * 100):
            with self.subTest(payload=payload[:30]), self.assertRaises(InvalidValueError):
                decode_json(payload)
        with self.assertRaises(InvalidValueError):
            encode_json("x" * MAX_BYTES)
        value = {"unicode": "hello 👋", "number": [1, 2.5, True, None]}
        self.assertEqual(decode_json(encode_json(value)), value)
