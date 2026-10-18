import unittest

from func_seq import DefinitionError, InvalidValueError, Ref, Sequence
from func_seq.definitions import resolve
from func_seq.values import MAX_BYTES, decode_json, encode_json


def identity(value):
    return value


class DefinitionTests(unittest.TestCase):
    def test_references_and_argument_isolation(self):
        sequence = Sequence("example")
        data = {"items": [1]}
        source = sequence.add_function(identity, data, step_id="source")
        sequence.add_function(identity, {"nested": [source["items"][0]]}, step_id="target")
        fingerprint = sequence.fingerprint
        data["items"].append(2)
        manifest = sequence.manifest
        manifest["steps"].clear()
        self.assertEqual(sequence.fingerprint, fingerprint)
        self.assertEqual(sequence.step_ids, ("source", "target"))
        results = {"source": {"items": [3]}}
        copied = resolve(source, results)
        copied["items"].append(4)
        self.assertEqual(resolve(source["items"][0], results), 3)
        self.assertEqual(results, {"source": {"items": [3]}})

    def test_fingerprint_covers_definition(self):
        first, same = Sequence("sample"), Sequence("sample")
        for sequence in (first, same):
            sequence.add_function(identity, 2)
        self.assertEqual(first.fingerprint, same.fingerprint)
        changed = Sequence("sample", version="2")
        changed.add_function(identity, 2)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        changed = Sequence("sample")
        changed.add_function(identity, 3)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)

    def test_invalid_definitions(self):
        sequence = Sequence("test")
        with self.assertRaises(DefinitionError):
            sequence.add_function(identity)
        with self.assertRaises(DefinitionError):
            sequence.add_function(identity, Ref("future"))
        sequence.add_function(identity, None)
        with self.assertRaises(DefinitionError):
            sequence.add_function(identity, 1)
        sequence.add_pause(step_id="boundary")
        with self.assertRaises(DefinitionError):
            sequence.add_function(identity, Ref("boundary"), step_id="bad")
        for part in (-1, True, object()):
            with self.assertRaises(DefinitionError):
                Ref("identity")[part]
        with self.assertRaises(DefinitionError):
            Sequence("../bad")

    def test_strict_json_and_reference_ambiguity(self):
        for bad in (object(), (1, 2), {1: "x"}, float("nan"), float("inf"), "\ud800"):
            with self.subTest(kind=type(bad)), self.assertRaises(InvalidValueError):
                Sequence("test").add_function(identity, bad)
        sequence = Sequence("test")
        sequence.add_function(identity, {"ref": {"step": "ordinary data"}})
        self.assertIn("dict", sequence.manifest["steps"][0]["args"][0])

    def test_bounded_strict_json_parser(self):
        for payload in (b'{"a":1,"a":2}', b'NaN', b'1e999', b'"\\ud800"', b'[' * 100):
            with self.subTest(payload=payload[:30]), self.assertRaises(InvalidValueError):
                decode_json(payload)
        with self.assertRaises(InvalidValueError):
            encode_json("x" * MAX_BYTES)
        value = {"unicode": "hello 👋", "number": [1, 2.5, True, None]}
        self.assertEqual(decode_json(encode_json(value)), value)
