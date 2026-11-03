# Backup engine (restic) and filesystem snapshots reference

Checked 2026-11-03. Sources: restic `master` docs (`doc/*.rst` on raw.githubusercontent.com), `restic <cmd> --help` and **real runs of `ghcr.io/restic/restic:0.19.1` against a local test repo** ("observed" below); btrfs-progs `Documentation/*.rst` plus `libbtrfsutil/subvolume.c`; Linux `fs/btrfs/ioctl.c`, `include/uapi/linux/magic.h`, `Documentation/filesystems/proc.rst`; OpenZFS `man/man{7,8}` plus `policy.c` and `libzfs_core.c`; docs.unraid.net (shares, file-systems, cache-pools, zfs-storage, 6.12.0 release notes). Btrfs, ZFS and Unraid were **not** tested on real hardware. **UNVERIFIED** marks inference.

## 1. Image
- Docker Hub `restic/restic` tags (hub API): `latest` and `0.19.1` (2026-07-05, amd64 about 17.5 MB), `0.19.0` (2026-06-09), `0.18.1` (2025-09-21). **Pin `0.19.1`**. The same image is at `ghcr.io/restic/restic:0.19.1`. Unauthenticated Docker Hub *registry* pulls returned `429 TOOMANYREQUESTS` here; GHCR worked.
- Alpine/busybox, runs as root, `Entrypoint ["/entrypoint.sh"]`, which runs `exec nice -n "${NICE:-0}" [ionice -c $IONICE_CLASS -n ${IONICE_PRIORITY:-4}] /usr/bin/restic "$@"`. Image env: `NICE=0`, `IONICE_CLASS=`, `IONICE_PRIORITY=4`. restic is therefore PID 1. For a shell, use `--entrypoint sh`.
- Docs: "Restic relies on the hostname… set a static hostname using `--hostname`". Observed: without `--host`, the snapshot `hostname` is the container hostname. **Always pass `--host <stable>`** (or `RESTIC_HOST`).
- `restic version --json` gives `{"message_type":"version","version":"0.19.1","go_version":"go1.26.4","go_os":"linux","go_arch":"amd64"}`.

## 2. Environment variables (subset of the documented list)
| Var | Meaning |
|---|---|
| `RESTIC_REPOSITORY` / `RESTIC_REPOSITORY_FILE` | repo location (`-r`) / file containing it |
| `RESTIC_PASSWORD_FILE` | password file (`-p/--password-file`). Alternatives: `RESTIC_PASSWORD`, `RESTIC_PASSWORD_COMMAND` |
| `RESTIC_CACHE_DIR` | cache dir (`--cache-dir`). Default `$XDG_CACHE_HOME/restic` or `~/.cache/restic`; restic exits with an error if neither env var is set. `--no-cache` disables the cache |
| `RESTIC_HOST` | hostname for new snapshots **and** the default `--host` filter for snapshots/forget/check/stats |
| `RESTIC_COMPRESSION` | `auto|off|fastest|better|max` (repo v2) |
| `RESTIC_PROGRESS_FPS` | status-message rate (e.g. `0.0166` is about 1/min) |
| `RESTIC_PACK_SIZE`, `RESTIC_READ_CONCURRENCY` (default 2), `TMPDIR` | tuning and temp files |
| `RESTIC_REST_USERNAME/PASSWORD`, `AWS_*`, `B2_ACCOUNT_ID/KEY`, `AZURE_*`, `GOOGLE_*` | remote backends |
Explicit flags override env vars. `check` creates a fresh temporary cache unless `--with-cache` is passed.

## 3. Exit codes and errors (docs, all confirmed by real runs)
| Code | Meaning |
|---|---|
| 0 | success |
| 1 | fatal error (backup: no snapshot). Also `init` on an existing repo: `Fatal: create repository at /s/repo failed: config file already exists` |
| 2 | Go runtime error |
| 3 | `backup` could not read some source data (snapshot **is** created), or `forget` could not remove some snapshots |
| 10 | repository does not exist (since 0.17.0) |
| 11 | failed to lock repository (since 0.17.0) |
| 12 | wrong password (since 0.17.1) |
| 130 | cancelled (SIGINT/SIGTERM) |
Unknown codes **must** be treated as failure. With `--json`, fatal errors print a final `exit_error` line on **stderr**. Observed lines:
```json
{"message_type":"exit_error","code":10,"message":"Fatal: repository does not exist: unable to open config file: stat /s/repo/config: no such file or directory\nIs there a repository at the following location?\n/s/repo"}
{"message_type":"exit_error","code":12,"message":"Fatal: wrong password or no key found"}
{"message_type":"exit_error","code":11,"message":"unable to create lock in backend: repository is already locked by PID 1 on other-host by root (UID 0, GID 0)\nlock was created at 2026-11-03 17:43:15 (1.541646393s ago)\nstorage ID 55ec9663\nthe `unlock` command can be used to remove stale locks"}
```
Some plain-text warnings still go to stderr in JSON mode (observed: `/s/nope does not exist, skipping`). Parse stdout line by line as JSON and treat stderr separately.

**Already-initialized check (documented approach):** run `restic cat config`. Exit 0 prints `{"version":2,"id":"dbd4…","chunker_polynomial":"3c7363ac2029bd"}`; then 10 means run `init`, 12 means wrong password (never re-init), 11 means retry, anything else is an error. `init --json` prints `{"message_type":"initialized","id":"dbd4c5…","repository":"/s/repo"}`. Repo format v2 (compression) is the default since 0.14.

## 4. Commands
### backup
`restic backup [--host H] [--tag t1,t2]… [--exclude P]… [--exclude-file F]… [--exclude-caches] [--skip-if-unchanged] [--json] [--quiet] PATH…`
- Summary (last stdout line, observed):
```json
{"message_type":"summary","files_new":3,"files_changed":0,"files_unmodified":0,"dirs_new":3,"dirs_changed":0,"dirs_unmodified":0,"data_blobs":3,"tree_blobs":4,"data_added":202160,"data_added_packed":201641,"total_files_processed":3,"total_bytes_processed":200050,"total_duration":0.725842749,"backup_start":"2026-11-03T17:41:59.834425626Z","backup_end":"2026-11-03T17:42:00.560268391Z","snapshot_id":"6c90a305ec262a2f7f0441f1efb41807499233c919e21d067e197d9f4e9d9d59"}
```
- Fields: `dry_run`, `files_new/changed/unmodified`, `dirs_*`, `data_blobs`, `tree_blobs`, `data_added` (uncompressed), `data_added_packed`, `total_files_processed`, `total_bytes_processed`, `backup_start`, `backup_end`, `total_duration` (seconds, float), `snapshot_id`. Fields with default values may be omitted.
- With `--skip-if-unchanged` and no changes, **`snapshot_id` is absent** (observed). Handle that as "no new snapshot", not as an error.
- Other message types: `status` (`percent_done`, `total_files`, `files_done`, `total_bytes`, `bytes_done`, `seconds_remaining`, `current_files`…), `error` on stderr (`error.message`, `during`, `item`), and `verbose_status`/`excluded_item` with `-v`. Observed in 0.19.1 with a non-TTY pipe: **no `status` lines**, even with `RESTIC_PROGRESS_FPS=20` (the backup lasted under 1 s). Don't depend on progress lines.
- One missing path plus one valid path gives a summary, a snapshot and **exit 3**. If all paths are missing: exit 1, `Fatal: all source directories/files do not exist`.
- Paths: snapshot `paths` always holds absolute paths. With a relative source, the tree is relative. Observed: `-w /s/data backup .` gave `paths:["/s/data"]` and tree entries `/world/level.dat`. Change detection depends on the path, so keep the same form every time.
- Parent selection: the latest snapshot in the same `--group-by` group (default `host,paths`; **tags are not used**). If you change it, use the same `--group-by` for `forget`. `--force` rereads everything. `--ignore-inode` is meant for FUSE sources.
- Excludes: `--exclude`/`-e`, `--iexclude`, `--exclude-file`, `--iexclude-file`, `--exclude-caches` (excludes a directory's contents when it has a `CACHEDIR.TAG` (header `Signature: 8a477f597d28d172789f06886806bc55`) but keeps the tag file; observed), `--exclude-if-present name[:header]`, `--exclude-larger-than 1M`. Patterns match whole path components, a leading `/` anchors the pattern, `**` matches across directories, `$VAR` is expanded in exclude files and `~` is not. Paths passed explicitly are never excluded (0.19.0 #5767). Observed: the snapshot `excludes` field lists only the `--exclude` patterns, not the contents of exclude files.
- Other flags: `--one-file-system`, `--files-from`, `--stdin-from-command -- cmd…` (with `--stdin-filename`; a non-zero exit from cmd cancels the backup with exit 1), `--time`, `--dry-run`, `--read-concurrency`.

### snapshots
`restic snapshots --json [--host H] [--tag t] [--path /abs] [--latest N] [--group-by …]`. Returns a JSON array, `[]` when nothing matches (observed). Element (observed, `summary` trimmed):
```json
{"time":"2026-11-03T17:41:59.834425626Z","tree":"f54fa9…","paths":["/s/data"],"hostname":"mc-agent1","username":"root","excludes":["*.lock"],"tags":["instance:42","kind:scheduled"],"program_version":"restic 0.19.1","summary":{"backup_start":"…","backup_end":"…","files_new":3,"data_added":202160,"total_bytes_processed":200050},"id":"6c90a305ec26…9d59","short_id":"6c90a305"}
```
Other fields: `parent`, `uid`, `gid` (omitted when 0). `short_id` is documented as deprecated, so use `id[:8]` instead. Tag filters: one `--tag a,b` means a AND b; repeated `--tag a --tag b` means a OR b; `--tag ''` matches untagged snapshots. Tags containing `:` work (observed); tags cannot contain `,`.

### ls
`restic ls --json <id|latest> [--host/--tag/--path] [/abs/dir…] [--recursive]`. JSON lines: the first line is the snapshot (`message_type:"snapshot"` plus the snapshot fields), then one `node` per entry (observed):
```json
{"name":"level.dat","type":"file","path":"/world/level.dat","uid":0,"gid":0,"size":6,"mode":420,"permissions":"-rw-r--r--","mtime":"…","atime":"…","ctime":"…","inode":788275,"message_type":"node","struct_type":"node"}
```
Directory filters must be absolute and only list direct children unless `--recursive`. Also available: `--long`, `--sort name|size|time|atime|ctime|extension`, `--reverse`, `--ncdu`.

### restore
`restic restore <id|latest>[:<subfolder>] --target DIR [--include P]… [--exclude P]… [--overwrite always|if-changed|if-newer|never] [--delete] [--verify] [--dry-run -vv] [--json]`
- `--include` and `--exclude` cannot be combined (there are also `--iinclude`/`--include-file` variants). `--path` and `--host` only *select* the snapshot.
- `restore 6c90a305 --target /s/restore1 --include /world/level.dat` produced `/s/restore1/world/level.dat`. `restore 6c90a305:/world --target /s/restore2` put `level.dat` and `region/` directly in the target (observed). With `:subfolder`, include/exclude paths are relative to the subfolder.
- Summary (observed): `{"message_type":"summary","total_files":1,"files_restored":2,"total_bytes":6,"bytes_restored":6}`. Counts include directories. Other fields: `seconds_elapsed`, `files_skipped`, `files_deleted`, `bytes_skipped`. Dry run with `-vv` emits `verbose_status` lines with `action` = `restored|updated|unchanged|deleted`.
- `--delete` removes files that are not in the snapshot. The master docs say that with `--target --delete` you must also pass `--include` or `--exclude`; 0.19.1 accepted it without (observed with `--dry-run`). Pass `--include /` anyway. The docs recommend making a backup before an in-place restore.
- restic restores mode, uid/gid, timestamps and xattrs (`--ownership-by-name` is new in 0.19).

### dump
`restic dump <id|latest> /path/file > out`: a single file goes to stdout (observed `level`). A directory is written as tar by default or zip with `-a zip` (observed tar entries `world/level.dat`, `world/region/r.0.0.mca`). `latest:/world /` puts the contents at the archive root. `--target FILE` writes to a file. `--host/--tag/--path` apply only with `latest`.

### forget and prune
`restic forget --json [--tag instance:42] [--host H] [--group-by ''] --keep-last N --keep-daily N --keep-weekly N [--keep-monthly/yearly/hourly N] [--keep-within 7d] [--keep-tag t] [--dry-run] [--prune] [--max-unused 5%]`
- Output is a JSON array of ForgetGroup `{tags, host, paths, keep:[Snapshot], remove:[Snapshot]|null, reasons:[{snapshot, matches:["last snapshot",…]}]}`. With `--group-by ''` it was `"tags":null,"host":"","paths":null` (observed). Forgetting explicit IDs prints nothing (observed).
- Policies are ORed. Calendar buckets use natural boundaries (weeks run Mon–Sun). `unlimited` is allowed. Grouping defaults to `host,paths` and the policy applies per group. If every snapshot in a group would be removed, `--unsafe-allow-remove-all` is required.
- `--prune` runs prune only if something was removed. Observed in 0.19.1: in JSON mode `forget --prune` printed **only** the forget array, and **`prune --json` prints plain text** (`to delete: … remaining: 15 blobs / 198.194 KiB … done`). The JSON `prune` summary (`blobs/bytes/packfiles`) in the master docs is not released yet. Rely on the exit code.
- The docs say prune locks the repo and backups cannot complete during it, and suggest running `check` after prune.

### check
`restic check --json [--read-data | --read-data-subset 1/5 | 2.5% | 50M] [--with-cache] [--host/--tag/--path (0.19.0+)]`. Observed output: `{"message_type":"summary","num_errors":0,"broken_packs":null,"suggest_repair_index":false,"suggest_prune":false}`. Errors are `{"message_type":"error","message":…}` lines on stderr. `n/t` rotates through all packs over t runs. `x%` and `nS` pick a random subset.

### stats
`restic stats --json [--mode restore-size|files-by-contents|blobs-per-file|raw-data] [--host/--tag/--path] [id|latest…]` (observed):
```json
{"total_size":202099,"total_uncompressed_size":203534,"compression_ratio":1.0071004804575976,"compression_progress":100,"compression_space_saving":0.705041909459847,"total_blob_count":10,"snapshots_count":2}
{"total_size":200050,"total_file_count":6,"snapshots_count":1}
```
The first line is `raw-data` over all snapshots (deduplicated size in the repo). The second is `restore-size` for `--tag instance:42 latest`; its `total_file_count` includes directories. In a shared repo, `raw-data --tag X` does **not** tell you how much space deleting X would free, because blobs are shared (UNVERIFIED).

### unlock
`restic unlock` removes only stale locks. Observed: it kept a fresh lock from a live process with a different hostname. `unlock --remove-all` removes all locks, including live ones, which is dangerous.

## 5. Locking and concurrency
- From design.rst: exclusive locks allow no other locks; any number of non-exclusive locks can coexist. A lock is a file in `locks/` (`{time, exclusive, hostname, username, pid, uid, gid}`). It is stale if older than 30 min, or if it was made on the *same hostname* and the PID is gone. `--retry-lock 5m` retries until it gets the lock (observed: `forget` waited about 16 s for a backup, then succeeded). `--no-lock` is for read-only repos.
- Observed in 0.19.1 while a backup held a lock: **`check`, `forget` and `prune` failed with exit 11** (exclusive). **`backup`, `snapshots`, `restore` and `stats` worked** (non-exclusive, so two backups can run at once).
- Container caveat (UNVERIFIED): the PID-liveness check runs in the checker's own PID namespace. In the official image restic is PID 1, so a same-hostname lock always looks alive and only the 30-min rule applies. If restic runs as a subprocess (not PID 1) in containers that share a hostname, a live lock could be treated as stale. Give each runner a unique `--hostname`.
- **Recommendation: one repository per instance** (e.g. `/backups/<instance_uuid>/repo`, each with its own password file).

| | Shared repo | Repo per instance |
|---|---|---|
| Lock contention | prune/check/forget of any instance blocks backups of **all** instances | isolated |
| Retention safety | must filter `--tag instance:<id>` and use `--group-by` that includes tags, or a unique host/path per instance. Default `host,paths` mixes instances that all back up `/data` | `--group-by ''` is safe |
| Dedup | across instances (shared modpack jars) | within the instance only |
| Delete an instance | forget plus a long prune | `rm -rf` the repo dir |
| Corruption blast radius | all instances | one |

## 6. Detecting the filesystem from a container
- GNU `stat -f -c %T PATH` prints `btrfs`, `zfs`, `fuse`, `ext2/ext3` (ext4), `xfs`… (coreutils stat.c). **busybox stat (Alpine/restic image) printed `UNKNOWN` for overlay** (observed), so use the hex magic `stat -f -c %t`:

| fs | f_type | source |
|---|---|---|
| btrfs | `0x9123683e` | magic.h |
| zfs | `0x2fc12fc1` | OpenZFS `ZFS_SUPER_MAGIC` |
| fuse (Unraid shfs) | `0x65735546` | magic.h |
| ext4 | `0xef53` | magic.h |
| xfs | `0x58465342` | magic.h |
| overlayfs / tmpfs | `0x794c7630` / `0x1021994` | magic.h |
- In Python (`os.statvfs` has no f_type), parse `/proc/self/mountinfo`. Format: `ID PARENT MAJ:MIN ROOT MOUNTPOINT OPTS [optional…] - FSTYPE SOURCE SUPEROPTS`. FSTYPE has the form `type[.subtype]` (so shfs is presumably `fuse.shfs`, UNVERIFIED). Pick the entry whose mount point is the longest prefix of `realpath(path)`. For bind mounts, ROOT is the source path inside that filesystem. Observed:
  `111 93 254:0 /tmp/…/rt/data /data rw,relatime - ext4 /dev/vda rw,discard,…`
  For ZFS, SOURCE is the dataset name (`pool/ds`). For btrfs, SUPEROPTS include `subvolid=…,subvol=/…`. Both are UNVERIFIED here.
- A btrfs path is a subvolume root iff f_type is btrfs and `st_ino == 256` (libbtrfsutil `btrfs_util_is_subvolume`; nested-subvolume stubs inside snapshots have inode 2).
- Default Docker capabilities (observed): `CapEff 00000000a80425fb`. That has CHOWN, DAC_OVERRIDE, **FOWNER**, FSETID, KILL, SETGID, SETUID, SETPCAP, NET_BIND_SERVICE, NET_RAW, SYS_CHROOT, MKNOD, AUDIT_WRITE and SETFCAP, and **no SYS_ADMIN**. `/dev/zfs` is absent unless passed in.

## 7. Btrfs (needs `btrfs-progs` in the agent image)
| Command | Notes |
|---|---|
| `btrfs subvolume show PATH` | prints Name, UUID, Parent UUID, Creation time, Subvolume ID, Generation, Flags (`readonly`), Snapshot(s) |
| `btrfs subvolume snapshot -r SRC DEST/NAME` | read-only snapshot. If SRC is not a subvolume, it errors. SRC and DEST must be on the same filesystem (kernel `-EXDEV`) |
| `btrfs subvolume create [-p] PATH` | make each instance dir a subvolume so it can be snapshotted |
| `btrfs subvolume delete [-c] PATH` / `sync PATH` | deletion is async. `-c` commits |
| `btrfs subvolume list PATH` | tree search, needs privileges |
- Permissions (from kernel source): the snapshot ioctl needs **owner of SRC or CAP_FOWNER**, not CAP_SYS_ADMIN. Delete needs **CAP_SYS_ADMIN** unless the filesystem is mounted with `user_subvol_rm_allowed`. Since 4.18, `rmdir` can remove an *empty* subvolume. `delete -R` needs CAP_SYS_ADMIN or `user_subvol_rm_allowed`.
- libbtrfsutil uses privileged tree-search ioctls (CAP_SYS_ADMIN) when `geteuid()==0`, and an unprivileged path otherwise. So `subvolume show` as root in a default container probably fails with EPERM (UNVERIFIED). Use the stat/inode-256 check, or `--cap-add SYS_ADMIN`.
- Snapshots are not recursive: nested subvolumes show up as empty dirs. Docs: "A snapshot is not a backup". It freezes state for restic to read.
- There is no in-place rollback. Recipe (UNVERIFIED): stop the server, move or delete the live subvolume, `btrfs subvolume snapshot RO_SNAP LIVE_PATH` (writable), start the server.

## 8. ZFS (needs `zfsutils` in the agent image plus `--device /dev/zfs`)
- `libzfs_core` opens `/dev/zfs` read-write (`ZFS_DEV "/dev/zfs"`). The kernel policy `secpolicy_zfs()` requires **CAP_SYS_ADMIN**; otherwise permissions must be delegated with `zfs allow <user> snapshot,destroy,rollback,clone,mount <dataset>`. snapshot, rollback, destroy and create also require `mount`, and clone needs create and mount. **On Linux, mount, unmount, mountpoint, canmount, rename and share cannot be delegated.** UNVERIFIED: userland and kernel module versions must be compatible.
- `zfs list -H -o name,mountpoint [-p] [-t filesystem|snapshot] [-r|-d N] [DATASET]`: `-H` drops headers and separates columns with tabs. `-p` gives exact numbers. `-j [--json-int]` gives JSON (in the current man page; which release added it is UNVERIFIED).
- `zfs snapshot [-r] [-o prop=val] pool/ds@name`: atomic, and `-r` snapshots all descendants at once. Read-only access is at `<dataset mountpoint>/.zfs/snapshot/<name>/`, which exists only at the dataset root. `snapdir=hidden` (default) means the directory is not listed but can still be opened.
- `zfs rollback [-r|-R [-f]] pool/ds@snap`: by default only to the **most recent** snapshot. `-r` destroys newer snapshots and bookmarks. `-R` also destroys their clones (`-f` force-unmounts them). This discards all changes since the snapshot.
- `zfs clone [-p] [-u] [-o …] pool/ds@snap pool/new`: writable and instant. The origin snapshot cannot be destroyed while the clone exists. `zfs promote` reverses the dependency. `zfs destroy pool/ds@snap` removes a snapshot.
- Snapshots are per dataset. A per-instance child dataset (`zfs create pool/appdata/mc-<id>`) keeps rollbacks per instance, but it needs create and mount rights (CAP_SYS_ADMIN, or doing it on the host).

## 9. Unraid
- `/mnt/user/<share>` is a user share on the **FUSE-based shfs** that merges the array and pools; `/mnt/user0` is deprecated. Physical paths: `/mnt/diskN` (array disks, **XFS by default**, ext4/btrfs/ZFS possible) and `/mnt/<poolname>` (most commonly `/mnt/cache`; pools default to **btrfs**, ZFS since **6.12**). Snapshot tools must target these, never `/mnt/user` (it shows f_type fuse `0x65735546`). The docs warn never to copy between a user-share path and a disk-share path of the same share.
- 6.12.0 notes: "Top-level user shares in a ZFS pool are created as datasets". **Exclusive shares** ("Permit exclusive shares", default No): if primary storage is a pool, secondary is none, and the share exists nowhere else, `/mnt/user/<share>` becomes a **symlink** to the pool dir and bypasses FUSE. Docker resolves the symlink at bind time, so the container would see the real fs (UNVERIFIED).
- A non-exclusive share can spread files across several disks, so no single snapshot covers it. Fall back to plain restic.
- On btrfs pools, top-level share dirs are probably plain directories, not subvolumes (UNVERIFIED). Create instance dirs with `btrfs subvolume create` to make them snapshot-capable.
- Detection order (suggestion): host path → `realpath` (follows the exclusive-share symlink) → mountinfo/f_type → btrfs with inode 256, or zfs with a dataset whose mountpoint equals the path, means snapshot-capable; anything else is restic-only.

## 10. Suggested agent flow (inferred, not upstream guidance)
1. Quiesce through RCON: `save-off`, then `save-all flush` (see `itzg-runtime.md` for `rcon-cli`). 2. Take a snapshot if possible (btrfs `-r` or ZFS `@name`), then `save-on`. 3. `restic backup --host <agent-id> --tag instance:<id> --tag kind:<scheduled|manual|pre-restore> --exclude-caches` run from the snapshot path or `/data`, using the same form every time, with `--retry-lock 5m`. 4. Delete the fs snapshot. 5. Off-peak: `forget --group-by '' --keep-last … --prune`, then `check --read-data-subset=5%`; both need the exclusive lock. 6. Restore: stop the container, `restore <id> --target /data --include / --delete` (back up first, tagged `kind:pre-restore`), or a fs rollback for a local fast path. Persist `RESTIC_CACHE_DIR` on a volume. The `.rcon-cli.env`/`.rcon-cli.yaml` files in `/data` hold the RCON password and could be excluded.
