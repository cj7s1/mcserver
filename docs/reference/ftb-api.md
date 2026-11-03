# FTB (Feed The Beast) modpack API + itzg `TYPE=FTBA` reference

Verified 2026-11-03 with real `curl` requests. Sources:
- itzg docs `docs/types-and-platforms/mod-platforms/ftb.md` and `docs/versions/java.md`, plus the script `scripts/start-deployFTBA` (raw.githubusercontent.com, `master`). The repo root has no `mkdocs.yml` (404), but the path above works.
- Live API: `https://api.feed-the-beast.com/v1/modpacks/public/...` ("new") and `https://api.modpacks.ch/public/...` ("legacy").
- No official FTB API documentation was found. The API root `GET https://api.feed-the-beast.com/v1/modpacks/` returns 404 `Cannot GET /`. Everything below comes from observed responses. Anything not observed is marked **UNVERIFIED**.

## 1. Hosts: use the new one

| | New: `https://api.feed-the-beast.com/v1/modpacks` | Legacy: `https://api.modpacks.ch` |
|---|---|---|
| itzg default | yes: `FTB_BASE_URL` default (script comment: "Legacy base URL was https://api.modpacks.ch") | no |
| Server | Express, Cloudflare. `cache-control: public, max-age=300`, `access-control-allow-origin: *`, weak `etag` | `cache-control: max-age=900 ... stale-while-revalidate=86400`, headers `x-modpackapi-ram-cache: STALE` |
| JSON | compact | pretty-printed |
| Freshness | current: pack 119 has 23 versions incl. `100051` "1.16.1" (2025-03) | **stale**: pack 119 has 22 versions, no `100051`. `GET /public/modpack/119/100051` gives HTTP **200** `{"status":"error","message":"Version id not valid.","target":""}` |
| Search `term=direwolf` | `packs:[126,119,101,95,79]` | `packs:[14,24,31,62]` (different index) |
| Errors | real HTTP 404/400 | HTTP 200 with `status:"error"` |

Conclusion: call the new host, path prefix `/v1/modpacks/public`. Always check both the HTTP code and `status == "success"`.

## 2. Endpoints (new host, all verified with GET)

Base `B = https://api.feed-the-beast.com/v1/modpacks/public`

| Path | Result |
|---|---|
| `B/modpack/popular/installs/{N}` | IDs by installs. `{"status","packs":[int],"count","limit","updated"}` |
| `B/modpack/popular/plays/{N}` | IDs by plays. `{"status","limit","packs","count","refreshed"}` |
| `B/modpack/search/{N}?term=X` | `{"status","packs":[ftbId],"curseforge":[cfProjectId],"count","updated"}` |
| `B/modpack/featured/{N}` | `{"status","total","packs","limit","refreshed"}` (N=5 returned 4) |
| `B/modpack/updated/{N}` | recently updated IDs, same shape as featured |
| `B/modpack/all` | `{"status","total":95,"packs":[135,134,...]}` (IDs only) |
| `B/modpack/{id}` | full pack + `versions[]` (summaries, no files) |
| `B/modpack/{id}/{versionId}` | one version + `files[]` (pack 119/100051 is 341 KB with 429 files) |
| `https://api.feed-the-beast.com/v1/modpacks/modpack/{id}/{versionId}/changelog` | **no `/public`** (this is the URL the version's `changelog` field gives). `{"status","updated":<ms>,"content":"## 1.16.1\n\n### Added..."}` (Markdown) |
| `B/modpack/{id}/{versionId}/server/linux` | server installer binary. `application/octet-stream`, ~18 MB, `content-disposition: attachment; filename="serverinstall_1_1"` |
| `B/modpack/{id}/{versionId}/server/arm/linux` | ARM installer, ~17 MB (checked with HEAD only) |
| `B/curseforge/{cfProjectId}` | a CurseForge-hosted pack in the FTB pack shape (see §7) |

List endpoints return **IDs only**. The panel needs one `GET B/modpack/{id}` per card, so cache them (the server caches for 300 s).

Observed list behaviour:
- `popular/installs/3` returned **5** IDs on both hosts. The minimum seems to be 5 (**UNVERIFIED** rule).
- `search/3?term=stoneblock` gives `{"packs":[130,100,4],"curseforge":[1373378,1091305,310396,290913,1283935],"count":8}`. N limits `packs`, but `curseforge` still had 5 entries. `count` = `packs` + `curseforge`.
- `search/5?term=zzzqqqxx` gives `{"status":"success","packs":[],"curseforge":[],"count":0,...}`.
- `search/5` with no term gives **400** `{"status":"error","error":"No search term provided"}`.
- `updated`/`refreshed` on list endpoints are Unix **seconds**. The changelog `updated` is in **milliseconds**.

Real responses:
```json
// GET B/modpack/popular/installs/10
{"status":"success","packs":[100,35,91,4,103,79,25,23,101,119],"count":10,"limit":10,"updated":1793727246}
// GET B/modpack/search/5?term=direwolf
{"status":"success","packs":[126,119,101,95,79],"curseforge":[281999,1103362,225578,225562,225589],"count":10,"updated":1793727247}
```
Legacy popular shape: `{"packs":[...],"total":5,"type":"installs","limit":5,"status":"success","refreshed":...,"days":0}`

## 3. `GET B/modpack/{id}`: pack object

Top-level keys (new host): `status, id, name, slug, synopsis, description, type, released, updated, private, featured, plays, installs, plays_14d, tags, links, art, authors, provider, notification, rating, meta, versions`. The legacy host has no `slug` or `meta`, and adds `refreshed`.

| Field | Notes (observed) |
|---|---|
| `id` int, `name`, `slug` | e.g. `119`, `"FTB Presents Direwolf20 1.20"`, `"ftb-presents-direwolf20-120"` |
| `synopsis` | one-line plain text |
| `description` | **Markdown** for FTB packs (starts with `# ...` on 126) |
| `type` | `"release"` for FTB packs (`"modpack"` for the CF endpoint) |
| `released`, `updated` | Unix seconds. The pack-level `updated` can be **older** than `released` (119: released 1696353509, updated 1693928127), so use version timestamps instead |
| `installs`, `plays`, `plays_14d` | ints (119: installs 247617, plays 1884344) |
| `tags[]` | `{"id":9,"name":"FTB"}`, `{"id":13,"name":"Tech"}`, ... |
| `links[]` | `{"id":-1,"name":"Wiki","link":"https://docs.feed-the-beast.com","type":"misc"}`. Types seen: `misc`, `presskit`, `website` |
| `art[]` | `{url,type,width,height,sha1,title,order,compressed,mirrors[],id,updated}`. **Types seen: `square` (512x512, title "Logo"), `splash` (1920x1080, "Background")**. No `logo` type was seen. On the CF endpoint: `square`, `thumbnail` |
| `authors[]` | `{"id":-1,"website":"https://www.feed-the-beast.com/","name":"FTB Team","type":"team","updated":0}` |
| `provider` | `"modpacks.ch"` for FTB packs, `"curseforge"` on the CF endpoint |
| `rating` | `{id,configured,verified,age:7,gambling,frightening,alcoholdrugs,nuditysexual,sterotypeshate(sic),language,violence}` |
| `meta` | `{"supportsWorlds":true,"curseforgeProjectId":1103362\|null,"isLegacy":false}`. The meaning of `isLegacy` is **UNVERIFIED** |
| `private`, `featured` | bool |

Art URLs are on `https://cdn.feed-the-beast.com/blob/..`. The CDN answers 200 but with `content-type: application/octet-stream` (not `image/png`). `<img>` still renders, but a proxy or cache that checks MIME type would need to sniff the content.

### `versions[]` entries (summary)
Keys: `id, name, type, updated, released, private, specs, targets`. The legacy host also has `creeperhostVersionId`, and its targets have real ids.
```json
{"specs":{"minimum":4096,"recommended":6144,"id":-1},
 "targets":[{"id":-1,"name":"minecraft","version":"1.20.1","type":"game","updated":0},
            {"id":-1,"name":"neoforge","version":"47.1.84","type":"modloader","updated":0},
            {"id":-1,"name":"java","version":"17.0.7+7","type":"runtime","updated":0}],
 "id":100051,"name":"1.16.1","type":"release","updated":1741881037,"released":1741881037,"private":false}
```
- `specs.minimum` / `specs.recommended` are RAM in **MB** (4096/6144 on every pack checked). The unit is inferred from the values: **UNVERIFIED**.
- `targets[].type`: `game` (name `minecraft`), `modloader` (names seen: `forge`, `neoforge`, `fabric`), `runtime` (name `java`).
- Loader examples: 100 "FTB StoneBlock 3" → `forge 40.2.34`, mc 1.18.2, java `17.0.2+8`. 35 "FTB Revelation" → `forge 14.23.5.2858`, mc 1.12.2, java `8.0.312+7`. 109 "FTB Unstable 1.20: Fabric" → `fabric 0.14.21`. 126 → `neoforge 21.1.248`, mc 1.21.1, java `21.0.4+7-LTS`. 135 "FTB Evolution 2" → `neoforge 26.1.2.126`, mc `26.1.2`, java `25.0.2+10-LTS`.
- Note: 119 reports `neoforge 47.1.84` for mc 1.20.1. That is the 1.20.1 NeoForge fork, whose version numbers look like Forge's.
- **Version `type` values seen: `release`, `beta`, `archived`**. A name like `1.0.0-rc.1` can still be `release`. `private:true` was not seen in packs 100, 109, 135.
- `versions[]` came back sorted by `updated` ascending on 119, 100 and 135, but sort it yourself anyway.
- No `quilt` loader was seen. Whether one exists is **UNVERIFIED**.

## 4. `GET B/modpack/{id}/{versionId}`: version detail
Keys: `status, id, name, type, updated, refreshed, plays, installs, changelog (URL), parent (pack id), notification, links, private, targets, specs, files`.
`files[]` item:
```json
{"id":307831584,"version":"0","path":"./config/Advancedperipherals","name":"world.toml",
 "url":"https://files.feed-the-beast.com/blob/5c/5c8c...cc15.toml","mirrors":[],"tags":[],
 "sha1":"7023...b","hashes":{"sha1":"..","sha256":"..","sha512":"..","murmur":1899780715,"cfMurmur":2306861601},
 "size":302,"clientonly":false,"serveronly":false,"optional":false,"type":"config","updated":1741881037}
```
The panel normally doesn't need `files[]`, because the itzg installer handles it. Avoid this call in UI paths: it can be over 300 KB.

## 5. Errors (new host)
- Unknown pack: `GET B/modpack/999999` gives **404** `{"status":"error","message":"Modpack not found"}`.
- Unknown version: `GET B/modpack/119/1` gives **404** `{"status":"error","ok":false,"message":"Modpack version not found"}`.
- Missing search term gives **400** `{"status":"error","error":"No search term provided"}`. The error text is in `error` here and in `message` elsewhere, so handle both.
- Note: `B/modpack/1/1/server/linux` (the placeholder pack itzg uses) still returns 200 with the installer binary.

## 6. itzg `TYPE=FTBA` (docs + `scripts/start-deployFTBA`)

Selection: `TYPE`, `MODPACK_PLATFORM` or `MOD_PLATFORM` set to **`FTBA`**. The "A" matters: `FTB` used to be an alias for `CURSEFORGE`. `VERSION` doesn't need to be set, because the script derives it from the install.

| Env var | Semantics (verified) |
|---|---|
| `FTB_MODPACK_ID` | **required**, numeric pack id (the `23` in `feed-the-beast.com/modpacks/23-ftb-infinity-evolved-17`). Missing: `FTB_MODPACK_ID is required with TYPE=FTB`, exit 1. The check is `=~ [0-9]+`, which is unanchored, so `abc1` passes |
| `FTB_MODPACK_VERSION_ID` | optional numeric version id (`versions[].id`, e.g. `100051`, **not** the name `1.16.1`). If unset, the script uses `curl -fsSL "${FTB_BASE_URL}/public/modpack/${FTB_MODPACK_ID}" \| jq -r '.versions \| sort_by(.updated)[-1].id'`. This does **not** filter by `type`, so it can pick `beta`/`archived` versions |
| `FTB_FORCE_REINSTALL` | default `false`. `true` re-runs the installer |
| `FTB_BASE_URL` | default `https://api.feed-the-beast.com/v1/modpacks`. Exists in the script only, not in the docs |

Upgrades (docs): with no version ID, restarting the container picks up the newest version. With a pinned ID, recreate the container with the new ID.

Install flow (script):
1. Installer: `/data/ftb-installer-v2`, downloaded once from `${FTB_BASE_URL}/public/modpack/1/1/server/linux`, or `.../server/arm/linux` when `uname -m == aarch64`.
2. Run `ftb-installer-v2 -pack ID -version VID -auto -force -no-java | tee ftb-installer.log`. `-no-java` means the **image's Java is used**.
3. It reinstalls when `FTB_FORCE_REINSTALL`, or when `/data/.manifest.json` or `/data/.ftb-installed` is missing, or when the marker isn't `ID=VID`. It then deletes `forge*installer.jar`, writes the marker and writes the EULA.
4. It reads `/data/.manifest.json`: `.name` (→ `MODPACK_NAME`), `.versionName` (→ `MODPACK_VERSION`), `.modPackTargets.modLoader.{name,version}`, `.modPackTargets.mcVersion` (→ `VERSION`), `.modPackTargets.javaVersion`.
5. It looks for a server launcher in this order: `run.sh`, `fabric-server-launch.jar`, `{loader}-{mc}-{lv}-universal.jar`, `{loader}-{mc}-{lv}-{mc}-universal.jar`, `{loader}-{mc}-{lv}-server-launch.jar`, `{loader}-{mc}-{lv}.jar`. If none is found: `Unable to locate the installed FTB server jar`, exit 2. It then remaps `TYPE`/`FAMILY` to NEOFORGE/FORGE/FABRIC by grepping `run.sh`.

Log lines worth matching: `Downloading FTB installer for x86|ARM`, `Installing modpack ID X, version ID Y`, `This could take a while...`, `FTB modpack ID X, version ID Y is ready to go`, `Unable to resolve latest modpack version ID for modpack X`, `FTB installer failed for modpack ID X, version ID Y`, `Failed to download FTB installer`.

Java / architecture limits:
- Docs: FTBA "Requires one of the Ubuntu with Hotspot images". The script exits 1 on Alpine: `The FTBA installer is not supported on Alpine. Use the java8-multiarch image tag instead.`
- Ubuntu Hotspot tags (java.md): `latest`/`stable`/`java25` (amd64, arm64, riscv64), `java21` (amd64, arm64), `java17`/`java16`/`java11`/`java8` (amd64, arm64, armv7). `java8-multiarch` is deprecated ("move to java8"), but the FTB doc example still uses it. GraalVM tags use Oracle Linux, and the Forge installer fails there.
- The installer is only offered for x86 and ARM, and the script picks ARM only for `aarch64`. On armv7 or riscv64 it would download the x86 binary, which is expected to fail. That is inferred from the script: **UNVERIFIED** at runtime.
- Panel guidance (derived, not itzg policy): choose the image tag from `targets[type=runtime].version` major: 8 → `java8`, 17 → `java17`, 21 → `java21`, 25 → `java25`. Default the memory limit from `specs.recommended`.
- Terms: the itzg FTB doc has no license or terms text. No FTB API terms of use were found: **UNVERIFIED**.

## 7. CurseForge-hosted FTB packs vs the FTB-app API
- Many FTB packs are also on CurseForge. The pack's `meta.curseforgeProjectId` links the two (126 → `1103362`, 100 → `1091305`, 135 → `1731757`; 35 and 119 have `null`). Search results list CF matches separately in `curseforge[]`.
- `GET B/curseforge/{cfProjectId}` (new host) returns the FTB-style shape with differences:
  - `provider:"curseforge"`, `type:"modpack"`, `slug:null`, `meta:null`, `plays:0`, and the typo key `refreashed`.
  - `description` is **HTML** here (Markdown on the legacy host).
  - `art` types are `square` and `thumbnail` (media.forgecdn.net). Authors look like `{"name":"FTBTeam","type":"curseforge"}`. Tag ids are `0`.
  - `versions[]` keys are only `id,name,targets,type,updated`: **no `specs`**. `id` looks like a CF file id (`8947701`), and `name` is the zip filename `ftb-presents-direwolf20-121-1.22.0.zip`.
  - The modloader target has no version number: `{"name":"neoforge","version":"NeoForge","type":"modloader"}`.
  - `links[]` has `{"name":"Website","type":"website","link":"https://www.curseforge.com/minecraft/modpacks/ftb-presents-direwolf20-s14"}`, which can be used as itzg `CF_PAGE_URL` (the slug is the last path segment, for `CF_SLUG`).
- FTB-native and CF ids are different spaces with different versions. Example: FTB 126 latest is `100516` "1.22.0" with a real `neoforge 21.1.248`. CF 1103362 latest is file `8947701` with no loader version.
- For a CF-provider pack, use itzg `TYPE=AUTO_CURSEFORGE` with `CF_PAGE_URL` or `CF_SLUG`, plus `CF_FILE_ID` to pin (see `itzg-runtime.md` §5). Java 17+ images bundle a CF API key; pre-17 tags need `CF_API_KEY`. Whether `ftb-installer-v2 -pack <cfId>` can install CF packs is **UNVERIFIED** and should not be relied on.

## 8. Minimal panel recipe
1. Browse: call `popular/installs/{N}` or `search/{N}?term=`, then `modpack/{id}` for each `packs[]` ID. Show `name`, `synopsis`, the `square` art url, `installs`, `tags[].name`, and the authors.
2. Version picker: sort `versions[]` by `updated` desc and hide `archived` (and optionally `beta`). Show `name` plus the mc/loader/java values from `targets`.
3. Store and send `FTB_MODPACK_ID=<id>` and `FTB_MODPACK_VERSION_ID=<versions[].id>`. Pin the version unless the user explicitly wants auto-latest. Use `TYPE=FTBA` on an Ubuntu tag that matches the Java major.
4. Validate IDs server-side as `^\d+$`. itzg's own check is unanchored.
