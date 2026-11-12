import base64
import unittest
from scenario import scenario
from transport import ReplaySource, fetch

class ScenarioTests(unittest.TestCase):
    def test_local_source_matches_declared_routes(self):
        routes = scenario()
        self.assertGreater(len(routes),0)
        with ReplaySource(routes) as server:
            for key, responses in routes.items():
                method, path = key.split(' ',1)
                if method != 'GET':
                    continue
                response = responses[0]
                status, headers, raw = fetch(server.url+path,headers=response.get('require_headers'))
                self.assertEqual(status,response.get('status',200),key)
                expected = base64.b64decode(response['base64']) if 'base64' in response else response.get('text','').encode()
                self.assertEqual(raw,expected,key)

    def test_export_is_deterministic(self):
        self.assertEqual(scenario(),scenario())
