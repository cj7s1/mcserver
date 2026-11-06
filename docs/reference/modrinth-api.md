# Modrinth API v2 reference (verified 2026-11-03)

Sources: official OpenAPI spec `https://docs.modrinth.com/openapi.yaml` (= `raw.githubusercontent.com/modrinth/code/main/apps/docs/public/openapi.yaml`, `info.version: v2.7.0/366f528`), labrinth source (`apps/labrinth/src/models/v3/pack.rs`, `validate/modpack.rs`, `models/v2/projects.rs`), Modrinth App source (`packages/app-lib/src/api/pack/install_mrpack.rs`), itzg docs, and **real requests to `https://api.modrinth.com/v2`** (all example IDs/values below are real). Anything not confirmed is marked UNVERIFIED.
The mrpack support article (`support.modrinth.com/en/articles/8802351`) returns "Request blocked" (403) to bots; format below is taken from labrinth source plus real index files.

## 1. Basics
| Item | Value |
|---|---|
| Base URL | `https://api.modrinth.com/v2` (staging: `https://staging-api.modrinth.com/v2`). `GET /` redirects 301 to docs; `GET /v2/` -> 404 JSON |
| Auth | None needed for reads. Writes/private data: `Authorization: <PAT>` header (PAT prefix `mrp_`), no `Bearer` |
| User-Agent | Docs: you **must** send a uniquely identifying UA; library-only UAs (e.g. `okhttp/4.9.3`) risk blocking. Recommended form `github_username/project_name/1.56.0 (contact@example.com)`. Observed: an empty UA still got 200 today - do not rely on it |
| Rate limit | Per IP, **300 req/min** (same with or without token). Headers: `X-Ratelimit-Limit` (max/min), `X-Ratelimit-Remaining`, `X-Ratelimit-Reset` (seconds until window reset) |
| Observed headers | `x-ratelimit-limit: 300`, `x-ratelimit-remaining: 300`, `x-ratelimit-reset: 0` on every response (remaining did not decrement through our egress proxy). 429 body format UNVERIFIED -> back off on 429 and honour Reset |
| IDs | 8-char base62 (`shFhR8Vx`). Slugs/usernames can change -> store IDs. Files identified by sha1/sha512 hex |
| Versioning | Breaking change bumps path version; fully deprecated API versions return **410** |
| CORS | `access-control-allow-origin: *` |
| Errors | 400: `{"error":"request_error","description":"...","details":[...]}`; unknown route 404: `{"error":"not_found","description":"the requested route does not exist"}`; missing project/version: **404 with empty body** |
| Array params | Query params that take arrays are JSON strings, URL-encoded: `ids=["a","b"]`, `loaders=["fabric"]` |

## 2. Endpoint summary (read-only ones relevant to the panel; all verified with real requests)
| Method + path | Params / body | Returns |
|---|---|---|
| `GET /search` | `query`, `facets`, `index`, `offset`, `limit` | `{hits[], offset, limit, total_hits}` |
| `GET /project/{id\|slug}` | - | Project |
| `GET /projects?ids=[...]` | ids **and/or slugs** (verified mixed). 245 ids in one request verified (2026-11-06: 0.68 s, vs 1.59 s for three requests of <= 100); no documented maximum, keep the URL a few KB | Project[] |
| `GET /project/{id\|slug}/version` | `loaders`, `game_versions`, `featured`, `include_changelog` (default true) | Version[] (observed newest first) |
| `GET /project/{id\|slug}/version/{id\|number}` | number match returns the **oldest** matching version (spec) | Version |
| `GET /version/{id}` | - | Version |
| `GET /versions?ids=[...]` | `include_changelog=false` also honoured (observed) | Version[] - **order NOT preserved** (observed) |
| `GET /version_file/{hash}?algorithm=sha1\|sha512` | `multiple` (bool, default false) | Version |
| `POST /version_files` | `{"hashes":[...],"algorithm":"sha1"\|"sha512"}`. 311 hashes in one request verified (0.28 s, 432 KB response) | `{hash: Version}` (unknown hashes omitted) |
| `POST /version_file/{hash}/update?algorithm=` | `{"loaders":[...],"game_versions":[...],"version_types":[...]?}` | Version |
| `POST /version_files/update` | `{"hashes","algorithm","loaders","game_versions","version_types"?}` (`loaders`,`game_versions` required) | `{hash: latest matching Version}` |
| `GET /project/{id\|slug}/members`, `GET /team/{id}/members` | - | TeamMember[] (`[]` for org-owned projects, see 6) |
| `GET /tag/category\|loader\|game_version\|project_type\|side_type\|license` | - | tag arrays (see 7) |
| `GET /v3/organization/{id}` (v3!) | - | org incl. `members[]` (verified) |

## 3. GET /search
- `index`: `relevance` (default) \| `downloads` \| `follows` \| `newest` \| `updated`. Invalid -> 400 `invalid index 'bogus'`.
- `limit`: default 10, max 100 (`limit=101` silently returned 100). `offset`: default 0.
- Quirk (observed): `offset=5&limit=3` returned the correct hits (items 5-7) but echoed `"offset":3`. Use offsets that are multiples of `limit` for paging; don't trust the echoed offset.
- `facets`: JSON **array of arrays**. Inner array = OR, outer = AND. Flat array -> 400 `invalid type: string ..., expected a sequence`.
  - Syntax `{type}{op}{value}`, ops `:` (= `=`), `!=`, `>=`, `>`, `<=`, `<`. E.g. `[["project_type:modpack"],["categories:forge","categories:neoforge"],["versions:1.21.1"],["downloads>=1000000"]]` (all verified).
  - Common types: `project_type`, `all_project_types`, `categories` (**loaders are categories in search**: `categories:fabric`), `versions` (MC version), `open_source`, `environment`, `disclosure_types`, `client_side`/`server_side` (**deprecated**, still work: `server_side:required`, `server_side:unsupported` verified), plus `title`, `author` (`author:SHXRKIE` verified), `follows`, `project_id`, `license`, `downloads`, `created_timestamp`, `modified_timestamp` (unix).
  - `environment` values: `client_and_server`, `client_only`, `client_only_server_optional`, `singleplayer_only`, `server_only`, `server_only_client_optional`, `dedicated_server_only`, `client_or_server`, `client_or_server_prefers_both`, `unknown` (`environment:server_only` verified).
- Hit fields (real, sorted): `all_project_types, author, author_id, categories, client_side, color, date_created, date_modified, description, disclosure_types, display_categories, downloads, environment, featured_gallery, follows, gallery, icon_url, latest_version, license, organization, organization_id, project_id, project_type, server_side, slug, title, versions`.
  - `versions` = MC game versions; `latest_version` = version ID; `license` = SPDX string; `environment` = **array**; `author` = username (resolved to org owner for org projects); `organization` = org **name**, `organization_id` = id.

Real hit (`query=better mc`, `facets=[["project_type:modpack"],["author:SHXRKIE"]]`, `index=downloads`; total_hits 5):
```json
{"project_id":"shFhR8Vx","project_type":"modpack","all_project_types":["modpack"],"slug":"better-mc-fabric-bmc2",
 "author":"SHXRKIE","author_id":"ITFr4xkL","organization":"Luna Pixel Studios","organization_id":"hPbEqJ6D",
 "title":"Better MC [FABRIC] - BMC2","categories":["adventure","combat","fabric","kitchen-sink","lightweight","multiplayer","optimization"],
 "display_categories":["adventure","combat","optimization"],"versions":["1.20.1"],"downloads":3784245,"follows":1519,
 "icon_url":"https://cdn.modrinth.com/data/shFhR8Vx/a19c2bcb51d38f32f138d3607e91cb2b7b8e387f_96.webp",
 "date_created":"2023-11-21T03:01:20.536866+00:00","date_modified":"2026-07-15T06:31:42.748667+00:00",
 "latest_version":"M5BnAIQy","license":"LicenseRef-All-Rights-Reserved","client_side":"required","server_side":"required",
 "environment":["client_and_server"],"disclosure_types":[],"color":3222862}
```

## 4. Project object (GET /project/{id|slug})
Fields (real): `id, slug, title, description, body, body_url, project_type, status, requested_status, moderator_message, categories, additional_categories, loaders, game_versions, versions, client_side, server_side, environment, downloads, followers, icon_url, raw_icon_url, color, gallery, license, issues_url, source_url, wiki_url, discord_url, donation_urls, team, organization, thread_id, published, updated, approved, queued, monetization_status`.
- Differs from search hit: `id` (not `project_id`), `followers` (not `follows`), `published`/`updated` (not `date_created`/`date_modified`), `versions` = **version IDs** (MC versions are in `game_versions`), `loaders` separate (not mixed into `categories`), `license` = object `{"id","name","url"}`, **no `author`** (use `team` / `organization` id, see 6).
- `client_side`/`server_side`: `required|optional|unsupported|unknown` (deprecated in favour of `environment`, still returned). `environment` = array.
- `project_type` enum in Project/hit schema: `mod, modpack, resourcepack, shader` (`/tag/project_type` also lists `plugin, datapack, minecraft_java_server`).
```json
{"id":"shFhR8Vx","slug":"better-mc-fabric-bmc2","project_type":"modpack","team":"5wZXowH4","organization":"hPbEqJ6D",
 "client_side":"required","server_side":"required","environment":["client_and_server"],"game_versions":["1.20.1"],
 "loaders":["fabric"],"categories":["adventure","combat","optimization"],"additional_categories":["kitchen-sink","lightweight","multiplayer"],
 "license":{"id":"LicenseRef-All-Rights-Reserved","name":"","url":null},"status":"approved","downloads":3786411,"followers":1520,
 "published":"2022-05-17T15:51:54.121490Z","updated":"2026-07-15T06:31:46.447453Z","versions":["MwRs7ZdQ","8xwR1JNo","..."]}
```

## 5. Version object (GET /version/{id}, /project/{id}/version)
Fields (real): `id, project_id, author_id, name, version_number, version_type (release|beta|alpha), status (listed|archived|draft|unlisted|scheduled|unknown), requested_status, featured, loaders[], game_versions[], environment (**string**, not array), date_published (ISO-8601), downloads, changelog (omitted with include_changelog=false), changelog_url (always null, deprecated), dependencies[], files[]`.
- `files[]`: `id` (present though not in spec), `hashes{sha1,sha512}`, `url` (cdn.modrinth.com, URL-encoded), `filename`, `primary` (max one true; if none, first file is primary), `size` (bytes), `file_type` (null or `required-resource-pack|optional-resource-pack|sources-jar|dev-jar|javadoc-jar|signature|unknown`).
- `dependencies[]`: `version_id?`, `project_id?`, `file_name?`, `dependency_type` (`required|optional|incompatible|embedded`). Modpack versions list their contents as `embedded`; non-Modrinth files appear with only `file_name` (BMC2 v40: 336 with ids + 26 file_name-only, all `embedded`).
- Filters: `GET /project/fabric-api/version?loaders=["fabric"]&game_versions=["1.20.1"]&include_changelog=false` -> 28 versions, newest `0.92.12+1.20.1` (`rvI2dfzR`). Quirk: adding `featured=true` returned 1 version whose `featured` was `false`; semantics UNVERIFIED - don't rely on it.
```json
{"id":"M5BnAIQy","project_id":"shFhR8Vx","author_id":"dSn53j6E","name":"Better MC [FABRIC] - BMC2 v40","version_number":"v40",
 "version_type":"release","status":"listed","featured":false,"loaders":["fabric"],"game_versions":["1.20.1"],
 "environment":"client_and_server","date_published":"2026-07-15T06:31:46.447453Z","downloads":1040303,"changelog_url":null,
 "files":[{"id":"3ZRIO3aw","hashes":{"sha1":"326da0b267496f5d952118b7f8aa2bddcb7fc269","sha512":"a9ebbf51...72d9"},
   "url":"https://cdn.modrinth.com/data/shFhR8Vx/versions/M5BnAIQy/BMC2%20%5BFABRIC%5D%201.20.1%20v40.mrpack",
   "filename":"BMC2 [FABRIC] 1.20.1 v40.mrpack","primary":true,"size":66273847,"file_type":null}],
 "dependencies":[{"version_id":"yGrKC4Ka","project_id":"UiFDlI7w","file_name":null,"dependency_type":"embedded"},
   {"version_id":null,"project_id":null,"file_name":"fastasyncworldsave-fabric-1.20.1-2.6.jar","dependency_type":"embedded"}]}
```

### Hash lookups (verified)
```bash
curl -X POST https://api.modrinth.com/v2/version_files -H 'Content-Type: application/json' -A "$UA" \
  -d '{"hashes":["5e120c3e3f8195f9295c9d8e544e8a0a7247dd9f","326da0b267496f5d952118b7f8aa2bddcb7fc269","000...0"],"algorithm":"sha1"}'
# -> {"326da0b2...":{"id":"M5BnAIQy","project_id":"shFhR8Vx",...},"5e120c3e...":{"id":"liAgrvFi","project_id":"OMiCHIwt","version_number":"1.0.11+1.20.1",...}}  (zero hash omitted)
curl -X POST https://api.modrinth.com/v2/version_files/update -H 'Content-Type: application/json' -A "$UA" \
  -d '{"hashes":["5e120c3e3f8195f9295c9d8e544e8a0a7247dd9f"],"algorithm":"sha1","loaders":["fabric"],"game_versions":["1.20.1"]}'
# -> {"5e120c3e...":{"id":"liAgrvFi","version_type":"release","loaders":["fabric"],"game_versions":["1.20.1"],"files":[{"filename":"voidz-1.0.11.jar",...}]}}
```
`GET /version_file/326da0b267496f5d952118b7f8aa2bddcb7fc269?algorithm=sha1` -> `M5BnAIQy`. Spec default algorithm is `sha1`; always pass it explicitly.

## 6. Author / ownership
- `project.team` -> `GET /team/{id}/members` returns `TeamMember[]` (`user{username,id,...}`, `role`, ...). For org-owned projects it is `[]` (BMC2 team `5wZXowH4` -> `[]`).
- `project.organization` = org id. v2 has no org route; `GET https://api.modrinth.com/v3/organization/hPbEqJ6D` -> `{"id":"hPbEqJ6D","slug":"luna-pixel-studios","name":"Luna Pixel Studios","team_id":"iVDcGu3n","members":[{"user":{"username":"SHXRKIE"},"role":"Owner","is_owner":true},...]}`.
- Simplest display author: the search hit's `author` (already resolved). `version.author_id` is the uploader (BMC2 v40: `dSn53j6E` != owner `ITFr4xkL`).

## 7. Tags (verified)
| Route | Fields | Notes |
|---|---|---|
| `/tag/category` | `name, project_type, header, icon` (SVG) | 126 entries. Modpack categories: adventure, challenging, combat, kitchen-sink, lightweight, magic, multiplayer, optimization, quests, technology |
| `/tag/loader` | `name, supported_project_types[], icon` | 29: babric, bta-babric, bukkit, bungeecord, canvas, datapack, fabric, folia, forge, geyser, iris, java-agent, legacy-fabric, liteloader, minecraft, modloader, neoforge, nilloader, optifine, ornithe, paper, purpur, quilt, rift, spigot, sponge, vanilla, velocity, waterfall. Modpack-capable: fabric, forge, neoforge, quilt |
| `/tag/game_version` | `version, version_type (release\|snapshot\|alpha\|beta), date, major` | 922 entries, newest first. Latest releases: `26.3, 26.2, 26.1.2, 26.1.1, 26.1, 1.21.11, 1.21.10...` -> **new year-based scheme; sort by this list/date, not semver** |
| `/tag/project_type` | strings | `mod, modpack, resourcepack, shader, plugin, datapack, minecraft_java_server` |
| `/tag/side_type` | strings | `required, optional, unsupported, unknown` |

## 8. Modpack format (.mrpack)
- Zip with `modrinth.index.json` at root + `overrides/` (both sides), `client-overrides/`, `server-overrides/` (dirs recognised by labrinth validator and Modrinth App). CDN content-type `application/x-modrinth-modpack+zip`.
- `modrinth.index.json` (camelCase, from labrinth `PackFormat`):

| Field | Type / rule |
|---|---|
| `formatVersion` | int (all observed packs: `1`) |
| `game` | must be `"minecraft"` |
| `versionId` | string 1-512 (pack's own label; BMC2 v40 index still says `"v1"` -> not reliable as identity) |
| `name` | string 1-512; `summary` optional <= 2048 |
| `files[]` | `path` (safe relative unix path, e.g. `mods/x.jar`), `hashes` (`sha1` AND `sha512` both required by validator), `env` optional `{"client":S,"server":S}` with S = `required\|optional\|unsupported\|unknown`, `downloads[]` (URLs; labrinth `.env.local` whitelist `["cdn.modrinth.com","github.com","raw.githubusercontent.com"]`, prod list UNVERIFIED), `fileSize` (u32) |
| `dependencies` | map, keys only `minecraft`, `forge`, `neoforge`, `fabric-loader`, `quilt-loader` -> version string |

- Modrinth App skips files where `env.client == "unsupported"`; a server installer should skip `env.server == "unsupported"`, then copy `overrides/` then `server-overrides/` (ordering by analogy with client logic; UNVERIFIED for server).
- Real (BMC2 v40): `{"game":"minecraft","formatVersion":1,"versionId":"v1","name":"Better MC [FABRIC] BMC2","summary":"","dependencies":{"minecraft":"1.20.1","fabric-loader":"0.19.3"},"files":[{"path":"mods/voidz-1.0.11.jar","hashes":{"sha1":"5e120c3e...","sha512":"6d4aaa06..."},"env":{"client":"required","server":"required"},"downloads":["https://cdn.modrinth.com/data/OMiCHIwt/versions/liAgrvFi/voidz-1.0.11.jar"],"fileSize":68262}, ...336 files]}`
- Tip: CDN supports HTTP Range (`accept-ranges: bytes`, 206). Read the zip EOCD/central directory then the `modrinth.index.json` entry (~50 KB deflated) instead of downloading 30-90 MB. Suffix ranges (`Range: bytes=-N`) returned **501**; use explicit `start-end`.

## 9. Better MC (Luna Pixel Studios / SHXRKIE) - verified data
`GET /search?query=better mc&facets=[["project_type:modpack"]]` -> 48 hits; official packs = `author:SHXRKIE` (5 BMC packs). No BMC1 on Modrinth (`better-mc-forge-bmc1`, `better-mc-forge`, `better-mc-fabric` -> 404).

| Slug | Project ID | Loader | MC | #ver | Latest versions (id, number, date) | Index deps (latest) |
|---|---|---|---|---|---|---|
| `better-mc-fabric-bmc2` | `shFhR8Vx` | fabric | 1.20.1 | 8 | `M5BnAIQy` v40 2026-07-15; `Ur9uoHH5` v26.5 2025-01-19; `SH0rTaJc` v25 2024-06-01 | minecraft 1.20.1, fabric-loader 0.19.3; 336 files |
| `better-mc-forge-bmc4` | `4BV47HRn` | forge | 1.20.1 | 12 | `bdRIjRgs` v59 2026-07-15; `krFPoaLH` v43 2025-06-09; `bXQ3GijP` v40 2025-05-15 | minecraft 1.20.1, forge 47.4.20; 340 files |
| `better-mc-fabric-bmc3` | `NHUidpfR` | fabric | 1.21.1 (v2 beta `DHAMuGqY` on 1.21) | 5 | `OdTbhRit` v30 2025-05-18; `wed5kBND` v25 2025-03-09; `3Y8ZvIGD` v23 2025-02-25 | minecraft 1.21.1, fabric-loader 0.16.14; 284 files |
| `better-mc-neoforge-bmc5` | `B37WQ89b` | neoforge | 1.21.1 | 10 | `Mo8ro6Ra` v31 2025-06-10; `AZeU2erH` v30 2025-05-31; `PMzLZVE0` v29 2025-05-18 | minecraft 1.21.1, neoforge 21.1.176; 261 files |
| `better-mc-skyblock` | `v62lHvut` | forge | 1.20.1 | 1 | `YuIsRsz3` v9 2025-03-02 | not fetched |

- All have `client_side=server_side=required`, `environment=["client_and_server"]`, license `LicenseRef-All-Rights-Reserved`, org `hPbEqJ6D`. Every listed version is `release` except BMC3 v2 (`beta`); all `featured:false`.
- Latest-version mrpack sizes: BMC2 66,273,847 B; BMC4 90,351,891; BMC3 43,221,850; BMC5 34,233,620; BMCS 27,449,433.
- **Server pack: none on Modrinth.** Every version (all 36 checked) has exactly 1 file: the `.mrpack` (`primary:true`, `file_type:null`); project bodies don't mention a server pack. Server must be built from the mrpack (e.g. itzg `TYPE=MODRINTH`).
- **`env` is useless for filtering in BMC**: every file in BMC2/3/4/5 indexes is `{"client":"required","server":"required"}`, including client-only mods (BMC2: sodium, iris, modmenu, xaerominimap, entityculling, fancymenu, drippyloadingscreen, continuity, ...). Panel must apply its own exclude list (itzg `MODRINTH_EXCLUDE_FILES` / default exclude-include file).
- Overrides (BMC2 v40): `overrides/mods` (20 jars not on Modrinth), `overrides/config` (526), `overrides/defaultconfigs` (505), `overrides/modernfix` (3,544 cache files), resourcepacks, shaderpacks. BMC4 similar (21 override jars).
- Other non-official "Better MC" modpacks in results: `a-better-mc` (`GsvSe9YD`, client_only), `better-mc-east` (`EHHB0PMK`), `bmcpack` (`pJXOXg2a`), `mcbb` (`o2lqKb5A`). SHXRKIE's other packs: medieval-mc-forge-mmc4, medieval-mc-fabric-mmc2, medieval-fabric-mmc3, medieval-mc-neoforge-mmc5, versatile, fearnightfall, steam-punk, remain, performatic.

## 10. itzg/minecraft-server Modrinth env vars (from itzg docs, master)
- Modpack: `TYPE` / `MODPACK_PLATFORM` / `MOD_PLATFORM` = `MODRINTH`; `MODRINTH_MODPACK` = slug, project ID, project URL, version-page URL, mrpack URL, or container path ending `.mrpack`; `VERSION` (`LATEST`/`SNAPSHOT` or MC version to narrow); `MODRINTH_MODPACK_VERSION_TYPE` (`release|beta|alpha`); `MODRINTH_LOADER` (docs list `forge|fabric|quilt`; neoforge UNVERIFIED there); `MODRINTH_VERSION` (version ID or number); `MODRINTH_IGNORE_MISSING_FILES`; `MODRINTH_EXCLUDE_FILES` / `MODRINTH_FORCE_INCLUDE_FILES` (substring or `/regex/`, comma/newline list); `MODRINTH_FORCE_SYNCHRONIZE`; `MODRINTH_DEFAULT_EXCLUDE_INCLUDES` (empty disables defaults); `MODRINTH_OVERRIDES_EXCLUSIONS` (ant-style paths).
- Mods: `MODRINTH_PROJECTS` (`[prefix:]project[?][:version|release-type]`, `@/listing/file`; prefixes `datapack|fabric|forge|paper`), `MODRINTH_DOWNLOAD_DEPENDENCIES` (`none|required|optional`), `MODRINTH_PROJECTS_DEFAULT_VERSION_TYPE`, `VERSION_FROM_MODRINTH_PROJECTS`.
- Suggested (not executed) pin for BMC4: `TYPE=MODRINTH MODRINTH_MODPACK=better-mc-forge-bmc4 MODRINTH_VERSION=bdRIjRgs VERSION=1.20.1`.

## 11. UNVERIFIED / caveats
- 429 response body and exact Reset semantics (never hit the limit; Remaining stayed 300 through proxy).
- `featured` filter semantics (see 5); `multiple=true` on `/version_file/{hash}` not tested.
- Production `WHITELISTED_MODPACK_DOMAINS`; precedence of `server-overrides/` over `overrides/` on servers.
- Whether UA enforcement will block empty/library UAs (docs say it may) - always send `mcserver-panel/<ver> (<contact>)`.
