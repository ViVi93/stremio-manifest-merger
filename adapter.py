"""Private redirect-only Stremio manifest adapter (Python standard library)."""
import copy
import hmac
import json
import re
import threading
import os
import secrets
import tempfile
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024


def read_json(path):
    with open(path, 'rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('invalid configuration')
    return json.loads(data)


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.stage-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_snapshot(root):
    root = Path(root)
    if (root/'snapshot.json').exists():
        snapshot = read_json(root/'snapshot.json')
        return snapshot['manifests'], snapshot['upstreams']
    urls = read_json(root/'upstreams.json')
    return [read_json(root/f'manifest-{i}.json') for i in range(len(urls))], urls


def load_token(root, environ=None):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    environ = os.environ if environ is None else environ
    config = root/'config.json'
    token = environ.get('ADAPTER_TOKEN')
    if token is None:
        token = read_json(config)['token'] if config.exists() else secrets.token_urlsafe(32)
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
        raise ValueError('invalid configuration')
    atomic_json(config, {'token': token})
    return token
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
import http.client
import ipaddress
import socket
import ssl
import time


def validate_url(url):
    if not isinstance(url, str) or not url.isascii() or re.search(r'[\x00-\x20\x7f\\\\]', url):
        raise ValueError('invalid configuration')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or not parsed.path.endswith('/manifest.json')
            or any(p in ('.','..') for p in unquote(unquote(parsed.path)).split('/'))):
        raise ValueError('invalid configuration')
    return parsed


def validate_snapshot(docs, urls):
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100 or len(docs) != len(urls):
        raise ValueError('invalid configuration')
    for url in urls:
        validate_url(url)
    for doc in docs:
        if not isinstance(doc, dict):
            raise ValueError('invalid configuration')
        for field in ['types', 'idPrefixes', 'resources', 'catalogs']:
            if not isinstance(doc.get(field, []), list):
                raise ValueError('invalid configuration')
        if not doc.get('types') or not doc.get('resources') or 'catalogs' not in doc:
            raise ValueError('invalid configuration')
        if not all(isinstance(v, str) and v for v in doc['types'] + doc.get('idPrefixes', [])):
            raise ValueError('invalid configuration')
        for cat in doc['catalogs']:
            if not isinstance(cat, dict) or not all(isinstance(cat.get(k), str) and cat[k] for k in ['type','id']):
                raise ValueError('invalid configuration')
        for r in doc['resources']:
            if isinstance(r, str):
                continue
            if not isinstance(r, dict) or not isinstance(r.get('name'), str):
                raise ValueError('invalid configuration')
            for key in ['types','idPrefixes']:
                if key in r and (not isinstance(r[key], list) or not all(isinstance(v, str) for v in r[key])):
                    raise ValueError('invalid configuration')


def fetch_manifest(url):
    parsed = validate_url(url)
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('upstream unavailable')
    address = addresses[0][4][0]
    # Pin the validated address while retaining hostname TLS verification and Host.
    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((address, 443), timeout=self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except BaseException:
                raw.close()
                raise
    connection = PinnedHTTPS(parsed.hostname, timeout=10, context=ssl.create_default_context())
    deadline = time.monotonic() + 30
    try:
        connection.request('GET', parsed.path, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('upstream unavailable')
        data = bytearray()
        while len(data) <= MAX_BYTES:
            if time.monotonic() > deadline:
                raise ValueError('upstream unavailable')
            chunk = response.read1(min(65536, MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise ValueError('upstream unavailable')
        return json.loads(data)
    finally:
        connection.close()


def refresh(root, fetch=fetch_manifest):
    root = Path(root)
    urls = read_json(root/'upstreams.json')
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
        raise ValueError('invalid configuration')
    for url in urls:
        validate_url(url)
    docs = [fetch(url) for url in urls]  # Deliberately sequential: one upstream request in flight.
    validate_snapshot(docs, urls)
    if len(json.dumps({'upstreams': urls, 'manifests': docs}).encode()) > MAX_BYTES:
        raise ValueError('invalid configuration')
    atomic_json(root/'snapshot.json', {'upstreams': urls, 'manifests': docs})


class BoundedServer(ThreadingHTTPServer):
    max_workers = 16
    connection_timeout = 5
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(self.max_workers)
        super().__init__(*args)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.connection_timeout)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # Never emit request paths, headers, credentials, or tracebacks.


def make_server(docs, urls, token, host='127.0.0.1', port=7778):
    validate_snapshot(docs, urls)
    merged = merge_manifests(docs)
    payload = json.dumps(merged, ensure_ascii=True).encode()
    if len(payload) > MAX_BYTES:
        raise ValueError('invalid configuration')
    owners = {}
    for i, d in enumerate(docs):
        for c in d['catalogs']:
            owners.setdefault((c['type'], c['id']), i)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self.reply(code, b'{"error":"request rejected"}')

        def do_POST(self):
            self.reply(405, b'{"error":"method not allowed"}')

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

        def reply(self, status, body=b'', location=None):
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Access-Control-Allow-Origin', '*')
            if location:
                self.send_header('Location', location)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = self.path.split('/', 2)
            if len(parts) != 3 or not hmac.compare_digest(parts[1], token):
                self.reply(404, b'{"error":"not found"}')
                return
            route, separator, query = parts[2].partition('?')
            if route == 'manifest.json' and not separator:
                self.reply(200, payload)
                return
            match = re.fullmatch(r'(catalog|meta|stream|subtitles)/([^/]+)/([^/]+?)(/[^/]+)?\.json', route)
            decoded = unquote(unquote(route))
            unsafe = (len(self.path) > 8192 or re.search(r'[\x00-\x1f\x7f\\\\]', decoded + unquote(unquote(query)))
                      or any(p in ('.', '..', '.json', '..json', '...json') for p in decoded.split('/'))
                      or '://' in unquote(unquote(query)))
            if not match or unsafe:
                self.reply(404, b'{"error":"not found"}')
                return
            resource, kind, identifier, extra = match.groups()
            kind, identifier = unquote(kind), unquote(identifier)
            if resource == 'catalog':
                index = owners.get((kind, identifier))
            else:
                index = 0 if kind in merged['types'] and not extra else None
            if index is None:
                self.reply(404, b'{"error":"not found"}')
                return
            self.reply(307, location=urls[index].removesuffix('/manifest.json') + '/' + route + (separator + query if separator else ''))

    return BoundedServer((host, port), Handler)


def merge_manifests(docs):
    def union(values):
        return list(dict.fromkeys(values))
    types = union(t for d in docs for t in d.get('types', []))
    prefixes = union(p for d in docs for p in d.get('idPrefixes', []))
    catalogs = {}
    resources = {}
    for d in docs:
        for c in d['catalogs']:
            catalogs.setdefault((c['type'], c['id']), copy.deepcopy(c))
        for r in d['resources']:
            name = r if isinstance(r, str) else r['name']
            item = resources.setdefault(name, {'name': name, 'types': [], 'idPrefixes': []})
            item['types'] = union(item['types'] + (types if isinstance(r, str) else r.get('types', d['types'])))
            item['idPrefixes'] = union(item['idPrefixes'] + (prefixes if isinstance(r, str) else r.get('idPrefixes', d.get('idPrefixes', []))))
    return {'id': 'local.private.aggregate', 'name': 'Aggregate', 'version': '1.0.0',
            'types': types, 'idPrefixes': prefixes, 'catalogs': list(catalogs.values()),
            'resources': list(resources.values())}


def main():
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Private redirect-only manifest adapter')
    parser.add_argument('command', choices=['serve', 'check', 'refresh'])
    parser.add_argument('--private-dir', type=Path, default=Path(__file__).resolve().parent/'private')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9141)
    args = parser.parse_args()
    try:
        if args.command == 'refresh':
            refresh(args.private_dir)
        docs, urls = load_snapshot(args.private_dir)
        validate_snapshot(docs, urls)
        if args.command != 'serve':
            print(f'Snapshot valid: {len(docs)} parts, {len(merge_manifests(docs)["catalogs"])} catalogs.')
            return 0
        token = load_token(args.private_dir)
        with make_server(docs, urls, token, args.host, args.port) as server:
            print('Adapter ready (access logging disabled).', flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('Adapter operation failed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
