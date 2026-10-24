import contextlib
import http.client
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest


def module():
    try:
        return importlib.import_module('adapter')
    except ModuleNotFoundError:
        raise AssertionError('adapter implementation missing') from None


def manifests():
    return [dict(id='private.part', name='Private', version='1.0.0',
                 types=['movie'] if i == 0 else ['series'],
                 idPrefixes=['a'] if i == 0 else ['b'],
                 resources=['catalog', 'meta', 'stream', 'subtitles'] if i == 0 else ['catalog','meta'],
                 catalogs=[{'type': 'movie' if i == 0 else 'series', 'id': 'cat/'+str(i)}])
            for i in range(2)]


class MergeTests(unittest.TestCase):
    def test_manifest_union_preserves_catalogs_and_all_resource_types(self):
        a = module()
        docs = manifests()
        merged = a.merge_manifests(docs)
        self.assertEqual(merged['catalogs'], docs[0]['catalogs']+docs[1]['catalogs'])
        self.assertEqual(merged['types'], ['movie','series'])
        self.assertEqual(merged['idPrefixes'], ['a','b'])
        resources = {r['name']:r for r in merged['resources']}
        for name in ['meta','stream','subtitles']:
            self.assertEqual(resources[name]['types'], ['movie','series'])
        self.assertNotIn('Private', merged['name'])


class SnapshotTests(unittest.TestCase):
    def test_cli_check_and_refresh_errors_are_sanitized(self):
        script = str(Path(__file__).with_name('adapter.py'))
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, script, 'check', '--private-dir', temp], capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, b'Adapter operation failed.\n')
            root = Path(temp)
            (root/'upstreams.json').write_text(json.dumps(['https://example.com/manifest.json']*2))
            for i,d in enumerate(manifests()):
                (root/f'manifest-{i}.json').write_text(json.dumps(d))
            result = subprocess.run([sys.executable, script, 'check', '--private-dir', temp], capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'Snapshot valid: 2 parts, 2 catalogs.\n')

    def test_refresh_validates_all_before_atomic_snapshot_replace(self):
        a = module()
        self.assertTrue(hasattr(a, 'refresh'), 'refresh missing')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            urls = ['https://example.com/one/manifest.json','https://example.net/two/manifest.json']
            (root/'upstreams.json').write_text(json.dumps(urls))
            original = {'upstreams':urls, 'manifests':manifests()}
            (root/'snapshot.json').write_text(json.dumps(original))
            def bad_fetch(url):
                return manifests()[0] if url == urls[0] else {'catalogs': 'broken'}
            with self.assertRaises(ValueError):
                a.refresh(root, fetch=bad_fetch)
            self.assertEqual(json.loads((root/'snapshot.json').read_text()), original)
            a.refresh(root, fetch=lambda url: manifests()[urls.index(url)])
            self.assertEqual(a.load_snapshot(root), (manifests(), urls))
            self.assertEqual((root/'snapshot.json').stat().st_mode & 0o777, 0o600)
            for url in ['http://example.org/manifest.json', 'https://127.0.0.1/manifest.json', 'https://[::1]/manifest.json', 'https://example.com/x/../manifest.json', 'https://example.com/manifest.json?x=1']:
                with self.assertRaises(ValueError):
                    a.fetch_manifest(url)

    def test_private_snapshot_and_token_persistence(self):
        a = module()
        self.assertTrue(hasattr(a, 'load_snapshot'), 'snapshot loader missing')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'upstreams.json').write_text(json.dumps(['https://example.com/one/manifest.json','https://example.net/two/manifest.json']))
            for i,d in enumerate(manifests()):
                (root/f'manifest-{i}.json').write_text(json.dumps(d))
            docs, urls = a.load_snapshot(root)
            self.assertEqual(docs, manifests())
            token = a.load_token(root, {})
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(a.load_token(root, {}), token)
            self.assertEqual((root/'config.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(a.load_token(root, {'ADAPTER_TOKEN': 'a'*32}), 'a'*32)
            self.assertEqual(a.load_token(root, {}), 'a'*32)


class HTTPTests(unittest.TestCase):
    @contextlib.contextmanager
    def server(self):
        a = module()
        self.assertTrue(hasattr(a, 'make_server'), 'HTTP service missing')
        server = a.make_server(manifests(), ['https://example.com/one/manifest.json', 'https://example.net/two/manifest.json'], 'test-secret', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def request(self, server, path):
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.request('GET', path)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_server_bounds_manifest_and_sanitizes_errors(self):
        a = module()
        with self.server() as server:
            self.assertEqual(getattr(server, 'max_workers', None), 16)
            self.assertEqual(getattr(server, 'connection_timeout', None), 5)
            status, headers, body = self.request(server, '/test-secret/manifest.json')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), a.merge_manifests(manifests()))
            self.assertEqual(headers['Cache-Control'], 'no-store')
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            connection.request('POST', '/test-secret/manifest.json')
            response = connection.getresponse()
            self.assertEqual(response.status, 405)
            self.assertEqual(response.read(), b'{"error":"method not allowed"}')
            connection.close()
        docs = manifests()
        docs[0]['catalogs'][0]['name'] = 'a' * (a.MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            a.make_server(docs, ['https://example.com/manifest.json']*2, 'test-secret', port=0)

    def test_auth_and_path_allowlist_reject_without_leaks(self):
        with self.server() as server:
            for path in ['/manifest.json','/wrong/manifest.json', '/test-secret/proxy/https://example.org', '/test-secret/meta/movie/%2e%2e.json', '/test-secret/meta/movie/a%0d%0aX.json', '/test-secret/meta/movie/a/extra.json', '/test-secret/catalog/movie/unknown.json', '/test-secret/meta/movie/%252e%252e.json', '/test-secret/meta/movie/a.json?url=http://127.0.0.1/']:
                status, headers, body = self.request(server, path)
                self.assertEqual(status, 404)
                self.assertNotIn('Location', headers)
                self.assertEqual(body, b'{"error":"not found"}')

    def test_catalog_redirect_preserves_encoded_path_and_query(self):
        with self.server() as server:
            status, headers, body = self.request(server, '/test-secret/catalog/series/cat%2F1/skip=1%2F2.json?search=a%2Fb&x=1')
            self.assertEqual(status, 307)
            self.assertEqual(headers['Location'], 'https://example.net/two/catalog/series/cat%2F1/skip=1%2F2.json?search=a%2Fb&x=1')
            self.assertEqual(body, b'')
            status, headers, _ = self.request(server, '/test-secret/catalog/series/cat%2F1.json?search=two%20words')
            self.assertEqual(status, 307)
            self.assertTrue(headers['Location'].endswith('?search=two%20words'))
            for resource in ['meta','stream','subtitles']:
                status, headers, _ = self.request(server, '/test-secret/'+resource+'/series/a%2Fb.json?x=%2F')
                self.assertEqual(status, 307)
                self.assertEqual(headers['Location'], 'https://example.com/one/'+resource+'/series/a%2Fb.json?x=%2F')


class RealSnapshotTests(unittest.TestCase):
    def test_real_snapshot_over_loopback_has_no_drops_and_correct_owners(self):
        a = module()
        root = Path(__file__).with_name('private')
        if not (root/'upstreams.json').exists():
            self.skipTest('private snapshot not present')
        docs, urls = a.load_snapshot(root)
        merged = a.merge_manifests(docs)
        owners = {}
        for i, doc in enumerate(docs):
            for catalog in doc['catalogs']:
                owners.setdefault((catalog['type'], catalog['id']), (i,catalog))
        self.assertEqual(len(merged['catalogs']), len(owners))
        self.assertNotIn('configureUrl', merged.get('behaviorHints', {}))
        self.assertTrue(merged['catalogs'] == [value[1] for value in owners.values()])
        for resource in merged['resources']:
            if resource['name'] in ['meta','stream','subtitles']:
                self.assertEqual(set(resource['types']), {t for d in docs for t in d['types']})
        server = a.make_server(docs, urls, 'snapshot-test', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        from urllib.parse import quote
        try:
            client = HTTPTests()
            status, _, body = client.request(server, '/snapshot-test/manifest.json')
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body) == merged)
            for (kind, identifier), (index, catalog) in owners.items():
                route = '/catalog/'+quote(kind, safe='')+'/'+quote(identifier, safe='')+'.json'
                status, headers, body = client.request(server, '/snapshot-test'+route)
                # Avoid printing private URLs on an assertion failure.
                self.assertTrue(status == 307 and headers.get('Location') == urls[index].removesuffix('/manifest.json')+route)
                self.assertEqual(body, b'')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
