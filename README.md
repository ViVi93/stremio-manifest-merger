# Stremio Manifest Merger

Combine several **parts of the same Stremio-compatible addon** into one manifest for clients such as Nuvio. Python 3.11+, standard library only; no pip dependencies.

Unofficial, independent project. Not affiliated with or endorsed by Stremio, Nuvio, or any addon provider. No upstream application code, logos, catalog data, or credentials are bundled.

## What it does

- Merges catalog descriptors, types, ID prefixes, and resource declarations.
- Deduplicates catalogs by `(type, id)`, retaining the first complete descriptor and owner.
- Serves one token-protected manifest.
- Redirects catalog requests with HTTP **307** to the part that owns the catalog.
- Redirects metadata, stream, and subtitle requests to the **first URL** in the configured list.
- Preserves encoded resource paths, catalog extras, and query strings after validation.
- Leaves playback and stream resolution with the original provider: this service is **not a video proxy**.
- Provides explicit, validated manifest refresh with atomic snapshot replacement.

A deployed instance has been tested with 20 manifest parts / 352 unique catalogs, and its user confirmed operation in Nuvio. That does not establish compatibility with every addon or client.

## Important: not a universal addon combiner

Use this for split manifests whose **first endpoint resolves metadata, streams, and subtitles for content from all parts**. Put that primary manifest first. Verify this assumption for your provider before use.

Do not combine unrelated addons that need separate metadata/stream resolvers. Resource capabilities are unioned, but noncatalog requests are not dispatched to individual providers or fanned out. String resources expand over all merged types/prefixes; explicit object restrictions are retained. Conflicting catalog descriptors with the same `(type,id)` are not reconciled: first occurrence wins.

The merger does **not bypass the original provider's manifest trimming**: descriptors or picker values already omitted upstream cannot be recovered. It also does not bypass a consuming client's own manifest limits. Large merged manifests may work in Nuvio but fail in clients with smaller limits.

## Quick start

```sh
git clone https://github.com/ViVi93/stremio-manifest-merger.git
cd stremio-manifest-merger
python3 -m unittest -v
mkdir -m 700 private
```

Using a private editor, create `private/upstreams.json` as a JSON array of your real **HTTPS** manifest URLs, in order. `examples/upstreams.example.json` shows the format but contains intentionally nonfunctional example URLs. Never post your real file in an issue: addon paths can contain debrid keys and other credentials.

```sh
chmod 600 private/upstreams.json
python3 adapter.py refresh
python3 adapter.py check
python3 adapter.py serve --host 127.0.0.1 --port 9141
```

`refresh` fetches all upstream manifests sequentially and writes `private/snapshot.json` only after validating the complete set. `serve` loads that snapshot once at startup. On first start it generates a random capability token in `private/config.json` (0600).

Read the token locally from that file, then construct your install URL privately:

```text
https://YOUR-HTTPS-HOST/<token>/manifest.json
```

The placeholder is not a working hosted service. You must host your own instance. An HTTPS reverse proxy or tunnel should target `http://127.0.0.1:9141`; disable sensitive URL/access logs. Keep the application port private. A browser-login gateway typically cannot be completed by an addon client; the secret path is this service's access control.

For a quick local HTTP check, open `http://127.0.0.1:9141/<token>/manifest.json` on the hosting machine. A phone's `localhost` is its own phone, not your server. Do not expose the service with `--host 0.0.0.0` as a shortcut to public hosting.

The full install URL is a bearer credential: anyone holding it can retrieve the merged manifest and follow redirects to your configured endpoints. It is not per-user authentication.

### Configuration

| Option | Default / behavior |
| --- | --- |
| `--private-dir` | `private/` beside `adapter.py`; applies to every command |
| `--host` | `127.0.0.1` |
| `--port` | `9141` |
| `ADAPTER_TOKEN` | Optional 32–128 URL-safe characters; overrides and persists the token |

Avoid putting tokens in shell history, command-line arguments, screenshots, proxy logs, or source control. To rotate, set a fresh `ADAPTER_TOKEN` securely for one startup, then reinstall the new URL in clients. Existing configuration otherwise keeps the same token across restarts.

The loader also accepts legacy `private/manifest-0.json`, `manifest-1.json`, etc. alongside `upstreams.json` if no combined snapshot exists. Normal setup should use `refresh`.

## Updates

```sh
python3 adapter.py refresh
python3 adapter.py check
# Restart your serving process after a successful refresh.
```

- **Live content/backend fixes:** inherited through redirects; no manifest refresh normally needed.
- **Changed filters, catalog types/IDs, prefixes, or resources:** refresh the snapshot and restart. The install URL stays the same if the token is unchanged; your client may need to reload the addon.
- **Changed saved configuration or part packing:** regenerate URLs with the provider, replace `private/upstreams.json`, then refresh and restart. Refreshing old encoded URLs cannot introduce new configuration flags.
- **Changed resource protocols:** may require adapter code changes.

There is **no automatic refresh, changelog monitor, Discord bot, or deployment integration** in this repository. A failed refresh leaves the prior snapshot intact. Keep backups and check the reported catalog count before restarting.

## Known limitations

- Clients must follow cross-host resource redirects.
- Metadata/stream/subtitle resolution always uses the first configured endpoint.
- Optional extra segments on noncatalog routes, including `/subtitles/type/id/extra.json`, are currently rejected. Ordinary `/subtitles/type/id.json` is supported. This is a known compatibility gap, not a claim of complete subtitle support.
- Required extras are preserved, so some catalogs intentionally stay off Home.
- Upstream branding, configuration links, and behavior hints are omitted; the manifest uses a generic identity. Only one aggregate identity is supported per client installation.
- Full URL values in query strings are rejected defensively.
- Upstream refresh accepts HTTPS port 443 only, without userinfo, query, fragment, or redirects. Providers using other manifest URL formats need adaptation.
- Generic errors protect secrets but do not provide detailed operator diagnostics.
- No Docker image, managed service, install UI, or automatic service supervisor is included.

## Security

Origin access logging is disabled. Private configuration is stored under a 0700 directory; atomic configuration/snapshot writes use 0600. Keep any manually created inputs equally restricted. The HTTP server has 16 worker slots and five-second socket timeouts; responses/snapshots are bounded to 8 MiB. This is a small private service, not a hardened general-purpose public application server.

Refresh rejects nonpublic resolved IPs and pins the validated address while retaining TLS hostname verification, preventing DNS-rebinding redirects into local services. Redirects are not followed during refresh. Fetches use ten-second socket timeouts and a thirty-second read-loop deadline; OS DNS resolution has its own timeout. Serving itself does not fetch resources.

HTTPS hosting providers, authorized clients, upstream providers, backups, and redirect `Location` headers can still expose private URLs. Disable URL/header logging at every ingress layer. Do not rely on `.gitignore` alone; inspect staged files before publishing. Use only providers and content you are authorized to access.

## Tests

```sh
python3 -m unittest -v
```

Tests cover union semantics, token persistence, loopback HTTP, encoded redirects, route rejection, response bounds, sanitized CLI failures, and atomic-refresh failure. One additional integration test runs when a private snapshot exists, otherwise it is skipped. Public CI uses synthetic fixtures and never needs real addon credentials or playback requests.

## License

MIT; see [LICENSE](LICENSE). Python itself is not bundled. This repository contains the independently implemented adapter and tests, not the upstream addon or client software.
