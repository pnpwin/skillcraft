import http.client
import json
from functools import partial
from http.server import HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from tests import SKILL
from scripts.generate_review import ReviewHandler


class ReviewFeedbackAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        run = self.workspace / 'run-a'
        (run / 'outputs').mkdir(parents=True)
        (run / 'outputs' / 'result.txt').write_text('result')
        self.feedback_path = self.workspace / 'feedback.json'
        self.token = 'test-capability-token'
        handler = partial(
            ReviewHandler,
            self.workspace,
            'demo',
            self.feedback_path,
            {},
            None,
            authorization_token=self.token,
        )
        self.server = HTTPServer(('127.0.0.1', 0), handler)
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.host = f'127.0.0.1:{self.server.server_port}'

    def _stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)

    def request(self, method, path, *, host=None, origin=None, authorization=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {'Host': host or self.host}
        if origin is not None:
            headers['Origin'] = origin
        if authorization is not None:
            headers['Authorization'] = authorization
        if body is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def payload(self):
        return json.dumps({
            'schema_version': 1,
            'reviews': [{
                'run_id': 'run-a', 'snapshot_hash': None, 'reviewed': False,
                'feedback': 'draft', 'timestamp': '2026-11-19T00:00:00Z',
            }],
            'status': 'in_progress',
        })

    def test_feedback_api_denies_missing_or_incorrect_authorization(self):
        for authorization in (None, 'Bearer incorrect'):
            status, _ = self.request('GET', '/api/feedback', authorization=authorization)
            self.assertEqual(status, 403)
            status, _ = self.request(
                'POST', '/api/feedback', origin=f'http://{self.host}',
                authorization=authorization, body=self.payload(),
            )
            self.assertEqual(status, 403)
        self.assertFalse(self.feedback_path.exists())

    def test_feedback_api_rejects_untrusted_host_and_origin(self):
        auth = f'Bearer {self.token}'
        for kwargs in (
            {'host': f'evil.test:{self.server.server_port}', 'origin': f'http://{self.host}'},
            {'origin': f'http://evil.test:{self.server.server_port}'},
            {'host': f'localhost:{self.server.server_port}', 'origin': f'http://{self.host}'},
            {'origin': None},
        ):
            status, _ = self.request(
                'POST', '/api/feedback', authorization=auth,
                body=self.payload(), **kwargs,
            )
            self.assertEqual(status, 403)
        self.assertFalse(self.feedback_path.exists())

    def test_authenticated_flow_and_page_capability_handling(self):
        status, html = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), html)
        status, _ = self.request(
            'POST', '/api/feedback', origin=f'http://{self.host}',
            authorization=f'Bearer {self.token}', body=self.payload(),
        )
        self.assertEqual(status, 200)
        status, saved = self.request(
            'GET', '/api/feedback', authorization=f'Bearer {self.token}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(saved)['status'], 'in_progress')

        viewer = (SKILL / 'assets/viewer.html').read_text()
        self.assertIn('new URLSearchParams(location.hash.slice(1)).get("cap")', viewer)
        self.assertIn('"Authorization": `Bearer ${feedbackCapability}`', viewer)


if __name__ == '__main__':
    unittest.main()
