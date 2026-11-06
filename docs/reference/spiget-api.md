# Spiget API (SpigotMC mirror) reference (verified 2026-11-06)

Sources: Spiget's Swagger spec (`https://raw.githubusercontent.com/SpiGetOrg/Documentation/master/swagger.json`,
linked from `https://spiget.org/documentation/`) and **real requests** to `https://api.spiget.org/v2` and
`https://cdn.spiget.org` from the sandbox (all IDs and values below are real). Used by `backend/apps/modpacks/spiget.py`.

## 1. Basics
| Item | Value |
|---|---|
| Base URL | `https://api.spiget.org/v2` (Express behind Cloudflare). Read-only, no auth |
| User-Agent | Send an identifying UA (the panel sends `MCPANEL_USER_AGENT`) |
| `fields` param | Comma-separated projection, nested `icon.url` works (so the inline base64 `icon.data` can be skipped); nested `file.type` did **not** work, request `file` |
| Paging | `size`, `page` (**1-based**), `sort` with `+`/`-` prefix (`-downloads`, `-releaseDate`). Headers `X-Page-Size`, `X-Page-Index`, `X-Page-Count`; no total count |
| Missing fields | Some resources omit `premium` entirely (treat a `price` as premium too) |

## 2. Endpoints used
| Method + path | Notes |
|---|---|
| `GET /search/resources/{query}?field=name&size&page&sort=-downloads&fields=` | Resource[]; **no match = HTTP 404 with body `[]`** (handle as empty). Query is a path segment: encode it, including dots (`..` would otherwise be a dot-segment) |
| `GET /resources/free?size&page&sort=-downloads` | Popular free resources (the panel's empty-query listing) |
| `GET /resources/{id}` | Full resource (below) |
| `GET /resources/{id}/versions?size&sort=-releaseDate&fields=id,name,releaseDate,downloads` | `[{id, name, releaseDate, downloads, rating, uuid, resource}]` — **no file, no hash, no Minecraft versions per version** |
| `GET /resources/{id}/versions/latest` | Same shape, newest version |
| `GET /authors/{id}?fields=id,name` | `{id, name}` (search results only carry `author.id`) |
| `GET /resources/{id}/download` | `302`. Hosted on SpigotMC: `Location: https://cdn.spiget.org/file/spiget-resources/{id}.jar` (or `.zip`), `X-Spiget-File-Source: cdn`. External: `Location: <author's URL>`, `X-Spiget-File-Source: external`. Premium resources **also** redirect to the CDN — the panel never downloads them |
| `GET /resources/{id}/versions/{vid}/download` | `302` to `https://spigotmc.org/resources/{id}/download?version={vid}` → `301` to `www.spigotmc.org` → **403 Cloudflare challenge** (`cf-mitigated: challenge`) for an automated client. Not usable; the panel does not try to get around it |

## 3. Resource fields (example: Chunky, id 81534)
```json
{"id": 81534, "name": "Chunky", "tag": "Pre-generates chunks, quickly, efficiently, and safely",
 "external": false, "premium": false,
 "file": {"type": ".jar", "size": 390.2, "sizeUnit": "KB", "url": "resources/chunky.81534/download?version=658870"},
 "testedVersions": ["1.13", "…", "1.21", "26.1", "26.2", "26.3"],
 "version": {"id": 658870}, "author": {"id": 9610}, "category": {"id": 18},
 "icon": {"url": "data/resource_icons/81/81534.jpg?1612919814", "data": "<base64 image>"},
 "downloads": 650713, "likes": 83, "rating": {"count": 176, "average": 5},
 "releaseDate": 1594853100, "updateDate": 1793762275,
 "description": "<base64 of HTML>", "sourceCodeLink": "https://github.com/pop4959/Chunky",
 "links": {"<base64 label>": "<url>", "…": "…"}}
```
* External: `"external": true, "file": {"type": "external", "size": 0, "externalUrl": "https://…"}` (e.g. LuckPermsGUI 53460).
* Archives: `file.type` `.zip` (e.g. TradeGUI 140306); the CDN copy is then `{id}.zip`.
* Premium: `premium: true` plus `price`/`currency` (VoteParty 987: 7 usd).
* `testedVersions` are author-declared, for the whole resource, at "major.minor" granularity.
* `description` is base64-encoded **HTML** with inline styles, links and spoilers. Render as text only.
* Icons: `https://www.spigotmc.org/{icon.url}` loads without a challenge (checked with an honest UA).
* Categories (`/categories`) have no parent information in the API; proxy (BungeeCord) plugins are only
  reliably told apart by their jar (`bungee.yml` instead of `plugin.yml`).

## 4. The CDN mirror (`cdn.spiget.org`)
Backblaze B2 behind Cloudflare. A `HEAD`/`GET` on `/file/spiget-resources/{id}.jar` returns:

| Header | Meaning |
|---|---|
| `x-bz-content-sha1` | SHA-1 that B2 computed when Spiget stored the file (verified: equals the SHA-1 of the bytes) |
| `x-bz-upload-timestamp` | When Spiget stored it (ms since epoch) |
| `content-length`, `content-type: application/java-archive`, `cache-control: max-age=1800` | |

Missing file: `404` JSON. The mirror holds **one file per resource, the latest at Spiget's last crawl**, and can lag:

| Resource | Mirror stored | Jar's `plugin.yml` version | SpigotMC latest |
|---|---|---|---|
| LuckPerms 28140 | 2026-10-19 | 5.5.71 | 5.5.71 (released 2026-08-06) → current |
| Chunky 81534 | 2026-05-05 | 1.5.3 | 1.5.66 (released 2026-11-04) → **outdated** |
| KranciBottleXP 140307 (new) | 2026-11-05, minutes after release | — | — |

So "stored after the latest version's `releaseDate`" is the panel's freshness test; the installed version is
read from the jar. Both LuckPerms 5.5.71 and Chunky 1.5.3 from the mirror are byte-identical to the Modrinth
releases (Modrinth's `version_files` identifies them), so the content manager then treats them as Modrinth files.

**Spiget publishes no hash of SpigotMC's original upload.** The B2 SHA-1 only proves the transfer matches what
Spiget stored. See `docs/SECURITY.md` for how the panel handles this.
