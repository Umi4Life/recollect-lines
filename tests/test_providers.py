import json
import tempfile
import unittest
from pathlib import Path

from recollect_lines.providers import ProviderConfigError, load_providers_config, validate_providers_document


class ProviderConfigFileTests(unittest.TestCase):
    def test_load_from_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(json.dumps({
                "providers": {
                    "local": {
                        "kind": "openai-compatible",
                        "base_url": "http://127.0.0.1:8765/v1",
                        "api_key_env": "LOCAL_KEY",
                        "default_model": "local-coder",
                        "allow_insecure_http": True,
                    }
                }
            }) + "\n")
            providers = load_providers_config(path)
            self.assertEqual(providers["local"].default_model, "local-coder")

    def test_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json")
            with self.assertRaises(ProviderConfigError):
                load_providers_config(path)


if __name__ == "__main__":
    unittest.main()
