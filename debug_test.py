import sys
sys.path.insert(0, 'scripts')
from provider_admission_prober import ProviderAdmissionProber
import unittest
from unittest.mock import patch, MagicMock
import requests

class DebugTest(unittest.TestCase):
    def test_debug(self):
        prober = ProviderAdmissionProber()
        
        # Mock successful response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{
                "message": {
                    "content": "test response",
                    "tool_calls": []  # Empty list
                }
            }]
        }
        
        with patch('requests.Session.post') as mock_post:
            mock_post.return_value = mock_response
            result = prober.probe_provider("gemini", "gemini-3.1-flash-lite")
            print(f"Result: {result}")
            print(f"Native tool call: {result['native_tool_call']}")
            print(f"Type: {type(result['native_tool_call'])}")

if __name__ == "__main__":
    unittest.main()