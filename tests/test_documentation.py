from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import re
import runpy
import tempfile
import unittest


class DocumentationTests(unittest.TestCase):
    def test_readme_python_examples(self):
        root = Path(__file__).resolve().parents[1]
        snippets = re.findall(r"```python\n(.*?)\n```", (root / "README.md").read_text(), re.S)
        self.assertEqual(len(snippets), 2)
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            try:
                os.chdir(directory)
                for source in snippets:
                    exec(compile(source, str(root / "README.md"), "exec"), {"__name__": "__main__"})
            finally:
                os.chdir(previous)

    def test_bundled_example(self):
        root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(str(root / "examples" / "greeting.py"))
        with redirect_stdout(StringIO()):
            result = namespace["script"].run()
        self.assertEqual(result.results["display"], {"displayed": True})
