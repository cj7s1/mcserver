# Minecraft server version metadata APIs (verified 2026-11-03)

All endpoints below were called with curl on 2026-11-03. Response shapes and sample values come from real responses. Anything not checked is marked **UNVERIFIED**.
State that day: Mojang `latest = {"release":"26.3","snapshot":"26.4-snapshot-7"}`.

## 0. Cross-cutting rules
| API | Auth / UA | Cache-Control seen | Rate-limit headers |
|---|---|---|---|
| Mojang piston-meta | none | `public, max-age=120` | none seen |
| Fabric meta | none | `public, max-age=1800` | none seen |
| Quilt meta | none | `no-store` | none seen |
| Forge files/maven | none | (none on promotions) | none seen |
| NeoForge maven API | none; **HEAD returns 404, so use GET** | `no-cache, no-store` | none seen |
| PaperMC Fill v3 | **User-Agent required by the docs** (see §6) | `max-age=1800, public, s-maxage=1800` | none seen |
| Purpur | none | `max-age=7200` | none seen |

- No API sent `RateLimit-*`/`Retry-After` headers. Cache responses server-side for at least the max-age, and don't poll.
- Send one UA everywhere, e.g. `mcserver-panel/<ver> (<contact url or email>)`.
- Lists are **not always sorted** (Forge maven, Quilt loader/{game}, NeoForge alpha strings). Sort client-side and don't rely on array order unless noted.

## 1. Mojang (vanilla), the source of truth for Java requirements
`GET https://piston-meta.mojang.com/mc/game/version_manifest_v2.json` (~279 KB, 922 versions)
```json
{"latest":{"release":"26.3","snapshot":"26.4-snapshot-7"},
 "versions":[{"id":"26.4-snapshot-7","type":"snapshot",
   "url":"https://piston-meta.mojang.com/v1/packages/6be84ec6.../26.4-snapshot-7.json",
   "time":"2026-11-03T13:18:26+00:00","releaseTime":"2026-11-03T13:08:56+00:00",
   "sha1":"6be84ec668c4c843c87f80f2a8efa3095f2989d2","complianceLevel":1}, ...]}
```
- `type` ∈ `release | snapshot | old_beta | old_alpha`. Versions are newest-first by releaseTime; the oldest is `rd-132211`.
- Per-version JSON (`versions[].url`) top-level keys: `arguments, assetIndex, assets, complianceLevel, downloads, id, javaVersion, libraries, logging, mainClass, minimumLauncherVersion, releaseTime, time, type`.
- `downloads.server = {"sha1","size","url"}`, e.g. 26.3 is `https://piston-data.mojang.com/v1/objects/33680f5f.../server.jar` with size 62294556. Old versions use the `launcher.mojang.com/v1/objects/...` host (1.8.9, 1.7.10). **`1.0` and `b1.7.3` have no `downloads.server`.** `1.2.5` has `server` + `windows_server`.
- `javaVersion = {"component": "...", "majorVersion": N}`. It was present on every version sampled, including 1.0 and b1.7.3.

Sampled `javaVersion` values:
| id | majorVersion | component |
|---|---|---|
| 1.7.10, 1.8.9, 1.12.2, 1.16.5 (and 1.0, 1.2.5, b1.7.3) | 8 | jre-legacy |
| 1.17, 1.17.1 | 16 | java-runtime-alpha |
| 1.18, 1.18.2 | 17 | java-runtime-beta |
| 1.20.1, 1.20.4, snapshot 24w13a | 17 | java-runtime-gamma |
| snapshot 24w14a, 1.20.5, 1.20.6, 1.21, 1.21.1, 1.21.11 | 21 | java-runtime-delta |
| 26.1-snapshot-1, 26.1, 26.1.2, 26.2, 26.3 (latest), 26.4-snapshot-7 | 25 | java-runtime-epsilon |

### 2026 version naming (seen in the manifest)
- Releases: `26.1`, `26.1.1`, `26.1.2`, `26.2`, `26.3`. The format is `YY.drop[.hotfix]`. **1.21.11 is the last `1.x` release.**
- Pre-releases: `26.1-snapshot-1`…`26.1-snapshot-11`, `26.x-pre-N`, `26.x-rc-N` (hyphenated `rc-N`; older ones are `1.21.11-rc3`), hotfix RCs like `26.1.2-rc-1`, and April Fools `26w14a`.
- All 26.x versions require **Java 25**. Don't parse ids as `1.MAJOR.MINOR`: compare versions by manifest `releaseTime`/order or with a tolerant tuple parser.

## 2. Java requirement table → itzg image tag
| MC version range (release) | Required Java (Mojang) | itzg tag |
|---|---|---|
| ≤ 1.16.5 (all alpha/beta/1.x up to 1.16.5) | 8 | `java8` (**required for Forge ≤1.16.5**) |
| 1.17 – 1.17.1 | 16 | `java16` (or `java17`; **UNVERIFIED** for every loader) |
| 1.18 – 1.20.4 | 17 | `java17` |
| 1.20.5 – 1.21.11 | 21 | `java21` |
| 26.1 and later (26.x) | 25 | `java25` (also what `latest`/`stable` carry today) |

- Snapshot boundaries: 24w13a=17 and 24w14a=21; 26.1-snapshot-1=25. Use the per-version `javaVersion.majorVersion` rather than hard-coded ranges, then map `{8:java8, 16:java16, 17:java17, 21:java21, 25:java25}`. `java11` exists but no Mojang version requires 11.
- Paper's Fill API gives the same thresholds (`java.version.minimum`: 1.16.5→8, 1.17.1→16, 1.20.4→17, 1.20.5…1.21.11→21, 26.x→25, 1.7.10→6).
- itzg docs (`docs/versions/java.md`) say: "For Forge versions less than 1.18, you _must_ use the `java8` (or other java8) image tag." But 1.17.x vanilla requires 16, so Forge 1.17.1 can't run on 8: use java8 for Forge ≤1.16.5 and java16/17 for 1.17.1 (the 1.17 case is derived, **UNVERIFIED**). The error `ClassLoaders$AppClassLoader cannot be cast to class java.net.URLClassLoader` means the server needs Java 8. `class file version 65.0` means it needs Java 21.
- itzg tags `java8/java11/java16` pin an old mc-image-helper with no bundled CurseForge key. GraalVM tags (Oracle Linux) can't run the Forge installer. The tag list is in `https://raw.githubusercontent.com/itzg/docker-minecraft-server/refs/heads/master/images.json` (`[{tag, java, distribution, jvm, architectures, lts?, deprecated?, jdk?, notes?}]`). Docker Hub confirms the plain tags `java8, java11, java16, java17, java21, java25`.

## 3. Fabric meta (`https://meta.fabricmc.net`)
| Endpoint | Response |
|---|---|
| `GET /v2/versions/game` | `[{"version":"26.4-snapshot-7","stable":false}, …, {"version":"26.3","stable":true}]`: 534 entries, newest first. Only releases are `stable:true`; the oldest stable is `1.14` |
| `GET /v2/versions/loader` | `[{"separator":".","build":1,"maven":"net.fabricmc:fabric-loader:0.20.1","version":"0.20.1","stable":true}, …]`: 255 entries, newest first |
| `GET /v2/versions/loader/{game}` | array, newest loader first, each `{loader{…}, intermediary{maven,version,stable}, launcherMeta{version:2, min_java_version:8, libraries{client,common,server,development}, mainClass{client,server}}}`. Supports `?limit=N` (verified with `limit=1`) |
| `GET /v2/versions/loader/{game}/{loader}` | the same object, unwrapped |
| `GET /v2/versions/installer` | `[{"url":"https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.1.2/fabric-installer-1.1.2.jar","maven":"net.fabricmc:fabric-installer:1.1.2","version":"1.1.2","stable":true}, …]` |
| `GET /v2/versions/loader/{game}/{loader}/{installer}/server/jar` | 200 `application/java-archive`, `content-disposition: attachment; filename="fabric-server-mc.1.21.1-loader.0.20.1-launcher.1.1.2.jar"` (verified, but missing from the README) |

- Unknown game version: **HTTP 400, body `[]`**.
- For 26.x, `intermediary` is `{"maven":"net.fabricmc:intermediary:0.0.0","version":"0.0.0"}` because 26.x is unobfuscated. Don't treat it as an error.
- `launcherMeta.min_java_version` (8) is the loader's own minimum. **Don't use it as the MC Java requirement.**
- Other endpoints in the README: `/v2/versions`, `/v2/versions/game/{yarn,intermediary}`, `/v2/versions/{intermediary,yarn}[/:game_version]`, `/v2/versions/loader/:game/:loader/{profile/json,profile/zip,server/json}`, and legacy `/v1/...`.

## 4. Quilt meta (`https://meta.quiltmc.org`)
| Endpoint | Response |
|---|---|
| `GET /v3/versions/game` | `[{"version":"26.4-snapshot-6","stable":false}, …]`: 447 entries, newest first. It lagged Fabric by one snapshot |
| `GET /v3/versions/loader` | `[{"maven":"org.quiltmc:quilt-loader:0.30.2","version":"0.30.2","build":2,"separator":".","file_size":3159782,"hashes":{"sha1","sha256","sha512"}}, …]`: 309 entries. **No `stable` field**; betas are `-beta.N`. Order isn't strict semver (`0.30.2` comes before `0.31.0-beta.5`) |
| `GET /v3/versions/loader/{game}` | array of `{loader, hashed{maven,version,file_size,hashes}, intermediary{maven,version}, launcherMeta{version,libraries,mainClass{client,server,serverLauncher}}}`. **Unsorted** (the first item for 1.21.1 was `0.20.0-beta.9`), so sort client-side. 26.x entries have only `{loader, launcherMeta}` |
| `GET /v3/versions/installer` | `[{"maven":"org.quiltmc:quilt-installer:0.15.1","version":"0.15.1","url":"https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/0.15.1/quilt-installer-0.15.1.jar","file_size":…,"hashes":{…}}]` |
| `GET /v3/versions/loader/{game}/{loader}/server/json` | 200 JSON |
| `GET /v3/versions/loader/{game}/{loader}/server/jar` | **404**: there's no ready-made server jar (use the installer, or itzg `TYPE=QUILT`) |

- Unknown game version: 404 `{"code":"not_found","message":"File with such name does not exist.","status":404}`.
- Quilt's upstream README is a copy of the fabric-meta one, so its documented paths aren't authoritative. Only the paths above were verified.

## 5. Forge (MinecraftForge)
`GET https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json` (4 KB). **Use this for "latest/recommended".**
```json
{"homepage":"https://files.minecraftforge.net/net/minecraftforge/forge/",
 "promos":{"1.12.2-latest":"14.23.5.2864","1.12.2-recommended":"14.23.5.2859",
  "1.16.5-latest":"36.2.42","1.16.5-recommended":"36.2.34","1.20.1-latest":"47.4.28",
  "1.21.1-latest":"52.1.21","1.21.1-recommended":"52.1.20",
  "26.3-latest":"66.1.5","26.3-recommended":"66.1.0", ...}}
```
- Keys are `{mc}-latest` / `{mc}-recommended` (118 keys). Values are the Forge version **without** the MC prefix. Some MC versions only have `-latest` (e.g. 1.18, 1.20, 1.21, 26.1, 26.1.1).
- Forge major → MC version (from the promos): 14.23→1.12.2, 36→1.16.5, 37→1.17.1, 40→1.18.2, 47→1.20.1, 49→1.20.4, 50→1.20.6, 51→1.21, 52→1.21.1, 61→1.21.11, 62→26.1, 63→26.1.1, 64→26.1.2, 65→26.2, 66→26.3. Forge publishes for 26.x.

`GET https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml` (214 KB, 5104 `<version>`)
- The format is `{mc}-{forge}` (4726), e.g. `1.20.1-47.4.28` or `26.1-62.0.0`. 378 entries have a 3rd part: `1.7.10-10.13.4.1614-1.7.10`, `1.8.9-11.15.1.2318-1.8.9`, `1.7.10_pre4-10.12.2.1149-prerelease` (MC pre uses `_`). **Use the full string as the maven version.**
- `<latest>`/`<release>` = `1.20.6-50.2.15` is stale, so **ignore it**. `<versions>` isn't sorted (it starts with `1.21-51.0.33`). `<lastUpdated>20261103021358`.
- Installer: `https://maven.minecraftforge.net/net/minecraftforge/forge/{v}/forge-{v}-installer.jar` (verified 200 for `1.20.1-47.4.28`, 8.9 MB).

## 6. NeoForge (`https://maven.neoforged.net`, Reposilite API, GET only)
`GET /api/maven/versions/releases/net/neoforged/neoforge` → `{"isSnapshot":false,"versions":["0.25w14craftmine.3-beta", "20.2.3-beta", …, "26.3.0.101"]}` (1856 entries, roughly ascending)
`GET /api/maven/latest/version/releases/net/neoforged/neoforge` → `{"isSnapshot":false,"version":"26.3.0.101"}`
- `?filter=` is a **raw string prefix**: `filter=21.1` returned `21.11.48`, and `filter=21.1.` returned `21.1.265`. **Always add a trailing dot.** `filter=26.1.2` returned `26.1.2.126`. No match gives 404 `{"status":404,"message":"Given artifact does not have any declared version"}`.
- Version → MC mapping:
  - **1.20.2–1.21.11 scheme `A.B.N[-beta]`** → MC `1.A.B` (`B=0` → `1.A`): 20.2→1.20.2, 20.4→1.20.4, 20.6→1.20.6, 21.0→1.21, 21.1→1.21.1, … 21.11→1.21.11. 21.2.x is beta only (`21.2.0-beta`, `21.2.1-beta`).
  - **26.x scheme `Y.D.H.BUILD[-beta|-alpha.N+snapshot-K]`** → MC `Y.D` when H=0, else `Y.D.H`: `26.1.0.19-beta`→26.1, `26.1.1.15-beta`→26.1.1, `26.1.2.126`→26.1.2, `26.2.0.93`→26.2, `26.3.0.101`→26.3. Snapshot builds look like `26.1.0.0-alpha.14+snapshot-11` or `26.1.0.0-alpha.15+pre-3`.
  - `-beta` suffix = not stable. `0.25w14craftmine.N-beta` = April Fools snapshot, so skip it.
- **Legacy 1.20.1** artifact `net/neoforged/forge`: `GET /api/maven/versions/releases/net/neoforged/forge` → 63 versions `1.20.1-47.1.5` … `1.20.1-47.1.106` (plus a stray `47.1.82`). `latest/version/...forge?filter=1.20.1-` → `1.20.1-47.1.106`.
- Installers (verified 200):
  - `https://maven.neoforged.net/releases/net/neoforged/neoforge/{v}/neoforge-{v}-installer.jar` (e.g. 21.1.265)
  - `https://maven.neoforged.net/releases/net/neoforged/forge/1.20.1-47.1.106/forge-1.20.1-47.1.106-installer.jar`

## 7. PaperMC Fill API v3 (`https://fill.papermc.io`)
- **v2 is gone**: `GET https://api.papermc.io/v2/projects/paper` → **410** `{"ok":false,"error":"sunset","message":"This API version has been sunset and is no longer available. ..."}`.
- UA rule (docs.papermc.io/misc/downloads-service): "All requests must now include a valid User-Agent header that clearly identifies your software or company, is not generic (e.g. curl, wget…), includes a contact URL or email." The doc's example is `mc-image-helper/1.39.11 (https://github.com/itzg/docker-minecraft-server)`. In my test the default curl UA wasn't rejected (the response was a Cloudflare HIT), but send a proper UA anyway.
- Swagger: `https://fill.papermc.io/swagger-ui/index.html`. GraphQL: `https://fill.papermc.io/graphql`.

| Endpoint | Response |
|---|---|
| `GET /v3/projects` | `{"projects":[{"project":{"id","name"},"versions":{…}}]}`. Ids: `folia, paper, travertine, velocity, waterfall` |
| `GET /v3/projects/paper` | `{"project":{"id":"paper","name":"Paper"},"versions":{"26.3":["26.3","26.3-rc-3"],"26.2":[…],"26.1":["26.1.2","26.1.1"],"1.21":["1.21.11",…,"1.21"],…,"1.7":["1.7.10"]}}`. Groups and their items are newest first. The latest version is `versions | to_entries[0].value[0]` |
| `GET /v3/projects/paper/versions` | `{"versions":[{"version":{"id":"26.3","support":{"status":"SUPPORTED"},"java":{"version":{"minimum":25},"flags":{"recommended":["-XX:+AlwaysPreTouch",…]}}},"builds":[3,4,…]}]}`: 68 entries. `support.status` ∈ `SUPPORTED, DEPRECATED, UNSUPPORTED`, with an optional `support.end` (e.g. 1.21.11 `"end":"2026-06-15"`) |
| `GET /v3/projects/paper/versions/{v}` | `{"version":{…as above},"builds":[238,237,…]}` (descending here) |
| `GET /v3/projects/paper/versions/{v}/builds[?channel=STABLE]` | array, newest first (see below). Channels seen: `STABLE, BETA, ALPHA` (26.3 had 64/38/48). The filter was verified |
| `GET /v3/projects/paper/versions/{v}/builds/latest` | a single build object. Whether it considers the channel is **UNVERIFIED**, so filter `/builds?channel=STABLE` and take `[0]` |

Build object (real, 26.3):
```json
{"id":238,"time":"2026-11-02T23:56:14Z","channel":"STABLE",
 "commits":[{"sha":"df120fd8…","time":"2026-11-02T23:55:58Z","message":"Add OperatorFeaturesAllowed API for Signs (#14451)"}],
 "downloads":{"server:default":{"name":"paper-26.3-238.jar",
   "checksums":{"sha256":"989f93c2…6b5be6"},"size":55268977,
   "url":"https://fill-data.papermc.io/v1/objects/989f93c2…/paper-26.3-238.jar"}}}
```
- Error: 404 `{"ok":false,"error":"version_not_found","message":"No version was found with the given identifier."}`. Fill errors carry `ok:false`.
- Paper versions include pre/rc ids (`1.21.11-rc3`, `26.3-rc-3`), and every pre-release I sampled is `UNSUPPORTED`. Missing from Paper: 1.21.2, 1.20.3, 1.16, 1.11–1.11.1, 1.8–1.8.7, among others, so validate against the list.

## 8. Purpur (`https://api.purpurmc.org/v2/purpur`)
| Endpoint | Response |
|---|---|
| `GET /v2/purpur` | `{"project":"purpur","metadata":{"current":"26.3"},"versions":["1.14.1",…,"1.21.11","26.1.2","26.2","26.3"]}`: 41 versions, **ascending** (no 26.1/26.1.1) |
| `GET /v2/purpur/{v}` | `{"project":"purpur","version":"26.3","builds":{"latest":"2655","all":["2634",…,"2655"]}}`. Build ids are **strings**, ascending |
| `GET /v2/purpur/{v}/{build\|latest}` | `{"project","version","build":"2329","result":"SUCCESS","timestamp":1730406106810,"duration":352888,"commits":[{"author","email","description","hash","timestamp"}],"metadata":{},"md5":"dc0026462cac76c869335feb43388a76"}` (timestamp in ms; checksum is **md5** only) |
| `GET /v2/purpur/{v}/{build\|latest}/download` | 200 `application/octet-stream`, `content-disposition: attachment; filename="purpur-1.21.1-2329.jar"` |

- Unknown version: 404 `{"error":"version not found"}`. The API has no Java field, so use Mojang (§2).

## 9. Implementation notes for the panel
- Java: fetch the Mojang manifest, then the version JSON, read `javaVersion.majorVersion`, and map it to an itzg tag (§2). Cache the per-version JSON indefinitely, since it's content-addressed by sha1. Refresh the manifest at most every few minutes.
- If the server type is FORGE and MC ≤ 1.16.5, force `java8`. Never use `latest`/`stable` for old versions: they're currently Java 25.
- The version pickers' "stable" flags differ by API: Fabric has `stable`, Quilt and NeoForge mark betas with `-beta`, Forge has `recommended` vs `latest`, Paper has `channel` + `support.status`, and Purpur has `result`.
- Parse versions with a tolerant parser that handles `1.x.y`, `26.x[.y]`, `-snapshot-N`, `-pre-N`/`-preN`, `-rc-N`/`-rcN`, and `NNwNNa`.
