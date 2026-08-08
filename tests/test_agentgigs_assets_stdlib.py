import unittest
from pathlib import Path

from markets.agentgigs.assets import (
    RemoteAssetSafetyError,
    assert_public_https_url,
    extract_source_asset_refs,
    safe_filename,
    supported_source_name,
)


class AgentGigsAssetContractTests(unittest.TestCase):
    def test_extracts_job_and_message_source_attachments_but_not_deliverables(self):
        details = {
            "job": {
                "attachments": [
                    {
                        "id": "input-1",
                        "name": "input.json",
                        "download_url": "https://files.example.com/input.json?sig=one",
                        "size": 12,
                    }
                ]
            },
            "deliverable_files": [
                {
                    "file_name": "our-output.csv",
                    "download_url": "https://files.example.com/our-output.csv",
                    "file_size": 99,
                }
            ],
        }
        messages = [
            {
                "id": "message-1",
                "message": "Use this cleanup source",
                "attachment_name": "source.csv",
                "attachment_url": "https://files.example.com/source.csv?sig=two",
                "attachment_size": 20,
            }
        ]
        refs = extract_source_asset_refs(details, messages)
        self.assertEqual([ref.name for ref in refs], ["input.json", "source.csv"])
        self.assertEqual([ref.source_kind for ref in refs], ["job_attachment", "message_attachment"])
        self.assertNotIn("our-output.csv", [ref.name for ref in refs])

    def test_attachment_without_download_url_is_not_guessed(self):
        refs = extract_source_asset_refs(
            {"job": {"attachments": [{"name": "input.json", "file_path": "job/input.json"}]}},
            [],
        )
        self.assertEqual(refs, [])

    def test_launch_lane_accepts_only_json_and_csv_and_sanitizes_names(self):
        self.assertTrue(supported_source_name("input.JSON"))
        self.assertTrue(supported_source_name("input.csv"))
        self.assertFalse(supported_source_name("instructions.pdf"))
        self.assertEqual(safe_filename("../../source data.csv", "agentgigs:abc"), "source_data.csv")

    def test_watcher_ingests_assets_before_dispatching_awarded_work(self):
        source = (Path(__file__).resolve().parents[1] / "control" / "management" / "commands" / "run_agentgigs_watcher.py").read_text()
        self.assertIn("sync_awarded_agentgigs_assets", source)
        self.assertLess(source.index('result["assets"]'), source.index('result["dispatch"]'))

    def test_url_guard_rejects_non_https_private_and_credential_urls(self):
        for url in (
            "http://8.8.8.8/file.csv",
            "https://127.0.0.1/file.csv",
            "https://10.0.0.4/file.csv",
            "https://user:pass@8.8.8.8/file.csv",
        ):
            with self.subTest(url=url), self.assertRaises(RemoteAssetSafetyError):
                assert_public_https_url(url)
        # Public literal avoids DNS in this deterministic test.
        assert_public_https_url("https://8.8.8.8/file.csv")


if __name__ == "__main__":
    unittest.main()
