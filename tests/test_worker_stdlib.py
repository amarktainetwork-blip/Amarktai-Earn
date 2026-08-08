import json
import tempfile
import unittest
from pathlib import Path
from workers.base import WorkRequest
from workers.structured_data.worker import StructuredDataWorker
from workers.qa.deterministic import verify_csv

class StructuredDataWorkerTests(unittest.TestCase):
    def test_json_to_csv_with_independent_qa(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "input.json"
            source.write_text(json.dumps([{"name":"A","value":1},{"name":"B","value":2}]), encoding="utf-8")
            result = StructuredDataWorker().execute(WorkRequest(job_id="j1", workspace=root / "work", inputs={"operation":"json_to_csv","source":str(source)}))
            self.assertTrue(result.ok)
            qa = verify_csv(result.artifacts[0], expected_rows=2, required_columns=["name","value"])
            self.assertTrue(qa.passed)

if __name__ == "__main__":
    unittest.main()
