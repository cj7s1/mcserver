"""Job handlers for the instance lifecycle (registered with the job runner)."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.core.errors import ApiError, ConflictError
from apps.jobs.models import Job
from apps.jobs.runner import JobContext, register
from apps.nodes.client import AgentClient

from . import properties
from .models import DesiredState, Instance, Status
from .reconcile import reconcile_instance
from .spec import build_spec, restart_required, spec_hash


def _instance(job: Job) -> Instance:
    inst = Instance.objects.select_related("node").get(pk=job.instance_id)
    return inst


def apply_container(
    instance: Instance, ctx: JobContext, *, start: bool, span: tuple[float, float] = (0.1, 0.8)
) -> dict[str, Any]:
    """(Re)create the container from the current configuration. World data is untouched.

    Central guard for every job that (re)creates or starts a server (start, restart, recreate,
    software and modpack updates, rollbacks, migrations): an archived server is never started,
    even by a job that was queued before it was archived (409 ``archived``), and it gets no new
    container either (archiving removed it; the next start after unarchiving creates it from the
    current configuration). The flag is read from the database because the caller's copy may
    predate the archive.
    """
    if Instance.objects.filter(pk=instance.pk, archived_at__isnull=False).exists():
        if start:
            raise ConflictError("This server is archived; unarchive it before starting it", code="archived")
        ctx.log("The server is archived: its container is created when it is next started")
        return {"skipped": "archived"}
    client = AgentClient(instance.node)
    spec = build_spec(instance)
    ctx.progress(span[0], "Preparing container (pulling the runtime image if needed)", force=True)
    op = client.apply(instance.id, spec, start=start)
    result = client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(span[0], span[1]))
    instance.container_spec_hash = spec_hash(spec)
    instance.applied_revision = Instance.objects.filter(pk=instance.pk).values_list("config_revision", flat=True).first() or 0
    instance.save(update_fields=["container_spec_hash", "applied_revision"])
    return result.get("result", {})


def _set_desired(instance: Instance, desired: str) -> None:
    """Record what a job was told to do with the server (start, stop, restart, archive, delete, ...).

    Such a decision also settles a pending "start again after the host restart" mark
    (``resume_after_boot``, set by ``manage.py host_shutdown``): the server is either being
    started now or meant to stay stopped. A kill clears it in ``services.power``.
    """
    instance.desired_state = desired
    instance.resume_after_boot = False
    instance.resume_flagged_at = None
    Instance.objects.filter(pk=instance.pk).update(desired_state=desired, resume_after_boot=False, resume_flagged_at=None)


@register("install")
def install(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Create the data directory, write initial config, create the container and optionally start it."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    initial = job.params.get("server_properties") or {}
    files: dict[str, str] = {}
    if initial:
        files["server.properties"] = properties.render(initial)
    ctx.progress(0.02, "Creating instance directory", force=True)
    client.prepare(instance.id, files)
    start = bool(job.params.get("start", True))
    if start:
        _set_desired(instance, DesiredState.RUNNING)
    apply_container(instance, ctx, start=start, span=(0.05, 0.9))
    if start:
        ctx.progress(0.95, "Server container started; the first start downloads and installs the server software")
    return {"started": start}


@register("start")
def start(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.RUNNING)
    status = client.status(instance.id)
    if not status.get("exists") or restart_required(instance) or not instance.container_spec_hash:
        ctx.progress(0.1, "Applying configuration")
        apply_container(instance, ctx, start=True, span=(0.1, 0.8))
    else:
        ctx.progress(0.3, "Starting container")
        client.start(instance.id)
    return {}


def _warn_players_first(job: Job, ctx: JobContext, action: str) -> dict[str, Any] | None:
    """Run the in-game countdown of a restart/stop enqueued with warn_minutes (see power_warnings).

    Returns a result when the action is skipped; otherwise the job holds the instance lock.
    Jobs without a warning are enqueued exclusive and skip this entirely.
    """
    if job.exclusive:
        return None
    from .power_warnings import PowerCountdown

    return PowerCountdown(job, ctx, _instance(job), action).run()


def _log_stop(ctx: JobContext, answer: Any) -> None:
    """Say in the job log what the agent's graceful stop did (agents before this field send none)."""
    info = answer.get("stop") if isinstance(answer, dict) else None
    if not isinstance(info, dict):
        return
    if info.get("flushed"):
        ctx.log("The world was saved (save-all flush) before the server was stopped")
    if info.get("stop_sent_via_rcon"):
        ctx.log("The server was still starting; the stop was delivered as soon as it accepted commands")
    for warning in info.get("warnings") or []:
        ctx.log(f"warning: {warning}")
    if isinstance(answer, dict) and answer.get("exit_code") == 137:
        ctx.log("warning: the server did not stop in time and was killed")


@register("stop")
def stop(job: Job, ctx: JobContext) -> dict[str, Any]:
    if (skipped := _warn_players_first(job, ctx, "stop")) is not None:
        return skipped
    instance = _instance(job)
    _set_desired(instance, DesiredState.STOPPED)
    ctx.progress(0.2, f"Stopping server gracefully (up to {instance.stop_timeout}s)", force=True)
    status = AgentClient(instance.node).status(instance.id)
    if status.get("exists"):
        _log_stop(ctx, AgentClient(instance.node).stop(instance.id, timeout=instance.stop_timeout))
    return {}


@register("restart")
def restart(job: Job, ctx: JobContext) -> dict[str, Any]:
    if (skipped := _warn_players_first(job, ctx, "restart")) is not None:
        return skipped
    instance = _instance(job)  # (re)loaded after the countdown: settings may have changed
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.RUNNING)
    status = client.status(instance.id)
    if not status.get("exists") or restart_required(instance):
        # also the case for a container made by an older release (spec.CONTAINER_FORMAT)
        ctx.progress(0.1, "Configuration changed; recreating container (world data is kept)")
        if status.get("running"):
            _log_stop(ctx, client.stop(instance.id, timeout=instance.stop_timeout))
        apply_container(instance, ctx, start=True, span=(0.2, 0.9))
    else:
        ctx.progress(0.2, "Restarting server", force=True)
        _log_stop(ctx, client.restart(instance.id, timeout=instance.stop_timeout))
    return {}


@register("recreate")
def recreate(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Recreate the container from the current configuration without touching world data."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    was_running = bool(status.get("running"))
    if was_running:
        ctx.progress(0.05, "Stopping server", force=True)
        client.stop(instance.id, timeout=instance.stop_timeout)
    apply_container(instance, ctx, start=was_running or job.params.get("start", False), span=(0.1, 0.95))
    return {"restarted": was_running}


@register("archive")
def archive(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    ctx.progress(0.1, "Stopping server", force=True)
    _set_desired(instance, DesiredState.STOPPED)
    client.remove_container(instance.id, timeout=instance.stop_timeout)
    instance.archived_at = timezone.now()
    instance.container_spec_hash = ""
    instance.status = Status.ARCHIVED
    instance.save(update_fields=["archived_at", "container_spec_hash", "status"])
    return {}


@register("delete")
def delete(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.STOPPED)
    ctx.progress(0.1, "Deleting container, files and backups", force=True)
    op = client.delete_data(instance.id)
    done = client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(0.1, 0.8))
    for warning in (done.get("result") or {}).get("warnings") or []:
        ctx.log(f"Warning: {warning}")  # e.g. a btrfs snapshot that could not be deleted
    name = instance.name
    instance_id = str(instance.id)
    # detach the job from the instance before deleting so the job history survives
    Job.objects.filter(instance=instance).update(instance=None)
    job.instance_id = None
    instance.delete()
    return {"deleted": instance_id, "name": name}


@register("clone")
def clone(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Copy the source instance's files into the (already created) target instance."""
    source = _instance(job)
    target = Instance.objects.select_related("node").get(pk=job.params["target_id"])
    client = AgentClient(source.node)
    status = client.status(source.id)
    if status.get("running"):
        raise ConflictError("Stop the source server before cloning it", code="source_running")
    ctx.progress(0.05, "Copying server files", force=True)
    op = client.clone(source.id, target.id, exclude_worlds=bool(job.params.get("exclude_worlds")))
    client.wait_operation(op["id"], timeout=7200, on_progress=ctx.agent_progress(0.05, 0.75))
    target.installed_at = source.installed_at
    target.save(update_fields=["installed_at"])
    ctx.progress(0.85, "Creating container for the clone")
    apply_container(target, ctx, start=False, span=(0.85, 0.98))
    return {"target_id": str(target.id)}


def ensure_stopped(instance: Instance, ctx: JobContext) -> bool:
    """Stop the server if running. Returns True if it was running."""
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    if status.get("running"):
        ctx.progress(None, "Stopping server", force=True)
        _log_stop(ctx, client.stop(instance.id, timeout=instance.stop_timeout))
        reconcile_instance(instance.id)
        return True
    return False


def require_not_running(instance: Instance) -> None:
    status = AgentClient(instance.node).status(instance.id)
    if status.get("running"):
        raise ApiError("The server must be stopped for this operation", code="must_stop", status_code=409)


@register("extract_archive")
def extract_archive(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Safely extract an uploaded archive inside the instance (import server / world / server pack)."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    require_not_running(instance)
    p = job.params
    ctx.progress(0.02, f"Extracting {p['path']}", force=True)
    op = client.extract(
        instance.id,
        p["path"],
        p.get("destination", ""),
        strip=p.get("strip", "auto"),
        overwrite=bool(p.get("overwrite")),
        delete_archive=bool(p.get("delete_archive")),
    )
    done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.02, 0.95))
    result = done.get("result") or {}
    if result.get("existing_count"):
        ctx.log(f"{result['existing_count']} existing file(s) were kept (overwrite was off)")
    if result.get("skipped_count"):
        ctx.log(f"{result['skipped_count']} unsafe entries skipped (links, special files or unsafe paths)")
    if p.get("mark_installed") and instance.installed_at is None:
        from django.utils import timezone

        instance.installed_at = timezone.now()
        instance.save(update_fields=["installed_at"])
    return result


@register("software_update")
def software_update(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Change Minecraft/loader/Java versions of a standard server, protected by a pre-update backup."""
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind

    from .spec import image_for

    instance = _instance(job)
    p = job.params
    before = {
        "minecraft_version": instance.minecraft_version,
        "loader_version": instance.loader_version,
        "java_version": instance.java_version,
        "image": instance.image,
    }
    was_running = ensure_stopped(instance, ctx)
    backup_id = None
    if p.get("backup", True):
        ctx.progress(0.05, "Creating a pre-update backup")
        backup = run_backup(
            instance,
            ctx,
            name=f"Before update to {p['minecraft_version']}",
            kind=BackupKind.PRE_UPDATE,
            user=job.user,
            span=(0.05, 0.55),
            job=job,
        )
        backup.meta = {**(backup.meta or {}), "software_before": before}
        backup.save(update_fields=["meta"])
        backup_id = str(backup.id)
    instance.minecraft_version = p["minecraft_version"]
    instance.loader_version = p.get("loader_version", "")
    instance.java_version = int(p["java_version"])
    instance.image = image_for(instance.java_version)
    instance.save(update_fields=["minecraft_version", "loader_version", "java_version", "image"])
    from .services import bump_revision

    bump_revision(instance)
    instance.refresh_from_db()
    ctx.progress(0.6, f"Recreating container for {instance.server_type} {instance.minecraft_version}", force=True)
    apply_container(instance, ctx, start=was_running or bool(p.get("start")), span=(0.6, 0.97))
    return {"before": before, "after": {k: getattr(instance, k) for k in before}, "backup_id": backup_id}


def _empty_target_folder(instance: Instance, source: AgentClient, target: AgentClient, ctx: JobContext) -> int:
    """Make the server folder on the migration target empty before files are unpacked into it.

    The folder exists there already when an earlier migration failed half-way or the server was
    moved away from that node before: unpacking over it would merge stale files (mods or
    worlds deleted since) into the server. Only the TARGET folder is touched, and only after a
    probe file proves that it is not the source folder (two nodes can point at the same agent
    or share storage). Returns the number of removed top-level entries.
    """
    import secrets as _secrets

    from apps.nodes.client import AgentError

    token = _secrets.token_hex(8)
    probe = f".mcpanel-migrate-probe-{token}"
    source.write_text(instance.id, probe, token, create_only=True)
    try:
        try:
            seen = target.read_text(instance.id, probe).get("content")
        except AgentError as exc:
            if exc.agent_status != 404:
                raise
            seen = None
    finally:
        source.delete_files(instance.id, [probe])
    if seen == token:
        raise ApiError(
            f"Node '{target.node.name}' stores this server in the same folder as its current node (same agent or "
            "shared storage), so there is nothing to move. Nothing was changed.",
            code="same_storage",
            status_code=409,
        )
    if target.status(instance.id).get("exists"):
        ctx.log(f"Removing a leftover container of this server on {target.node.name}")
        target.remove_container(instance.id, timeout=30)
    names = [e["name"] for e in target.list_files(instance.id, "")["entries"]]
    if not names:
        return 0
    ctx.progress(None, f"Removing {len(names)} leftover item(s) of an earlier copy on {target.node.name}", force=True)
    slow = AgentClient(target.node, timeout=3600)  # deleting a big stale world can take a while
    for i in range(0, len(names), 500):
        slow.delete_files(instance.id, names[i : i + 500], recursive=True)
    if target.list_files(instance.id, "")["entries"]:
        raise ApiError(f"Could not empty the server folder on {target.node.name}", code="target_not_empty", status_code=409)
    return len(names)


@register("migrate")
def migrate(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Move a server to another node: stream its folder agent-to-agent, then recreate it there.

    The target folder is emptied first (see ``_empty_target_folder``) so it ends up an exact
    copy of the source. restic backups stay in the source node's repository (they are not
    copied): each backup records the node that holds it (``Backup.meta["node_id"]``), so they
    are no longer offered for in-place restores, retention or verification here, while browse,
    download and "restore as new server" keep using the node that has them
    (``apps.backups.handlers.backup_agent``).
    """
    import secrets as _secrets

    from apps.nodes.models import Node

    from .services import allocate_port, ports_in_use

    instance = _instance(job)
    source = AgentClient(instance.node)
    target_node = Node.objects.get(pk=job.params["target_node"])
    target = AgentClient(target_node)
    if target_node.pk == instance.node_id:
        raise ApiError("The server is already on that node", code="same_node", status_code=409)
    target.info()  # fail fast if the target agent is unreachable or rejects the token
    was_running = ensure_stopped(instance, ctx)

    ctx.progress(0.05, f"Preparing {target_node.name}", force=True)
    prepared = target.prepare(instance.id)
    cleared = 0
    if not prepared.get("created"):
        cleared = _empty_target_folder(instance, source, target, ctx)
    archive = f".mcpanel-migrate-{_secrets.token_hex(6)}.zip"
    ctx.progress(0.1, "Copying server files to the target node (streamed)", force=True)
    chunks, _length, _resp = source.download(instance.id, "", endpoint="archive")
    uploaded = target.upload(instance.id, archive, chunks, overwrite=False)
    ctx.log(f"Transferred {uploaded.get('size', 0)} bytes")
    ctx.progress(0.6, "Unpacking on the target node")
    op = target.extract(instance.id, archive, "", strip="auto", overwrite=True, delete_archive=True)
    target.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.6, 0.25))

    old_node = instance.node
    _tag_backups_on_node(instance, old_node)  # they stay in the old node's restic repository
    port_changed = None
    if instance.game_port in ports_in_use(target_node, exclude=instance):
        new_port = allocate_port(target_node, exclude=instance)
        port_changed = (instance.game_port, new_port)
        instance.game_port = new_port
    instance.node = target_node
    instance.container_spec_hash = ""
    instance.save(update_fields=["node", "game_port", "container_spec_hash"])
    ctx.progress(0.88, f"Creating the container on {target_node.name}")
    apply_container(instance, ctx, start=was_running, span=(0.88, 0.97))

    source.remove_container(instance.id, timeout=30)
    from apps.backups.handlers import repository_node_id
    from apps.backups.models import Backup, BackupStatus

    on_old = [
        b
        for b in Backup.objects.filter(instance=instance, status=BackupStatus.SUCCEEDED)
        if repository_node_id(b, instance) == old_node.pk
    ]
    if job.params.get("delete_source"):
        ctx.log(f"Deleting the server folder and backups on {old_node.name}")
        op = source.delete_data(instance.id)
        source.wait_operation(op["id"], timeout=3600)
        # delete_data removed the old node's repository; backups in other nodes' repositories stay
        Backup.objects.filter(pk__in=[b.pk for b in on_old]).update(status=BackupStatus.DELETED)
    return {
        "from": old_node.name,
        "to": target_node.name,
        "bytes": uploaded.get("size"),
        "port_changed": port_changed,
        "restarted": was_running,
        "target_leftovers_removed": cleared,
        "source_data_deleted": bool(job.params.get("delete_source")),
        "backups_left_on_source": 0 if job.params.get("delete_source") else len(on_old),
    }


def _tag_backups_on_node(instance: Instance, node: Any) -> None:
    """Record ``node`` as the holder of every backup of the server that does not name one yet.

    Called while ``node`` is still the server's node (before a migration switches it), so an
    interrupted migration cannot leave untagged rows that would look local to the new node.
    Rows from versions that only stored ``left_on_node`` (a node name) are resolved by name.
    """
    from apps.backups.models import Backup, BackupStatus
    from apps.nodes.models import Node

    for b in Backup.objects.filter(instance=instance).exclude(status=BackupStatus.DELETED):
        meta = dict(b.meta or {})
        if meta.get("node_id") is not None:
            continue
        legacy = meta.get("left_on_node")
        if legacy:
            node_id = Node.objects.filter(name=legacy).values_list("pk", flat=True).first()
            if node_id is None:
                continue  # that node is gone: the backup stays unreachable
        else:
            node_id = node.pk
            meta["left_on_node"] = node.name
        meta["node_id"] = node_id
        meta.setdefault("node_name", legacy or node.name)
        b.meta = meta
        b.save(update_fields=["meta"])


def _discard(client: AgentClient, instance: Instance, path: str) -> None:
    try:
        client.delete_files(instance.id, [path])
    except ApiError:
        pass  # already gone (the agent operation removes temporary uploads itself)


# Failures after which the world folders may be in an unknown state: never start the server then
# (Minecraft would generate a brand-new world where the old one is missing).
WORLD_STATE_UNKNOWN = {"world_rollback_failed", "operation_lost", "operation_timeout"}


@register("world_replace")
def world_replace(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Replace the world with the one in an uploaded archive: stop, safety backup, swap, start again.

    The agent extracts into a staging folder, moves the old world folders aside, renames the new
    ones into place and deletes the old ones only after that worked; on failure it puts the old
    world back. If the server was running it is started again, also after a clean failure.
    """
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind
    from apps.nodes.client import AgentError

    from . import world

    instance = _instance(job)
    client = AgentClient(instance.node)
    p = job.params
    archive = p["archive"]
    was_running = False
    submitted = False
    safety_id = None
    try:
        level, source = world.level_name(instance, client)
        level = world.validate_level_name(level)
        world_layout = world.layout(instance, client)
        ctx.log(f"World folder: {level} (from {source}); layout: {world_layout}")
        for warning in world.warnings_for(instance):
            ctx.log(f"warning: {warning}")
        if client.status(instance.id).get("running") and not p.get("stop_if_running"):
            raise ConflictError("The server is running. Confirm that it may be stopped to replace the world.", code="must_stop")
        was_running = ensure_stopped(instance, ctx)
        if p.get("pre_restore_backup", True):
            ctx.progress(0.05, "Creating a safety backup of the current state", force=True)
            safety = run_backup(
                instance,
                ctx,
                name=f"Before replacing the world '{level}'",
                kind=BackupKind.PRE_RESTORE,
                user=job.user,
                span=(0.05, 0.45),
                job=job,
                # the uploaded archive sits in the server folder meanwhile; it does not belong in the backup
                excludes=[archive] if p.get("uploaded") else None,
            )
            safety_id = str(safety.id)
        else:
            ctx.log("No safety backup was requested")
        ctx.progress(0.5, f"Replacing the world '{level}'", force=True)
        # Set before the request: if it times out or the connection drops, the agent may have started
        # the swap anyway, so only an error the agent itself reported proves the world is untouched.
        submitted = True
        # a temporary upload is removed by the agent whatever the outcome
        op = client.world_replace(
            instance.id, archive=archive, level_name=level, layout=world_layout, delete_archive=bool(p.get("uploaded"))
        )
        done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.5, 0.4))
    except ApiError as exc:
        # AgentError = the agent answered (a refused request or a failed operation, which rolls back);
        # anything else after submitting (agent unreachable, auth) leaves the outcome unknown.
        intact = not submitted or (isinstance(exc, AgentError) and exc.error_code not in WORLD_STATE_UNKNOWN)
        if not intact:
            _set_desired(instance, DesiredState.STOPPED)
            ctx.log(
                "The world folders may be incomplete, so the server was left stopped. Check the Files page "
                "(a '.mcpanel-world-old-…' folder holds the previous world)"
                + (" or restore the safety backup." if safety_id else ".")
            )
        elif was_running:
            try:
                client.start(instance.id)
                ctx.log("The previous world is unchanged; the server was started again")
            except ApiError as start_exc:
                ctx.log(f"Could not start the server again: {start_exc.detail}")
        raise
    finally:
        if p.get("uploaded"):
            _discard(client, instance, archive)  # the job may have failed before the agent operation
    if p.get("delete_archive") and not p.get("uploaded"):
        _discard(client, instance, archive)  # an archive the user chose from the server folder: only after success
    result = done.get("result") or {}
    for warning in result.get("warnings") or []:
        ctx.log(f"warning: {warning}")
    if result.get("converted"):
        ctx.log(f"Moved the {' and '.join(result['converted'])} from Bukkit-style folders into '{level}'")
    if was_running:
        ctx.progress(0.93, "Starting server")
        try:
            client.start(instance.id)
        except ApiError as exc:
            # the job fails, but say clearly that the world itself was replaced
            raise ApiError(
                f"The world was replaced, but the server could not be started again: {exc.detail}",
                code=exc.error_code,
                status_code=exc.status_code,
            ) from exc
    return {
        "level_name": level,
        "layout": world_layout,
        "installed": result.get("installed", []),
        "replaced": result.get("replaced", []),
        "converted": result.get("converted", []),
        "files": result.get("files"),
        "bytes": result.get("bytes"),
        "warnings": result.get("warnings", []),
        "safety_backup": safety_id,
        "restarted": was_running,
    }


def _log_after_cancel(job: Job, ctx: JobContext, line: str) -> None:
    """Add a last line to the log of a job that was marked as failed while its handler ran."""
    job.refresh_from_db(fields=["log"])  # keep what the abandon request wrote
    ctx.log(line)


def _reset_refused(exc: ApiError) -> bool:
    """Whether a failed agent request proves that the agent did not do what it was asked.

    Only an error the agent itself reported does. A 5xx answer without an agent error code came
    from something in between (a proxy, a crashed request handler), and an unreachable agent
    gave no answer at all: the agent may be resetting the world (or have written the file) anyway.
    """
    from apps.nodes.client import AgentError

    return isinstance(exc, AgentError) and not (exc.agent_status >= 500 and exc.error_code == "agent_error")


@register("world_reset")
def world_reset(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Delete the world so the server generates a new one: stop, safety backup, seed, delete, start again.

    Nothing is deleted unless the folders are still the ones the user confirmed and the safety
    backup (when requested) finished without warnings and holds them. The agent moves the world
    folders aside before deleting them and reports whether a failure touched the world: after a
    failure that left the world as it was, the seed is put back and a server that was running
    is started again; after anything else the server is left stopped, because Minecraft would
    generate a new world wherever the old one is missing.
    """
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind
    from apps.jobs.runner import JobCancelled
    from apps.nodes.client import AgentError

    from . import world

    instance = _instance(job)
    client = AgentClient(instance.node)
    p = job.params
    seed_mode = p.get("seed_mode", "keep")
    keep = list(p.get("keep") or [])
    was_running = False
    submitted = False  # the reset request was sent: only the agent's own answer says what happened
    started = False  # the agent accepted it and runs the operation
    previous_seed: str | None = None
    seed_unconfirmed = False  # the request that sets level-seed is out: only its answer says whether it was written
    safety_id = None
    try:
        if Instance.objects.filter(pk=instance.pk, archived_at__isnull=False).exists():
            raise ConflictError("This server is archived; unarchive it before resetting its world", code="archived")
        plan = world.reset_plan(instance, client, sizes=False)
        world.raise_reset_blocker(plan)
        level, world_layout, folders = plan["level_name"], plan["layout"], plan["delete"]
        if level != p["level_name"] or world_layout != p["layout"] or sorted(folders) != sorted(p["folders"]):
            raise ConflictError(
                f"The world folders changed since this reset was confirmed (they are now: {', '.join(folders)}; "
                f"confirmed: {', '.join(p['folders'])}). Open the dialog again and check what would be deleted.",
                code="world_changed",
            )
        world.check_seed_env(instance, seed_mode)
        ctx.log(
            f"World folder: {level} (from {plan['level_name_source']}); layout: {world_layout}; deleting: {', '.join(folders)}"
        )
        for warning in plan["warnings"]:
            ctx.log(f"warning: {warning}")
        if world.is_live(client.status(instance.id)):
            if not p.get("stop_if_running"):
                raise ConflictError("The server is running. Confirm that it may be stopped to reset the world.", code="must_stop")
            ctx.progress(0.02, "Stopping server", force=True)
            client.stop(instance.id, timeout=instance.stop_timeout)
            reconcile_instance(instance.id)
            was_running = True
        if p.get("pre_restore_backup", True):
            ctx.progress(0.05, "Creating a safety backup of the current state", force=True)
            safety = run_backup(
                instance,
                ctx,
                name=f"Before resetting the world '{level}'",
                kind=BackupKind.PRE_RESTORE,
                user=job.user,
                span=(0.05, 0.6),
                job=job,
                # the server's own backup exclusions could leave the world out of its safety backup
                policy_excludes=False,
            )
            safety_id = str(safety.id)
            if safety.warnings:
                raise ConflictError(
                    "The safety backup finished with warnings (see the log of this job), so it may not hold the whole "
                    "world, and nothing was deleted.",
                    code="safety_backup_incomplete",
                )
            ctx.progress(0.61, "Checking that the safety backup holds the world", force=True)
            world.verify_world_in_backup(
                instance, client, safety.snapshot_id, [f for f in plan["folders"] if f["state"] == "world"]
            )
        else:
            ctx.log("No safety backup was requested")
        try:
            ctx.check_cancelled()
        except JobCancelled:
            _log_after_cancel(
                job,
                ctx,
                "This job was marked as failed before the world was touched: nothing was deleted"
                + (", and the server was left stopped." if was_running else "."),
            )
            raise
        if seed_mode in ("random", "custom"):
            seed = world.validate_seed(p.get("seed", "")) if seed_mode == "custom" else ""
            seed_unconfirmed = True
            previous_seed = world.write_seed(instance, client, seed)
            seed_unconfirmed = False
            if previous_seed is not None:
                ctx.log(
                    "Set level-seed in server.properties" if seed else "Cleared level-seed in server.properties (random seed)"
                )
        ctx.progress(0.65, f"Resetting the world '{level}'", force=True)
        # Set before the request: if it times out or the connection drops, the agent may have started
        # the reset anyway, so only an error the agent itself reported proves the world is untouched.
        submitted = True
        op = client.world_reset(instance.id, level_name=level, layout=world_layout, expect=folders, keep=keep)
        started = True
        done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.65, 0.27))
    except ApiError as exc:
        # Compensation acts on the server, so it needs the lock: a job that was marked as failed
        # meanwhile no longer owns the server and does nothing more.
        try:
            ctx.check_cancelled()
        except JobCancelled:
            _log_after_cancel(
                job,
                ctx,
                f"This job was marked as failed, and then the reset failed ({exc.detail}). Nothing more was done: "
                + (
                    "the server was not started again and server.properties was left as it is. Check the world "
                    "folder before starting the server."
                    if submitted
                    else "the world was not touched" + (", and the server was left stopped." if was_running else ".")
                ),
            )
            raise
        if not submitted:
            untouched = True
        elif not started:
            untouched = _reset_refused(exc)
        else:
            untouched = isinstance(exc, AgentError) and exc.error_code in world.WORLD_RESET_UNTOUCHED
        if not untouched:
            _set_desired(instance, DesiredState.STOPPED)
            ctx.log(
                "The reset may be incomplete, so the server was left stopped and server.properties was left as it is. "
                "Check the Files page: a '.mcpanel-world-old-…' folder may hold the previous world"
                + (", and the safety backup taken by this job can be restored." if safety_id else ".")
            )
            raise
        if previous_seed is not None:
            try:
                world.restore_seed(instance, client, previous_seed)
                ctx.log("Put level-seed in server.properties back to its previous value")
            except ApiError as seed_exc:
                ctx.log(f"Could not put level-seed in server.properties back to its previous value: {seed_exc.detail}")
        elif seed_unconfirmed and not _reset_refused(exc):
            # the previous value is not known here, so there is nothing to put back
            ctx.log(
                "Setting level-seed in server.properties failed without an answer from the agent, so the file may "
                "already hold the new seed. Check level-seed before the server generates a new world."
            )
        if was_running:
            try:
                client.start(instance.id)
                ctx.log("The world is unchanged; the server was started again")
            except ApiError as start_exc:
                ctx.log(f"Could not start the server again: {start_exc.detail}")
        detail = str(exc.detail).removesuffix("Nothing was changed.").rstrip()
        raise ApiError(
            f"The world was not changed. {detail}", code=exc.error_code, status_code=exc.status_code, details=exc.details
        ) from exc
    result = done.get("result") or {}
    deleted = list(result.get("deleted") or [])
    ctx.log(f"Deleted: {', '.join(deleted) or 'nothing'}")
    if result.get("kept"):
        ctx.log(f"Carried over: {', '.join(result['kept'])}")
    if result.get("skipped"):
        ctx.log(f"Left as they are (no world in them): {', '.join(result['skipped'])}")
    for warning in result.get("warnings") or []:
        ctx.log(f"warning: {warning}")
    if was_running:
        try:
            ctx.check_cancelled()
        except JobCancelled:
            _log_after_cancel(
                job, ctx, "The world was reset. This job was marked as failed meanwhile, so the server was left stopped."
            )
            raise
        ctx.progress(0.93, "Starting server")
        try:
            client.start(instance.id)
        except ApiError as exc:
            # the job fails, but say clearly that the world itself was reset
            raise ApiError(
                f"The world was reset, but the server could not be started again: {exc.detail}",
                code=exc.error_code,
                status_code=exc.status_code,
            ) from exc
    return {
        "level_name": level,
        "layout": world_layout,
        "deleted": deleted,
        "kept": result.get("kept", []),
        "skipped": result.get("skipped", []),
        "seed_mode": seed_mode,
        "seed_changed": previous_seed is not None,
        "leftover": result.get("leftover"),
        "warnings": result.get("warnings", []),
        "safety_backup": safety_id,
        "restarted": was_running,
    }
