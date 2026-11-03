# CurseForge Core API reference

Checked 2026-11-03. Sources:
- Official docs: `https://docs.curseforge.com/rest-api/` (one Slate page, about 1.1 MB HTML, fetched with curl).
- 3rd-party API Terms: `https://support.curseforge.com/support/solutions/articles/9000207405-curse-forge-3rd-party-api-terms-and-conditions`.
- CurseForge blog post of 2026-06-10, "Introducing API Key Authentication for CurseForge File Downloads".
- Client sources: itzg mc-image-helper `main` (`src/main/java/me/itzg/helpers/curseforge/*`), Prism Launcher `develop` (`launcher/modplatform/flame/FlameAPI.cpp`, `launcher/modplatform/helpers/HashUtils.cpp`, `libraries/murmur2/src/MurmurHash2.cpp`), packwiz `main` (`curseforge/request.go`, `curseforge/curseforge.go`).

We have **no API key**, so no live `api.curseforge.com` response could be captured beyond the 403 below. Response shapes come from the docs' example schemas ("string"/0 placeholders). Tags used below:
- **client-src**: read from client source code, not from the CF docs.
- **observed**: seen in a real request.
- **UNVERIFIED**: not confirmed.

## 1. Access
| Item | Value |
|---|---|
| Base URL | `https://api.curseforge.com` (all endpoints; docs) |
| Auth | header `x-api-key: <key>` on every call (docs: "API Key (API_KEY) Parameter Name: x-api-key, in: header"). Keys come from the CurseForge for Studios console `https://console.curseforge.com/`. Non-developers must "apply for a key" (3rd-party form linked from the docs). |
| Key format | client-src (mc-image-helper `ApiKeyHelper.EXPECTED_API_KEY_PREFIX`): keys start with `$2a$10$` (bcrypt-style), so they contain `$`. |
| Accept | `Accept: application/json` (docs samples) |
| Ints | "Unless stated otherwise all int32 responses are unsigned." Fingerprints are uint32. Some fields are int64 (`downloadCount`, `fileLength`, `fileFingerprint`). |
| Pagination | "maximum page size is 50 results per page and capped at 10000 total results"; the rule is **`index + pageSize <= 10,000`**. `index` is 0-based. The default and the max `pageSize` are both 50. |
| Envelope | Success responses are `{"data": ...}`. Paged ones add `"pagination": {"index", "pageSize", "resultCount", "totalCount"}` (`totalCount` is int64). |
| Error body | client-src: mc-image-helper parses error bodies as `{ok:boolean, error:string, data}`. A missing file can come back as **400** with `error` starting `"Error: 404"`, or as a plain 404. UNVERIFIED live. |

Real response without a key (observed on `/v1/games/432` and `/v1/mods/search?...`):
```
HTTP/2 403   content-type: text/plain   content-length: 37   server: Kestrel   (via CloudFront)
Forbidden: API Key missing or invalid
```
- client-src (mc-image-helper `errorMapForbidden`): a 403 whose body contains `There might be too much traffic` means **rate-limited**. Any other 403 means the key is invalid or the rate limit was exceeded.
- The docs publish no rate-limit numbers.
- The Terms say that above an Overwolf-defined quota, a written license may be required.

## 2. Minecraft constants
| Thing | Value | Source |
|---|---|---|
| `gameId` Minecraft | **432** | mc-image-helper `MINECRAFT_GAME_ID="432"`, Prism `gameId=432`, packwiz |
| classId Mods | **6** | Prism `g_classIDMappings`, packwiz `defaultFolders` (`mods`) |
| classId Modpacks | **4471** | Prism |
| classId Bukkit Plugins | **5** | packwiz (`plugins`, comment "Bukkit Plugins") |
| classId Resource Packs | 12 | Prism, packwiz |
| classId Worlds | 17 | Prism, packwiz (`saves`) |
| classId Shaders | 6552 | Prism |
| classId Data Packs | 6945 | Prism |

- These classIds are client-src, not checked live.
- The authoritative list is `GET /v1/categories?gameId=432&classesOnly=true`: entries with `isClass=true` are classes, and their `id` is the classId.
- mc-image-helper does not hard-code IDs. It looks up class slugs `modpacks`, `mc-mods`, `bukkit-plugins`, `worlds` from that call.

## 3. Enums (docs, Schemas section)
| Enum | Values |
|---|---|
| `ModLoaderType` | 0=Any, 1=Forge, 2=Cauldron, 3=LiteLoader, 4=Fabric, 5=Quilt, 6=NeoForge |
| `ModsSearchSortField` | 1=Featured, 2=Popularity, 3=LastUpdated, 4=Name, 5=Author, 6=TotalDownloads, 7=Category, 8=GameVersion, 9=EarlyAccess, 10=FeaturedReleased, 11=ReleasedDate, 12=Rating |
| `SortOrder` | `asc`, `desc` (strings) |
| `FileReleaseType` | 1=Release, 2=Beta, 3=Alpha |
| `FileRelationType` | 1=EmbeddedLibrary, 2=OptionalDependency, 3=RequiredDependency, 4=Tool, 5=Incompatible, 6=Include |
| `HashAlgo` | 1=Sha1, 2=Md5 |
| `FileStatus` | 1=Processing, 2=ChangesRequired, 3=UnderReview, 4=Approved, 5=Rejected, 6=MalwareDetected, 7=Deleted, 8=Archived, 9=Testing, 10=Released, 11=ReadyForReview, 12=Deprecated, 13=Baking, 14=AwaitingPublishing, 15=FailedPublishing, 16=Cooking, 17=Cooked, 18=UnderManualReview, 19=ScanningForMalware, 20=ProcessingFile, 21=PendingRelease, 22=ReadyForCooking, 23=PostProcessing |
| `ModStatus` | 1=New, 2=ChangesRequired, 3=UnderSoftReview, 4=Approved, 5=Rejected, 6=ChangesMade, 7=Inactive, 8=Abandoned, 9=Deleted, 10=UnderReview |
| `ModLoaderInstallMethod` | 1=ForgeInstaller, 2=ForgeJarInstall, 3=ForgeInstaller_v2, 4=FabricInstaller, 5=QuiltInstaller, 6=NeoForgeInstaller |
| `CoreStatus` / `CoreApiStatus` (Game) | 1=Draft…6=Live / 1=Private, 2=Public |

All enums are serialized as **integers** in JSON, except `SortOrder`. mc-image-helper's Java enums agree: ModLoaderType is the 0-based ordinal, FileRelationType and HashAlgo are ordinal+1.

## 4. Endpoint list (all from the docs)
| Method + path | Purpose / params |
|---|---|
| `GET /v1/games` | `index`, `pageSize` |
| `GET /v1/games/{gameId}` | game: `id, name, slug, dateModified, assets{iconUrl,tileUrl,coverUrl}, status, apiStatus` |
| `GET /v1/games/{gameId}/versions`, `GET /v2/games/{gameId}/versions` | versions grouped by type. V2 returns `[{type, versions:[GameVersion]}]` |
| `GET /v1/games/{gameId}/version-types` | `[{id, gameId, name, slug, isSyncable, status}]` |
| `GET /v1/categories` | `gameId` (required), `classId`, `classesOnly` (bool) |
| `GET /v1/mods/search` | see §5 |
| `GET /v1/mods/{modId}` | `{"data": Mod}` |
| `POST /v1/mods` | body `{"modIds":[...], "filterPcOnly": true}`. The ids must belong to one game. Returns `{"data":[Mod]}` |
| `POST /v1/mods/featured` | body `{"gameId":432, "excludedModIds":[...], "gameVersionTypeId":0}` |
| `GET /v1/mods/{modId}/description` | HTML in `{"data":"..."}`. Query `raw` (bool), `stripped` (bool) |
| `GET /v1/mods/{modId}/files/{fileId}` | `{"data": File}` (404 if unknown) |
| `GET /v1/mods/{modId}/files` | see §6 |
| `POST /v1/mods/files` | body `{"fileIds":[...]}` returns `{"data":[File]}` |
| `GET /v1/mods/{modId}/files/{fileId}/changelog` | **HTML** string in `{"data":"..."}` |
| `GET /v1/mods/{modId}/files/{fileId}/download-url` | `{"data":"<url>"}`. Behaviour for distribution-disallowed files is UNVERIFIED |
| `POST /v1/fingerprints` and `POST /v1/fingerprints/{gameId}` | exact fingerprint match (§8). 400, or 503 "Service Unavailable" (string) |
| `POST /v1/fingerprints/fuzzy` and `/fuzzy/{gameId}` | body `{"gameId":432, "fingerprints":[{"foldername":"mods", "fingerprints":[...]}]}` |
| `GET /v1/minecraft/version` | `sortDescending` (bool). `[{id, gameVersionId, versionString, jarDownloadUrl, jsonDownloadUrl, approved, dateModified, gameVersionTypeId, gameVersionStatus, gameVersionTypeStatus}]` |
| `GET /v1/minecraft/version/{gameVersionString}` | single version |
| `GET /v1/minecraft/modloader` | `version` (MC version), `includeAll` (bool). Returns `[{name, gameVersion, latest, recommended, dateModified, type}]` |
| `GET /v1/minecraft/modloader/{modLoaderName}` | `{id, name, type, downloadUrl, filename, installMethod, minecraftVersion, mavenVersionString, versionJson, installProfileJson, latest, recommended, approved, ...}` |

## 5. `GET /v1/mods/search`
| Param | Type | Notes (docs wording) |
|---|---|---|
| `gameId` | int, **required** | 432 |
| `classId` | int | "section id (discoverable via Categories)" |
| `categoryId` / `categoryIds` | int / string | `categoryIds=[1,2,3]`, max 10. Overrides `categoryId` |
| `gameVersion` / `gameVersions` | string | `gameVersions=["1.19.1","1.20.1"]`, max 4. Overrides `gameVersion` |
| `searchFilter` | string | "free text search in the mod name and author" |
| `sortField` | int (ModsSearchSortField) | 1–12 |
| `sortOrder` | `asc`\|`desc` | |
| `modLoaderType` | int (ModLoaderType) | **"Must be coupled with gameVersion."** |
| `modLoaderTypes` | string | max 5. Docs show `modLoaderTypes=[Forge, Fabric, ...]`. Prism sends numeric `modLoaderTypes=[6,1,4,5]` (client-src). Overrides `modLoaderType` |
| `gameVersionTypeId`, `authorId`, `primaryAuthorId` | int | |
| `slug` | string | "coupled with classId will result in a unique result". mc-image-helper resolves packs with `?gameId=432&slug={slug}&classId={classId}` |
| `index`, `pageSize` | int | pageSize ≤ 50, index+pageSize ≤ 10000 |

The response is `{"data":[Mod], "pagination":{...}}`. Fields of **Mod** (docs schema):
```json
{"id":0,"gameId":0,"name":"string","slug":"string",
 "links":{"websiteUrl":"string","wikiUrl":"string","issuesUrl":"string","sourceUrl":"string"},
 "summary":"string","status":1,"downloadCount":0,"isFeatured":true,"primaryCategoryId":0,
 "categories":[{"id":0,"gameId":0,"name":"string","slug":"string","url":"string","iconUrl":"string",
   "dateModified":"…","isClass":true,"classId":0,"parentCategoryId":0,"displayIndex":0}],
 "classId":0,"authors":[{"id":0,"name":"string","url":"string"}],
 "logo":{"id":0,"modId":0,"title":"string","description":"string","thumbnailUrl":"string","url":"string"},
 "screenshots":[ModAsset],"mainFileId":0,"latestFiles":[File],
 "latestFilesIndexes":[{"gameVersion":"string","fileId":0,"filename":"string","releaseType":1,
   "gameVersionTypeId":0,"modLoader":0}],
 "latestEarlyAccessFilesIndexes":[FileIndex],
 "dateCreated":"…","dateModified":"…","dateReleased":"…",
 "allowModDistribution":true,"gamePopularityRank":0,"isAvailable":true,"thumbsUpCount":0,"rating":0}
```
- `classId` is `int|null`.
- `allowModDistribution` is `boolean|null` ("Is mod allowed to be distributed").
- `isAvailable=false` can mean "experimental, in a deleted state or has only alpha files".
- `FileIndex.gameVersionTypeId` is nullable.
- `FileIndex.modLoader` is a ModLoaderType int. **UNVERIFIED**: whether it can be null or absent for non-mod content.
- `latestFiles` holds only one or two files (mc-image-helper comment). For a full list call `/files`.
- Logo: use `logo.thumbnailUrl` for the small image and `logo.url` for full size. **UNVERIFIED**: whether `logo` can be null.

## 6. `GET /v1/mods/{modId}/files`
Params: `gameVersion` (string), `modLoaderType` (int), `gameVersionTypeId` (int), `index`, `pageSize` (default and max 50; index+pageSize ≤ 10000). Returns `{"data":[File], "pagination":{...}}`. Prism sends `pageSize=10000` to this endpoint (client-src). Whether the server accepts that or caps it at 50 is **UNVERIFIED**, so paginate.

Fields of **File** (docs schema; `¦null` means nullable):
```json
{"id":0,"gameId":0,"modId":0,"isAvailable":true,"displayName":"string","fileName":"string",
 "releaseType":1,"fileStatus":1,"hashes":[{"value":"string","algo":1}],
 "fileDate":"2019-08-24T14:15:22Z","fileLength":0,"downloadCount":0,"fileSizeOnDisk":0,
 "downloadUrl":"string","gameVersions":["string"],
 "sortableGameVersions":[{"gameVersionName":"string","gameVersionPadded":"string","gameVersion":"string",
   "gameVersionReleaseDate":"…","gameVersionTypeId":0}],
 "dependencies":[{"modId":0,"relationType":1}],
 "exposeAsAlternative":true,"parentProjectFileId":0,"alternateFileId":0,
 "isServerPack":true,"serverPackFileId":0,"isEarlyAccessContent":true,"earlyAccessEndDate":"…",
 "fileFingerprint":0,"modules":[{"name":"string","fingerprint":0}]}
```
- Nullable: `fileSizeOnDisk`, `exposeAsAlternative`, `parentProjectFileId`, `alternateFileId`, `isServerPack`, `serverPackFileId`, `isEarlyAccessContent`, `earlyAccessEndDate`.
- `hashes[].algo`: 1=sha1, 2=md5. mc-image-helper verifies downloads against these.
- `fileFingerprint` is the murmur2 value (§8). packwiz falls back to it when sha1 and md5 are missing.
- `gameVersions` is a flat list that mixes MC versions with loader and environment tags. A real example (**UNVERIFIED**): `["1.20.1","Forge","Server"]`. Filter on the `sortableGameVersions` names, or use the `gameVersion`/`modLoaderType` query params instead of parsing.
- `dependencies` are mod-level only (modId, relationType). The right file has to be resolved separately.
- **Server packs**: a client pack file can point at its server zip through `serverPackFileId`. The server zip has `isServerPack=true` and `parentProjectFileId` set to the client file. itzg: "Don't pick a server pack file (it has no manifest)". mc-image-helper `resolveModpackFile` returns the "first matching non-server-pack file".
- `downloadUrl` **can be null**. mc-image-helper treats null as "The modpack authors have indicated this file is not allowed for project distribution". Official docs list it as plain `string`; the null behaviour is client-src. It goes with `allowModDistribution=false`. The CurseForge site gives no visible indication of that setting.

## 7. Downloads, CDN and distribution
- CDN URL pattern (client-src, from mc-image-helper `buildFallbackUrl`): `https://edge.forgecdn.net/files/{id[0:4]}/{id[4:]}/{fileName}` when the id has 7 or more digits. For example, 8273779 becomes `/files/8273/779/<fileName>`. Older ids use `/files/{id/1000}/{id}/...` (mc-image-helper's legacy branch). URL-encode space, `[` and `]` in the filename (mc-image-helper `normalizeDownloadUrl`).
- mc-image-helper (recent `main`): when `downloadUrl` is null it **tries the CDN fallback URL anyway**. Only on 404 does it mark the file as "needs manual download". Our panel should not copy this for disallowed mods: the author opted out.
- **CDN key auth** (blog, 2026-06-10):
  - `edge.forgecdn.net` accepts `x-api-key: <key>`, or `?api-key=<key>` (the header is preferred).
  - It was announced as "required from July 16th", after which a missing key returns `401 {"error":"Unauthorized","message":"A valid api-key is required."}`.
  - **Observed 2026-11-03 without a key**:
    - `GET`/`HEAD https://edge.forgecdn.net/files/8273/779/cc-tweaked-1.21.1-forge-1.120.0.jar` returned **302** to `https://mediafilez.forgecdn.net/...`, which returned **200** (`application/java-archive`, 3126946 bytes). Enforcement was not in effect from this network.
    - A `Range:` GET to edge returned **404** (empty body), so don't send Range to edge.
  - Send the key anyway and handle 401.
- **Terms** (3rd-party API T&C):
  - The API Key is "non-transferable and may not be shared with any third party".
  - Developer shall not "conceal your identity or geographic location when accessing the API, including accessing the API through a **proxy server or VPN**".
  - Developer shall not "**save or cache any data obtained through the API**", and shall not use the API to build a product "that competes, directly or indirectly, with CF".
  - packwiz comment: "According to the CurseForge API T&Cs, [downloadUrl] must not be saved or cached".
  - Practical rules for our panel:
    - Each panel operator uses their own key.
    - Don't redistribute or re-host files. Don't serve CF files from our backend to third parties.
    - Don't persist `downloadUrl`. Keep any response caching short-lived and per-installation (strictly, the terms ban caching).
    - When `allowModDistribution` is false or `downloadUrl` is null, show the CurseForge page link `links.websiteUrl` + `/download/{fileId}` (the format mc-image-helper prints) for a manual browser download into `/downloads`.
  - The Studios ToS on the docs page targets game developers and does not cover 3rd-party API clients.

## 8. Fingerprints (`POST /v1/fingerprints[/{gameId}]`)
Request `{"fingerprints":[3376380438, 1621425345]}` (uint32 list). Response `data` is a FingerprintsMatchesResult:
```json
{"isCacheBuilt":true,
 "exactMatches":[{"id":0,"file":{File},"latestFiles":[File]}],
 "exactFingerprints":[0],
 "partialMatches":[FingerprintMatch],"partialMatchFingerprints":{"property1":[0]},
 "installedFingerprints":[0],"unmatchedFingerprints":[0]}
```
- In `exactMatches`, `id` is the **modId** and `file` is the matched file. `unmatchedFingerprints` lists the jars CF doesn't know (custom or local builds).
- The algorithm is not described in the CF docs. client-src (Prism `HashUtils.cpp` + `MurmurHash2.cpp`): **MurmurHash2, 32-bit, seed 1**, computed over the file bytes with every byte 9, 10, 13 or 32 (`\t \n \r space`) **removed**. The length used to seed the hash is the filtered length. The result is unsigned.
- Python transcription, sanity-run locally (`b"abc"` and `b"a b\tc"` both give 1621425345). It was **not** cross-checked against a live CF fingerprint (no key):
```python
def cf_fingerprint(data: bytes) -> int:
    b = data.translate(None, b"\t\n\r ")
    m, n, M = 0x5BD1E995, len(b), 0xFFFFFFFF
    h = (1 ^ n) & M
    for i in range(0, n - n % 4, 4):
        k = (int.from_bytes(b[i:i+4], "little") * m) & M
        k ^= k >> 24
        h = ((h * m) & M) ^ ((k * m) & M)
    t = n % 4
    if t:
        r = b[n - t:]
        if t == 3: h ^= r[2] << 16
        if t >= 2: h ^= r[1] << 8
        h = ((h ^ r[0]) * m) & M
    h ^= h >> 13; h = (h * m) & M; h ^= h >> 15
    return h
```
Pure Python is slow on large jars (about 4 bytes per loop iteration), so run it in the agent off the event loop. Typical use: hash every `mods/*.jar`, POST the hashes in one batch, and map the results to modId/fileId for update checks.

## 9. itzg `AUTO_CURSEFORGE` and the key
See `docs/reference/itzg-runtime.md` §5 for all `CF_*` variables. Key-specific facts:
- Env var `CF_API_KEY`, or `CF_API_KEY_FILE` (a path to a UTF-8 file, trimmed). Precedence in mc-image-helper `ApiKeyHelper.loadApiKey`:
  1. `CF_API_KEY` if it is not blank
  2. `CF_API_KEY_FILE`
  3. a key bundled into the jar (`/cf-api.properties`, `cfApiKey`)
  4. otherwise it fails with "CurseForge API key is required"
- itzg docs: the bundled key exists on Java 17+ images only. Pre-17 tags (`java8` etc.) need `CF_API_KEY`.
- The key is sent as `x-api-key` with `apiKey.trim()`.
- `CF_API_BASE_URL` overrides `https://api.curseforge.com`. It is an mc-image-helper option `--api-base-url`; the itzg docs don't mention it.
- **`$` escaping**: compose interpolates `$`, so write `CF_API_KEY: '$$2a$$10$$...'` in compose YAML. In a `.env` file, single quotes need no doubling. With `docker run`, use single quotes `-e CF_API_KEY='$2a$10$...'`.
  - mc-image-helper logs an error if the key starts with `$$` ("extra dollar sign escaping"). It also logs one if the key does not start with `$2a$10$` ("Make sure to escape dollar signs with two each").
  - Our agent passes env through the Docker API (docker-py `environment={...}`), which does no interpolation, so pass the raw key. It is better to mount it as a file and set `CF_API_KEY_FILE` so it isn't visible in `docker inspect`.
- Forbidden from CF makes mc-image-helper raise "Access to https://api.curseforge.com is forbidden or rate-limit has been exceeded. Ensure CF_API_KEY is set to a valid API key from https://console.curseforge.com/ or allow rate-limit to reset."
- API calls mc-image-helper makes (client-src):
  - `GET /v1/categories?gameId=432&classesOnly=true`
  - `GET /v1/mods/search?gameId=432&slug=..&classId=..`
  - `GET /v1/mods/{id}`
  - `GET /v1/mods/{id}/files`
  - `GET /v1/mods/{id}/files/{fileId}`

  Responses are cached on disk (`CF_API_CACHE_DEFAULT_TTL`, default `P2D`).
