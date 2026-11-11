"""Job handlers for the instance lifecycle (registered with the job runner)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from django.utils import timezone

from apps import games
from apps.core.errors import ApiError, ConflictError
from apps.jobs.models import Job
from apps.jobs.runner import JobContext, register
from apps.nodes.client import AgentClient, AgentError

from .models import DesiredState, Instance, Status
from .reconcile import reconcile_instance
from .spec import CONTAINER_FORMAT, applied_container_format, build_spec, spec_hash, spec_outdated

log = logging.getLogger(__name__)


def _instance(job: Job) -> Instance:
    inst = Instance.objects.select_related("node").get(pk=job.instance_id)
    return inst


def apply_container(
    instance: Instance,
    ctx: JobContext,
    *,
    start: bool,
    span: tuple[float, float] = (0.1, 0.8),
    admit: bool = True,
) -> dict[str, Any]:
    """(Re)create the container from the current configuration. World data is untouched.

    Central guard for every job that (re)creates or starts a server (start, restart, recreate,
    software and modpack updates, rollbacks, migrations): an archived server is never started,
    even by a job that was queued before it was archived (409 ``archived``), and it gets no new
    container either (archiving removed it; the next start after unarchiving creates it from the
    current configuration). The flag is read from the database because the caller's copy may
    predate the archive.

    A start must fit into the node's memory (``capacity``; 409 ``insufficient_memory``) unless
    the job was asked to overcommit (``allow_overcommit``) or ``admit`` is False: a job that
    stopped the server for its work starts it again without the check.

    A start asks the agent to look for a newer build of the runtime image tag (``pull="newer"``,
    agents with the ``pull_newer`` feature), and the job log says when the image changed.

    A server that runs afterwards is recorded as meant to run (``_started``): the jobs that start
    a server on the way (an update with "start afterwards", a recreate of a running server) do
    not say so themselves.
    """
    if Instance.objects.filter(pk=instance.pk, archived_at__isnull=False).exists():
        if start:
            raise ConflictError("This server is archived; unarchive it before starting it", code="archived")
        ctx.log("The server is archived: its container is created when it is next started")
        return {"skipped": "archived"}
    client = AgentClient(instance.node)
    # a server of another game needs an agent with that game's runtime profile (409 agent_lacks_game)
    games.require_agent_support(instance.game, client, games.for_instance(instance).agent_variant(instance))
    if start and admit:
        admit_memory(instance, ctx, client)
    spec = build_spec(instance)
    write_control_secret(instance, client)
    games.for_instance(instance).before_apply(instance, client)  # panel-owned keys of the game's files
    pull = "newer" if start and client.supports(PULL_NEWER_FEATURE) else "missing"
    ctx.progress(span[0], "Preparing container (pulling the runtime image if needed)", force=True)
    try:
        op = client.apply(instance.id, spec, start=start, pull=pull)
        result = client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(span[0], span[1]))
    except AgentError as exc:
        if not unclaimed_refusal(instance, ctx, exc):
            raise
        # the ports are closed now: the same apply without them (the setup claims and publishes again)
        spec = build_spec(instance)
        op = client.apply(instance.id, spec, start=start, pull=pull)
        result = client.wait_operation(op["id"], timeout=3600, on_progress=ctx.agent_progress(span[0], span[1]))
    done = result.get("result") or {}
    # The hash records how the agent really created the container: one made by an agent that is
    # older than the panel does not match the current hash and keeps reading restart-required.
    applied = applied_container_format(done)
    if applied < CONTAINER_FORMAT:
        ctx.log(
            f"warning: the agent of node '{instance.node.name}' is older than the panel, so this container was "
            "created without what newer versions add (a longer stop time when Docker itself stops the server). "
            "Update the agent and restart the server; until then it keeps showing 'restart required'."
        )
    _log_image_pull(ctx, done)
    instance.container_spec_hash = spec_hash(spec, applied)
    instance.applied_revision = Instance.objects.filter(pk=instance.pk).values_list("config_revision", flat=True).first() or 0
    fields = ["container_spec_hash", "applied_revision"]
    if spec["image"] != instance.image:
        # the runtime image channel changed since the image was stored (spec.current_image)
        ctx.log(f"Runtime image: {spec['image']} (was {instance.image or 'not set'})")
        instance.image = spec["image"]
        fields.append("image")
    instance.save(update_fields=fields)
    container = done.get("container") if isinstance(done.get("container"), dict) else {}
    if start or container.get("running"):  # (the agent also starts the replacement of a container that was running)
        _started(instance)
    return done


def unclaimed_refusal(instance: Instance, ctx: JobContext | None, exc: AgentError) -> bool:
    """The agent refused to start or publish a server that is not claimed (409 ``server_unclaimed``,
    Satisfactory): the definition closes the ports and puts the claim steps back. True when the spec
    changed, so the caller applies again (unpublished)."""
    if exc.error_code != "server_unclaimed":
        return False
    changed = games.for_instance(instance).mark_unclaimed(instance, str(exc.detail))
    if changed and ctx is not None:
        ctx.log(
            f"warning: {exc.detail} The ports stay closed: the server is started unpublished and its setup "
            "(claim, join password, publishing) runs again."
        )
    return changed


def write_control_secret(instance: Instance, client: AgentClient) -> None:
    """Games whose image reads the control secret from a file (Factorio: config/rconpw) get it
    written before every apply, so the server, the spec and the agent's driver agree on it. The
    secret is never logged."""
    definition = games.get_or_none(instance.game)
    path = definition.control_secret_file if definition is not None else None
    if not path:
        return
    # the control secret (Factorio); Satisfactory: its API token, nothing before the claim
    value = definition.control_secret_value(instance)
    if value is None:
        return
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if parent:
        client.mkdir(instance.id, parent, parents=True)
    client.write_text(instance.id, path, value + "\n")


CLONE_LEVEL_NAME_FEATURE = "clone_level_name"  # clone leaves out exactly the world named by level_name / layout
PULL_NEWER_FEATURE = "pull_newer"  # the agent's name for pull="newer" in GET /v1/info ("features")


def _log_image_pull(ctx: JobContext, done: dict[str, Any]) -> None:
    """What an agent with ``pull_newer`` reports about the runtime image of the apply."""
    for warning in done.get("warnings") or []:
        ctx.log(f"warning: {warning}")
    before, now = done.get("image_id_before"), done.get("image_id")
    if done.get("pulled"):
        ctx.log("Downloaded the runtime image from the registry")
    if before and now and before != now:
        ctx.log(f"The runtime image is a newer build now ({str(before)[:19]} before, {str(now)[:19]} now)")
    removed = done.get("images_removed") or []
    if isinstance(removed, list) and removed:
        ctx.log(f"Removed {len(removed)} superseded runtime image(s) that no container uses")


def admit_memory(instance: Instance, ctx: JobContext, client: AgentClient | None = None) -> None:
    """Refuse a start that does not fit into the node's memory (``capacity.admit``), unless the job
    was asked to start it anyway (``allow_overcommit``), which the log then says."""
    from . import capacity

    if (ctx.job.params or {}).get("allow_overcommit"):
        short = capacity.insufficient(instance, client=client)
        if short is not None:
            ctx.log(f"warning: {short.message(instance.name)} It is started anyway, as requested.")
        return
    capacity.admit(instance, client=client)


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


def _started(instance: Instance) -> None:
    """A job has just started the server: from now on it is meant to run.

    Every start by a job ends here (``apply_container`` with a start, ``start_server``, the
    restart of an existing container), whatever the recorded state was before: a software or
    modpack update with "start afterwards" on a stopped server, or a restart during which someone
    pressed Kill (which records "stopped"). A server that runs while it is recorded as "stopped"
    is treated as stopped on purpose when it dies: no "failed", no notification, no stop of a
    crash loop, and its container is removed as that of a stopped server.
    """
    _set_desired(instance, DesiredState.RUNNING)


def _drop_resume_mark(instance: Instance) -> None:
    """Settle the "start again after the host restart" mark this job found, without a decision
    about ``desired_state``: the job ends with the server left stopped on purpose (it was marked
    as failed half-way), so the panel must not start the server by itself afterwards.

    Only the mark the job saw when it began is dropped (``instance`` is the copy loaded then): a
    job that was marked as failed no longer owns the server, and a mark written since then
    belongs to whatever happened after it.
    """
    if not instance.resume_after_boot:
        return
    Instance.objects.filter(pk=instance.pk, resume_after_boot=True, resume_flagged_at=instance.resume_flagged_at).update(
        resume_after_boot=False, resume_flagged_at=None
    )


@register("install")
def install(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Create the data directory, write initial config, create the container and optionally start it."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    definition = games.for_instance(instance)
    # initial config files of the game (Minecraft: server.properties from job.params["server_properties"];
    # other games: their config files from job.params["settings"])
    files = definition.initial_files(instance, job.params or {})
    ctx.progress(0.02, "Creating instance directory", force=True)
    client.prepare(instance.id, files)
    start = bool(job.params.get("start", True))
    if start:
        _set_desired(instance, DesiredState.RUNNING)
    apply_container(instance, ctx, start=start, span=(0.05, 0.9))
    if start:
        ctx.progress(0.95, "Server container started; the first start downloads and installs the server software")
        if definition.retryable_install_exits:
            from .game_handlers import wait_first_start

            try:
                wait_first_start(instance, ctx, client, span=(0.95, 0.04))
            except ConflictError as exc:
                if exc.error_code == "first_start_timeout" and definition.setup_steps:
                    # the setup waits for the server to answer (Satisfactory's claim) and goes on then
                    from .game_handlers import enqueue_setup

                    if enqueue_setup(instance, user=job.user) is not None:
                        ctx.log("Setup steps follow in their own job once the server is ready")
                raise
    if definition.setup_steps:
        from .game_handlers import enqueue_setup

        if enqueue_setup(instance, user=job.user) is not None:
            ctx.log("Setup steps follow in their own job")
    return {"started": start}


def start_server(
    instance: Instance,
    ctx: JobContext,
    *,
    span: tuple[float, float] = (0.1, 0.8),
    apply_changes: bool = False,
    admit: bool = True,
    watch: bool = True,
) -> bool:
    """Start the server, creating its container when there is none. Returns True when it was (re)created.

    ``span`` is (where the job's progress stands, how much of it creating the container may take).

    The one way a job starts a server. A stopped server normally has no container: the
    reconciler removes it once the server is meant to be stopped (``reconcile.maybe_retire``),
    and the agent keeps the record of its last run. So "start" means "create the container from
    the current configuration and start it" whenever there is none, also for the jobs that stop
    a server, work on its files and start it again (restore, rollback, world replace and reset),
    and also when the container disappears between the status request and the start.

    ``apply_changes`` (the Start job): an existing container whose configuration is out of date
    is recreated too. Without it an existing container is started as it is, as those jobs
    always did.

    ``admit``: check that the server fits into the node's memory (see ``apply_container``). The
    jobs that start a server again after they stopped it for their work pass False.

    ``watch``: for games whose image downloads the game at every start (Satisfactory: restart
    policy "no"), wait until it is ready, start it again after SteamCMD's failures and handle the
    start wrapper's refusal (``watch_start``).
    """
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    exists = bool(status.get("exists"))
    # Satisfactory while its claim is checked again: a container made with published ports is never
    # started as it is, also not by the jobs that start a server again after their work
    apply_changes = apply_changes or games.for_instance(instance).must_apply_before_start(instance)
    outdated = exists and apply_changes and (spec_outdated(instance) or not instance.container_spec_hash)
    if exists and not outdated:
        if apply_changes:
            ctx.progress(0.3, "Starting container")
        if admit and not status.get("running"):
            admit_memory(instance, ctx, client)
        try:
            client.start(instance.id)
            _started(instance)
            if watch:
                watch_start(instance, ctx, client)
            return False
        except AgentError as exc:
            if exc.error_code == "server_unclaimed" and unclaimed_refusal(instance, ctx, exc):
                outdated = True  # recreated below without its ports
            elif exc.error_code != "no_container":
                raise
            else:
                # removed between the two requests (a retire that was already on its way)
                status = client.status(instance.id)
                exists = bool(status.get("exists"))
    if outdated:
        ctx.progress(span[0], "Applying configuration")
    else:
        ctx.progress(span[0], "Creating the server's container (a stopped server has none; world data is kept)")
    previous = status if exists else (status.get("last_run") or {})
    done = apply_container(instance, ctx, start=True, span=span, admit=admit)
    try:
        _note_runtime_image(instance, ctx, previous, done)
    except Exception:  # a note only: the server is started
        log.exception("could not compare the runtime image of %s with its last run", instance.pk)
    if watch:
        watch_start(instance, ctx, client)
    return True


def watch_start(instance: Instance, ctx: JobContext, client: AgentClient) -> None:
    """After a start of a game whose image downloads the game at every start (``retryable_install_exits``;
    Satisfactory, restart policy "no"): wait until it is ready, start it again after SteamCMD's failures,
    and handle the start wrapper's refusal (``game_handlers.wait_first_start``). Nothing for other games."""
    if not games.for_instance(instance).retryable_install_exits:
        return
    from .game_handlers import wait_first_start

    instance.refresh_from_db()
    wait_first_start(instance, ctx, client, span=(0.85, 0.1), first=False)


def _note_runtime_image(instance: Instance, ctx: JobContext, previous: dict[str, Any], applied: dict[str, Any]) -> None:
    """Say so when the same image tag now is a different image than the server last ran on.

    A stopped server holds no container, so nothing pins its runtime image: after the image was
    updated or removed and downloaded again, the tag (``stable-java21``) can point to a newer
    build. ``previous`` is the last container (or the record of the last run), ``applied`` the
    result of the apply. Agents that do not report image ids are skipped.
    """
    from apps.audit import service as audit

    current = applied.get("container") if isinstance(applied, dict) else None
    if not isinstance(current, dict) or applied.get("image_id_before"):
        return  # (an agent with pull_newer compared the images itself: _log_image_pull)
    before, now = previous.get("image_id"), current.get("image_id")
    if not before or not now or before == now or previous.get("image") != current.get("image"):
        return
    ctx.log(
        f"note: the runtime image {current.get('image')} is not the build this server last ran on "
        f"({str(before)[:19]} then, {str(now)[:19]} now): the image was updated or downloaded again since."
    )
    audit.record(
        "instance.runtime_image_changed",
        instance=instance,
        target=instance,
        details={"image": current.get("image"), "previous_image_id": before, "image_id": now},
    )


def _queue_setup(instance: Instance, job: Job, ctx: JobContext) -> None:
    """Setup steps that waited for a running server (Satisfactory's claim after its ports were closed)
    go on in their own job."""
    if games.for_instance(instance).setup_steps:
        from .game_handlers import enqueue_setup

        if enqueue_setup(instance, user=job.user) is not None:
            ctx.log("Setup steps follow in their own job")


@register("start")
def start(job: Job, ctx: JobContext) -> dict[str, Any]:
    instance = _instance(job)
    _set_desired(instance, DesiredState.RUNNING)
    # (Satisfactory: start_server waits until it is ready, retries SteamCMD's failures and handles the
    # start wrapper's refusal: restart policy "no")
    start_server(instance, ctx, apply_changes=True)
    if games.for_instance(instance).setup_steps:
        # setup steps that waited for a running server (Satisfactory's claim) go on now
        from .game_handlers import enqueue_setup

        if enqueue_setup(instance, user=job.user) is not None:
            ctx.log("Setup steps follow in their own job")
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
    """Say in the job log what the agent's graceful stop did (agents before this field send none).

    ``stop.exit_code`` is the exit code of the run the stop ended (none when nothing was running).
    After a restart the container status in the answer describes the new run, so the status'
    own ``exit_code`` is never used here.
    """
    info = answer.get("stop") if isinstance(answer, dict) else None
    if not isinstance(info, dict):
        return
    if info.get("flushed"):
        ctx.log(f"The world was saved ({_save_command(ctx)}) before the server was stopped")
    if info.get("stop_sent_via_rcon"):
        ctx.log("The server was still starting; the stop was delivered as soon as it accepted commands")
    for warning in info.get("warnings") or []:
        ctx.log(f"warning: {warning}")
    exit_code = info.get("exit_code")
    if exit_code not in (0, None, 137) and exit_code in _clean_exit_codes(ctx):
        return  # e.g. Terraria's image: 143 after its saving SIGTERM handler
    if exit_code == 137:
        ctx.log("warning: the server did not stop in time and was killed (exit code 137); its last changes may not be saved")
    elif exit_code not in (0, None):
        # e.g. 255: the server runner gave up waiting for the server to stop and ended it
        ctx.log(
            f"warning: the server did not shut down cleanly (exit code {exit_code}): it was ended when its time to stop "
            "ran out, or it failed while stopping. Its last changes may not be saved; see the end of the console log"
        )


def _clean_exit_codes(ctx: JobContext) -> frozenset[int]:
    """Exit codes of a clean stop of the job's server (0; Terraria's image also 143)."""
    try:
        game = Instance.objects.filter(pk=ctx.job.instance_id).values_list("game", flat=True).first()
    except Exception:  # the log line is a note only
        game = None
    definition = games.get_or_none(game)
    return definition.clean_exit_codes if definition is not None else frozenset({0})


def _save_command(ctx: JobContext) -> str:
    """The command the agent's driver saves with before a stop (Minecraft: save-all flush)."""
    try:
        game = Instance.objects.filter(pk=ctx.job.instance_id).values_list("game", flat=True).first()
    except Exception:  # the log line is a note only
        game = None
    definition = games.get_or_none(game)
    if definition is None or definition.id == games.DEFAULT_GAME or not definition.flush_command:
        return "save-all flush"
    return definition.flush_command


@register("stop")
def stop(job: Job, ctx: JobContext) -> dict[str, Any]:
    if (skipped := _warn_players_first(job, ctx, "stop")) is not None:
        return skipped
    instance = _instance(job)
    _set_desired(instance, DesiredState.STOPPED)
    ctx.progress(0.2, f"Stopping server gracefully (up to {instance.stop_timeout}s)", force=True)
    status = AgentClient(instance.node).status(instance.id)
    if status.get("exists"):
        try:
            _log_stop(ctx, AgentClient(instance.node).stop(instance.id, timeout=instance.stop_timeout))
        except AgentError as exc:
            if exc.error_code != "no_container":
                raise
            # it was not running and its container was removed between the two requests: stopped
    return {}


@register("restart")
def restart(job: Job, ctx: JobContext) -> dict[str, Any]:
    if (skipped := _warn_players_first(job, ctx, "restart")) is not None:
        return skipped
    instance = _instance(job)  # (re)loaded after the countdown: settings may have changed
    client = AgentClient(instance.node)
    _set_desired(instance, DesiredState.RUNNING)
    status = client.status(instance.id)
    if not status.get("exists") or spec_outdated(instance):
        if status.get("exists"):
            # changed settings, or a container made by an older release or agent (spec.CONTAINER_FORMAT)
            ctx.progress(
                0.1, "Recreating the container to apply changed settings or a newer MCPanel version (world data is kept)"
            )
        else:
            ctx.progress(0.1, "The server has no container; creating it (world data is kept)")
        _recreate(job, instance, ctx, client, running=bool(status.get("running")), start=True, span=(0.2, 0.9))
        watch_start(instance, ctx, client)
    else:
        ctx.progress(0.2, "Restarting server", force=True)
        try:
            _log_stop(ctx, client.restart(instance.id, timeout=instance.stop_timeout))
            _started(instance)  # also after a Kill that came in while the server was stopping
            watch_start(instance, ctx, client)
        except AgentError as exc:
            if exc.error_code == "server_unclaimed" and unclaimed_refusal(instance, ctx, exc):
                ctx.progress(0.2, "Recreating the container without its ports (the claim is checked again)")
                apply_container(instance, ctx, start=True, span=(0.2, 0.9))
                watch_start(instance, ctx, client)
                _queue_setup(instance, job, ctx)
                return {}
            if exc.error_code != "no_container":
                raise
            # the server was stopped and its container was removed between the two requests
            ctx.progress(0.2, "The server has no container; creating it (world data is kept)")
            apply_container(instance, ctx, start=True, span=(0.2, 0.9))
            watch_start(instance, ctx, client)
    _queue_setup(instance, job, ctx)
    return {}


def _recreate(
    job: Job,
    instance: Instance,
    ctx: JobContext,
    client: AgentClient,
    *,
    running: bool,
    start: bool,
    span: tuple[float, float],
) -> None:
    """Stop a running server and create its container from the current configuration.

    The memory check comes before the stop. After a failure that left the old container as it
    was (the agent refused the configuration or could not get the image) a server that was
    running is started again from it; after a later failure it is recorded as stopped.
    """
    if start:
        admit_memory(instance, ctx, client)
    if running:
        ctx.remember(stopped_server=True)  # cut off from here on: the watchdog records it stopped
        _log_stop(ctx, client.stop(instance.id, timeout=instance.stop_timeout))
    try:
        if running and start:
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=span, admit=False))
        else:
            apply_container(instance, ctx, start=start, span=span, admit=False)
    except Exception as exc:
        put_back(job, instance, ctx, exc, was_running=running, untouched=_apply_refused(exc, instance))
        raise


@register("recreate")
def recreate(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Recreate the container from the current configuration without touching world data."""
    instance = _instance(job)
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    was_running = bool(status.get("running"))
    if was_running:
        ctx.progress(0.05, "Stopping server", force=True)
    _recreate(
        job,
        instance,
        ctx,
        client,
        running=was_running,
        start=was_running or bool(job.params.get("start", False)),
        span=(0.1, 0.95),
    )
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
    # The server is gone even when something could not be removed (e.g. a btrfs snapshot): what
    # stayed on the node's disk is in the job's result as well as in its log.
    agent_result = done.get("result") or {}
    warnings = [str(w) for w in agent_result.get("warnings") or []]
    leftovers = [str(path) for path in agent_result.get("leftovers") or []]
    for warning in warnings:
        ctx.log(f"Warning: {warning}")
    if leftovers:
        ctx.log(f"Still on the disk of node '{instance.node.name}': {', '.join(leftovers)}")
    name = instance.name
    instance_id = str(instance.id)
    # detach the job from the instance before deleting so the job history survives
    Job.objects.filter(instance=instance).update(instance=None)
    job.instance_id = None
    instance.delete()
    return {"deleted": instance_id, "name": name, "warnings": warnings, "leftovers": leftovers}


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
    exclude_worlds = bool(job.params.get("exclude_worlds"))
    level = world_layout = None
    other_game = (source.game or games.DEFAULT_GAME) != games.DEFAULT_GAME
    world_folders = games.for_instance(source).world_folders() if exclude_worlds and other_game else ()
    if other_game:
        # the agent's world exclusion knows Minecraft's folders only: the game's save folders are
        # removed from the copy below
        exclude_worlds = False
    if exclude_worlds:
        # which folders hold the worlds: the agent leaves out these (older agents: the default names)
        from . import world

        level = world.validate_level_name(world.level_name(source, client)[0])
        world_layout = world.layout(source, client)
        ctx.log(f"Leaving out the worlds: {level} (layout: {world_layout})")
        if not client.supports(CLONE_LEVEL_NAME_FEATURE):
            if level != "world":
                ctx.log(
                    f"Warning: the node agent is older and leaves out only the folders world, world_nether and "
                    f"world_the_end, not '{level}': the clone gets a copy of this world. Update the agent, or delete "
                    "the world folders of the clone on its Files page."
                )
            level = world_layout = None  # an older agent does not know these fields
    op = client.clone(source.id, target.id, exclude_worlds=exclude_worlds, level_name=level, layout=world_layout)
    client.wait_operation(op["id"], timeout=7200, on_progress=ctx.agent_progress(0.05, 0.75))
    if world_folders:
        ctx.log(f"Leaving out the saves: {', '.join(world_folders)}")
        for folder in world_folders:
            try:
                AgentClient(target.node).delete_files(target.id, [folder], True, timeout=600)
            except ApiError as exc:
                if exc.error_code != "not_found":
                    raise
    target.installed_at = source.installed_at
    target.save(update_fields=["installed_at"])
    ctx.progress(0.85, "Creating container for the clone")
    apply_container(target, ctx, start=False, span=(0.85, 0.98))
    return {"target_id": str(target.id)}


def ensure_stopped(instance: Instance, ctx: JobContext) -> bool:
    """Stop the server if running. Returns True if it was running.

    The job remembers that it stopped the server (``Job.result["stopped_server"]``): when it is
    cut off, the watchdog says so and records the server as stopped (``jobs.runner.settle_interrupted``).
    """
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    if status.get("running"):
        ctx.progress(None, "Stopping server", force=True)
        ctx.remember(stopped_server=True)
        _log_stop(ctx, client.stop(instance.id, timeout=instance.stop_timeout))
        reconcile_instance(instance.id)
        return True
    return False


class Progress:
    """How far a job that stopped a server for its work got, for ``put_back`` after a failure.

    ``safe`` nothing was changed yet; ``requested`` the request for the step that changes the
    server was sent (only a refusal by the agent proves that nothing happened); ``running`` the
    agent accepted it (only a failure code of ``untouched_codes`` proves that nothing happened);
    ``changed`` the server's files or configuration were changed.
    """

    def __init__(self, untouched_codes: frozenset[str] = frozenset()) -> None:
        self.stage = "safe"
        self.untouched_codes = untouched_codes

    def requested(self) -> None:
        self.stage = "requested"

    def running(self) -> None:
        self.stage = "running"

    def changed(self) -> None:
        self.stage = "changed"

    def untouched(self, exc: BaseException) -> bool:
        if self.stage == "safe":
            return True
        if self.stage == "requested":
            return isinstance(exc, ApiError) and _reset_refused(exc)
        if self.stage == "running":
            return isinstance(exc, AgentError) and exc.error_code in self.untouched_codes
        return False


def put_back(job: Job, instance: Instance, ctx: JobContext, exc: BaseException, *, was_running: bool, untouched: bool) -> None:
    """After a failure of a job that stopped a running server for its work: start it again when
    the failure provably left everything as it was, else record it as stopped and say why.

    A job that was marked as failed or cancelled meanwhile no longer owns the server: it is left
    as it is (and a pending "start again after the host restart" mark is dropped). When the worker
    is being stopped nothing is started either: the server is recorded as stopped, unless the
    worker stopped while the node was starting it again (``starting_again``).
    """
    from apps.jobs.runner import JobCancelled, WorkerStopping

    if not was_running:
        return
    if isinstance(exc, WorkerStopping):
        if (ctx.job.result or {}).get("stopped_server") is False:
            return  # cut off while the node started it again (``starting_again``): the node finishes that start
        _set_desired(instance, DesiredState.STOPPED)
        ctx.log(WORKER_STOPPED_LEFT_STOPPED)
        return
    if isinstance(exc, JobCancelled):
        _drop_resume_mark(instance)
        _log_after_cancel(job, ctx, "This job was marked as failed; the server it had stopped was left stopped.")
        return
    if not untouched:
        _set_desired(instance, DesiredState.STOPPED)
        ctx.log(
            "This step may have changed the server's files or configuration before it failed, so the server was left "
            "stopped. Check it (the Files page, the job log) before starting it."
        )
        return
    try:
        ctx.check_cancelled()
    except JobCancelled as cancelled:
        put_back(job, instance, ctx, cancelled, was_running=True, untouched=True)
        return
    try:
        starting_again(ctx, lambda: start_server(instance, ctx, span=(0.93, 0.06), admit=False))
        ctx.log("Nothing was changed; the server was started again")
    except ApiError as start_exc:
        ctx.log(f"Nothing was changed, but the server could not be started again: {start_exc.detail}")


def start_again(
    job: Job,
    instance: Instance,
    ctx: JobContext,
    *,
    done: str,
    span: tuple[float, float] = (0.92, 0.07),
    recreate: bool = False,
) -> None:
    """Start a server again that this job stopped and whose work is done (``done`` says what
    happened); ``recreate``: from a new container (its software changed). A job that was marked
    as failed meanwhile leaves it stopped."""
    from apps.jobs.runner import JobCancelled, WorkerStopping

    try:
        ctx.check_cancelled()
    except WorkerStopping:
        # interrupted, not marked as failed: the runner fails it as interrupted and records the server stopped
        ctx.log(f"{done} {WORKER_STOPPED_LEFT_STOPPED}")
        raise
    except JobCancelled:
        _drop_resume_mark(instance)
        _log_after_cancel(job, ctx, f"{done} This job was marked as failed meanwhile, so the server was left stopped.")
        raise
    ctx.progress(span[0], "Recreating the container and starting the server" if recreate else "Starting server")
    try:
        if recreate:
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=span, admit=False))
        else:
            starting_again(ctx, lambda: start_server(instance, ctx, span=span, admit=False))
    except ApiError as exc:
        raise ApiError(
            f"{done} But the server could not be started again: {exc.detail}",
            code=exc.error_code,
            status_code=exc.status_code,
        ) from exc
    _continue_setup(job, instance, ctx)


def _continue_setup(job: Job, instance: Instance, ctx: JobContext) -> None:
    """Setup steps that became pending through this job's work (Satisfactory after a restore: the
    claim is checked again before the ports are published) go on now that the server runs."""
    if not games.for_instance(instance).setup_steps:
        return
    from .game_handlers import enqueue_setup

    try:
        if enqueue_setup(instance, user=job.user) is not None:
            ctx.log("Setup steps follow in their own job")
    except ApiError as exc:
        ctx.log(f"warning: the setup steps could not be queued: {exc.detail}")


WORKER_STOPPED_LEFT_STOPPED = (
    "The panel was being stopped, so the server this job had stopped was left stopped. Start it when you want it back."
)


def starting_again(ctx: JobContext, start: Callable[[], Any]) -> Any:
    """Run the step that starts a server again that this job stopped for its work.

    From then on the job no longer counts as having stopped the server (``stopped_server``):
    when the worker is stopped while the node's agent works on that start (the panel stops
    waiting, ``WorkerStopping``), the agent finishes it, so the interrupted job must not record
    the server as stopped (``jobs.runner.settle_interrupted``).
    """
    from apps.jobs.runner import WorkerStopping

    try:
        result = start()
    except WorkerStopping:
        ctx.remember(stopped_server=False)
        ctx.log("The panel was being stopped while the node started the server again; the node finishes that start.")
        raise
    ctx.remember(stopped_server=False)
    return result


def require_not_running(instance: Instance) -> None:
    status = AgentClient(instance.node).status(instance.id)
    if status.get("running"):
        raise ApiError("The server must be stopped for this operation", code="must_stop", status_code=409)


# Name prefix of the archive ImportArchiveView uploads into the server folder (agent backups exclude it).
IMPORT_ARCHIVE_PREFIX = ".mcpanel-import-"


def drop_import_archive(client: AgentClient, instance: Instance, path: str) -> None:
    """Delete the panel's own uploaded import archive (never an archive the user put there)."""
    if not path.rsplit("/", 1)[-1].startswith(IMPORT_ARCHIVE_PREFIX):
        return
    try:
        client.delete_files(instance.id, [path])
    except ApiError as exc:
        log.warning("could not delete the import archive %s of %s: %s", path, instance.pk, exc)


@register("extract_archive")
def extract_archive(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Safely extract an uploaded archive inside the instance (import server / world / server pack).

    The agent deletes the archive (``delete_archive``) once its operation ran, also when the
    extraction failed. When the import is refused before the operation starts (the server is
    running: ``must_stop``, or another refusal), the panel deletes its own uploaded archive.

    No entry may write a file with the server's secret (``secret_files.extract_protected``:
    Factorio's ``config/rconpw``, Satisfactory's ``.mcpanel/`` and its claim): the agent skips them
    (feature ``extract_protected``; an older agent is refused for those games, 409
    ``agent_outdated``) and the log says how many it skipped.
    """
    from apps.nodes.client import AgentTimeout, AgentUnavailable

    from . import secret_files

    instance = _instance(job)
    client = AgentClient(instance.node)
    p = job.params
    try:
        require_not_running(instance)
        secret_files.require_extract_destination(instance, p.get("destination", ""))
        protected = secret_files.require_extract_support(instance, client)
        ctx.progress(0.02, f"Extracting {p['path']}", force=True)
        op = client.extract(
            instance.id,
            p["path"],
            p.get("destination", ""),
            strip=p.get("strip", "auto"),
            overwrite=bool(p.get("overwrite")),
            delete_archive=bool(p.get("delete_archive")),
            **({"protected": protected} if protected else {}),  # (Minecraft: the request as before)
        )
    except (AgentTimeout, AgentUnavailable):
        raise  # the agent may have started the operation (it deletes the archive), or cannot be reached
    except ApiError:
        if p.get("delete_archive"):
            drop_import_archive(client, instance, p["path"])
        raise
    done = client.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.02, 0.95))
    result = done.get("result") or {}
    if result.get("existing_count"):
        ctx.log(f"{result['existing_count']} existing file(s) were kept (overwrite was off)")
    if result.get("skipped_count"):
        ctx.log(f"{result['skipped_count']} unsafe entries skipped (links, special files or unsafe paths)")
    if result.get("protected_count"):
        names = ", ".join(str(n)[:200] for n in (result.get("protected") or [])[:10])
        ctx.log(
            f"{result['protected_count']} entr{'y was' if result['protected_count'] == 1 else 'ies were'} not "
            f"extracted: the panel manages these files of the server and keeps its own ({names})"
        )
    if p.get("mark_installed") and instance.installed_at is None:
        from django.utils import timezone

        instance.installed_at = timezone.now()
        instance.save(update_fields=["installed_at"])
    return result


# The fields that say which software a server runs: recorded with every backup
# (``apps.backups.handlers.run_backup``, ``Backup.meta["software"]``) and put back with its files.
SOFTWARE_FIELDS = (
    "server_type",
    "minecraft_version",
    "loader_version",
    "java_version",
    "image",
    "modpack_provider",
    "modpack_project_id",
    "modpack_slug",
    "modpack_name",
    "modpack_version_id",
    "modpack_version_name",
    "modpack_loader",
)


def software_state(instance: Instance) -> dict[str, Any]:
    """The software record of a backup: ``game`` plus the game's fields (Minecraft: SOFTWARE_FIELDS)."""
    return games.for_instance(instance).software_state(instance)


def set_software(instance: Instance, state: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Set the software fields of ``state`` (others are left alone); returns {field: (old, new)} of
    what changed. The image follows the Java version (``spec.image_for``); a change counts as a
    new configuration (``bump_revision``)."""
    from .services import bump_revision
    from .spec import image_for

    if (instance.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
        return games.for_instance(instance).set_software(instance, state)
    wanted = {f: state[f] for f in SOFTWARE_FIELDS if f in state and f != "image"}
    if "java_version" in wanted:
        wanted["java_version"] = int(wanted["java_version"])
    changes = {f: (getattr(instance, f), v) for f, v in wanted.items() if getattr(instance, f) != v}
    if not changes:
        return {}
    for f, (_old, new) in changes.items():
        setattr(instance, f, new)
    instance.image = image_for(instance.java_version)
    instance.save(update_fields=[*changes, "image"])
    bump_revision(instance)
    return changes


def describe_software(state: dict[str, Any]) -> str:
    """The software of a record as text, e.g. "Paper 1.21.1 (loader 130) on Java 21" (no ``game``: Minecraft)."""
    definition = games.get_or_none(state.get("game")) or games.get(games.DEFAULT_GAME)
    return definition.describe_software(state)


def describe_minecraft_software(state: dict[str, Any]) -> str:
    parts = [str(state.get("server_type") or "").title(), str(state.get("minecraft_version") or "")]
    if state.get("modpack_version_name") or state.get("modpack_version_id"):
        parts.append(f"(pack {state.get('modpack_version_name') or state.get('modpack_version_id')})")
    elif state.get("loader_version"):
        parts.append(f"(loader {state['loader_version']})")
    parts.append(f"on Java {state.get('java_version')}")
    return " ".join(p for p in parts if p)


@register("software_update")
def software_update(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Change Minecraft/loader/Java versions of a standard server, protected by a pre-update backup.

    After a failure that changed nothing (the backup failed, the agent refused the new
    configuration or could not get its image) the versions are put back and a server that was
    running is started again.
    """
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind

    from .spec import image_for

    instance = _instance(job)
    p = job.params
    if (instance.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
        return _game_software_update(job, ctx, instance)
    before = {
        "minecraft_version": instance.minecraft_version,
        "loader_version": instance.loader_version,
        "java_version": instance.java_version,
        "image": instance.image,
    }
    was_running = False
    backup_id = None
    try:
        was_running = ensure_stopped(instance, ctx)
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
        ctx.check_cancelled()
        instance.minecraft_version = p["minecraft_version"]
        instance.loader_version = p.get("loader_version", "")
        instance.java_version = int(p["java_version"])
        instance.image = image_for(instance.java_version)
        instance.save(update_fields=["minecraft_version", "loader_version", "java_version", "image"])
        from .services import bump_revision

        bump_revision(instance)
        instance.refresh_from_db()
        ctx.progress(0.6, f"Recreating container for {instance.server_type} {instance.minecraft_version}", force=True)
        if was_running:
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=(0.6, 0.97), admit=False))
        else:
            apply_container(instance, ctx, start=bool(p.get("start")), span=(0.6, 0.97))
    except Exception as exc:
        versions = (instance.minecraft_version, instance.loader_version, instance.java_version)
        if versions == (before["minecraft_version"], before["loader_version"], before["java_version"]):
            untouched = True  # failed before the versions were changed (the stop, the backup)
        else:
            untouched = _apply_refused(exc, instance)
            if untouched:
                set_software(instance, before)
                ctx.log("The update was not applied: the previous versions were put back")
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=untouched)
        raise
    return {"before": before, "after": {k: getattr(instance, k) for k in before}, "backup_id": backup_id}


def _game_software_update(job: Job, ctx: JobContext, instance: Instance) -> dict[str, Any]:
    """Software update of a game other than Minecraft: the exact version (and so the image tag)
    changes, protected by a pre-update backup; after a failure that changed nothing the version
    is put back and a server that was running is started again."""
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind

    definition = games.for_instance(instance)
    p = job.params
    before = definition.software_state(instance)
    target = str(p["version"])
    was_running = False
    backup_id = None
    try:
        was_running = ensure_stopped(instance, ctx)
        if p.get("backup", True):
            ctx.progress(0.05, "Creating a pre-update backup")
            backup = run_backup(
                instance,
                ctx,
                name=f"Before update to {definition.label} {target}",
                kind=BackupKind.PRE_UPDATE,
                user=job.user,
                span=(0.05, 0.55),
                job=job,
            )
            backup.meta = {**(backup.meta or {}), "software_before": before}
            backup.save(update_fields=["meta"])
            backup_id = str(backup.id)
        ctx.check_cancelled()
        definition.set_software(instance, {"version": target})
        instance.refresh_from_db()
        ctx.progress(
            0.6, f"Recreating container for {definition.describe_software(definition.software_state(instance))}", force=True
        )
        if was_running:
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=(0.6, 0.97), admit=False))
        else:
            apply_container(instance, ctx, start=bool(p.get("start")), span=(0.6, 0.97))
    except Exception as exc:
        untouched = instance.minecraft_version == before.get("version")
        if not untouched:
            untouched = _apply_refused(exc, instance)
            if untouched:
                definition.set_software(instance, before)
                ctx.log("The update was not applied: the previous version was put back")
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=untouched)
        raise
    return {"before": before, "after": definition.software_state(instance), "backup_id": backup_id}


# Failures of an apply that prove the old container was not touched: the agent refused the
# configuration when it was sent, or could not get the image (it pulls before it removes anything).
# cpu_limit: the agent checks the limit against the host's CPUs before it touches a container.
APPLY_UNTOUCHED_CODES = frozenset({"pull_failed", "image_missing", "insufficient_memory", "archived", "cpu_limit"})
# create_failed: Docker refused to create the new container. Agents with ``pull_newer`` (this
# release) create it before they remove the old one (a failed rename after the removal is
# ``rename_failed``, never listed); older agents remove the old container first, so for them it
# proves nothing.
APPLY_UNTOUCHED_WITH_CREATE_FIRST = frozenset({"create_failed"})


def _apply_refused(exc: BaseException, instance: Instance) -> bool:
    """Whether a failed ``apply_container`` provably left the server's container as it was."""
    if isinstance(exc, AgentError):
        # a refusal of the request itself (4xx), or a failed operation that stopped before the old container
        if exc.agent_status < 500 or exc.error_code in APPLY_UNTOUCHED_CODES:
            return True
        return exc.error_code in APPLY_UNTOUCHED_WITH_CREATE_FIRST and AgentClient(instance.node).supports(PULL_NEWER_FEATURE)
    return isinstance(exc, ConflictError) and exc.error_code in APPLY_UNTOUCHED_CODES  # the panel's own refusals


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
    # (again: the view checked it) a game other than Minecraft needs its runtime profile on the target
    games.require_agent_support(instance.game, target, games.for_instance(instance).agent_variant(instance))
    if source.status(instance.id).get("running"):
        # it is started on the target node afterwards, where it does not count yet: check before
        # anything is stopped or copied (and again when its container is created there)
        from . import capacity

        admit_memory(capacity.on_node(instance, target_node), ctx, target)
    was_running = False
    moved = False  # the server's record points at the target node now
    try:
        was_running = ensure_stopped(instance, ctx)

        ctx.progress(0.05, f"Preparing {target_node.name}", force=True)
        prepared = target.prepare(instance.id)
        cleared = 0
        if not prepared.get("created"):
            cleared = _empty_target_folder(instance, source, target, ctx)
        # a record of a run this server had on the target node before it was moved away
        _forget_last_run(target, instance, ctx)
        archive = f".mcpanel-migrate-{_secrets.token_hex(6)}.zip"
        ctx.progress(0.1, "Copying server files to the target node (streamed)", force=True)
        chunks, _length, _resp = source.download(instance.id, "", endpoint="archive")
        uploaded = target.upload(instance.id, archive, chunks, overwrite=False)
        ctx.log(f"Transferred {uploaded.get('size', 0)} bytes")
        ctx.progress(0.6, "Unpacking on the target node")
        op = target.extract(instance.id, archive, "", strip="auto", overwrite=True, delete_archive=True)
        target.wait_operation(op["id"], timeout=6 * 3600, on_progress=ctx.agent_progress(0.6, 0.25))
        ctx.check_cancelled()

        old_node = instance.node
        _tag_backups_on_node(instance, old_node)  # they stay in the old node's restic repository
        port_changed = None
        fields = ["node", "game_port", "container_spec_hash"]
        if instance.ports and (instance.game or games.DEFAULT_GAME) != games.DEFAULT_GAME:
            # named ports: each keeps its number when it is free there for its protocol
            from .services import assign_named_ports

            ports = assign_named_ports(games.for_instance(instance), target_node, exclude=instance, current=instance.ports)
            if ports != instance.ports:
                port_changed = (instance.game_port, int(ports[0]["host_port"]))
                instance.ports = ports
                instance.game_port = int(ports[0]["host_port"])
                fields.append("ports")
        elif instance.game_port in ports_in_use(target_node, exclude=instance):
            new_port = allocate_port(target_node, exclude=instance)
            port_changed = (instance.game_port, new_port)
            instance.game_port = new_port
        instance.node = target_node
        instance.container_spec_hash = ""
        instance.save(update_fields=fields)
        moved = True
        ctx.progress(0.88, f"Creating the container on {target_node.name}")
        # (memory is checked: the server did not count on the target node before)
        if was_running:
            starting_again(ctx, lambda: apply_container(instance, ctx, start=True, span=(0.88, 0.97)))
        else:
            apply_container(instance, ctx, start=False, span=(0.88, 0.97))
    except Exception as exc:
        # Before the switch the server on its own node is as it was: only the target's copy is
        # incomplete. After it, the server lives on the target node, whose container may be missing.
        put_back(job, instance, ctx, exc, was_running=was_running, untouched=not moved)
        raise

    source.remove_container(instance.id, timeout=30)
    # the record of the last run does not move with the server and must not stay behind
    _forget_last_run(source, instance, ctx)
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


def _forget_last_run(client: AgentClient, instance: Instance, ctx: JobContext) -> None:
    """Delete the record an agent keeps of the server's last run (console output included) on a
    node the server is leaving or returning to. Agents without the feature keep no record."""
    if not client.supports("retire"):
        return
    try:
        client.forget_last_run(instance.id)
    except ApiError as exc:
        ctx.log(
            f"warning: could not delete the saved console output of the last run on {client.node.name}: {exc.detail}. "
            "It is removed when the server is deleted on that node."
        )


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
                starting_again(ctx, lambda: start_server(instance, ctx, span=(0.93, 0.06), admit=False))
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
            starting_again(ctx, lambda: start_server(instance, ctx, span=(0.93, 0.06), admit=False))
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
    from apps.jobs.runner import JobCancelled, WorkerStopping
    from apps.nodes.client import AgentError

    from . import world

    instance = _instance(job)
    client = AgentClient(instance.node)
    p = job.params
    seed_mode = p.get("seed_mode", "keep")
    keep = list(p.get("keep") or [])
    was_running = False  # the server ran and this job asked the agent to stop it
    stopped = False  # the agent answered that stop
    submitted = False  # the reset request was sent: only the agent's own answer says what happened
    started = False  # the agent accepted it and runs the operation
    previous_seed: str | None = None
    seed_unconfirmed = False  # the request that sets level-seed is out: only its answer says whether it was written
    safety_id = None

    def still_owner(last_line: str) -> None:
        """Stop here (JobCancelled) when the job was marked as failed meanwhile: it no longer owns
        the server, so it does nothing more to it. The server is then left as it is on purpose,
        which also settles a pending "start again after the host restart" mark."""
        try:
            ctx.check_cancelled()
        except WorkerStopping:
            # interrupted, not marked as failed: the runner fails the job as interrupted and records
            # a server this job stopped as stopped (``settle_interrupted``)
            ctx.log(
                "The panel was being stopped, so this job ended here"
                + (". The server it had stopped was left stopped." if was_running else ".")
            )
            raise
        except JobCancelled:
            _drop_resume_mark(instance)
            _log_after_cancel(job, ctx, last_line)
            raise

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
        # checked before every step that acts on the server: the stop, the backup, the reset, the start
        still_owner("This job was marked as failed before it did anything: nothing was changed.")
        if world.is_live(client.status(instance.id)):
            if not p.get("stop_if_running"):
                raise ConflictError("The server is running. Confirm that it may be stopped to reset the world.", code="must_stop")
            ctx.progress(0.02, "Stopping server", force=True)
            # Set before the request: if its answer is lost the agent may have stopped the server
            # anyway, and a failure that left the world untouched has to start it again.
            was_running = True
            ctx.remember(stopped_server=True)  # cut off from here on: the watchdog records it stopped
            answer = client.stop(instance.id, timeout=instance.stop_timeout)
            stopped = True
            _log_stop(ctx, answer)
            reconcile_instance(instance.id)
        if p.get("pre_restore_backup", True):
            still_owner(
                "This job was marked as failed before the safety backup was taken: nothing was deleted"
                + (", and the server was left stopped." if was_running else ".")
            )
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
        still_owner(
            "This job was marked as failed before the world was touched: nothing was deleted"
            + (", and the server was left stopped." if was_running else ".")
        )
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
        still_owner(
            f"This job was marked as failed, and then the reset failed ({exc.detail}). Nothing more was done: "
            + (
                "the server was not started again and server.properties was left as it is. Check the world "
                "folder before starting the server."
                if submitted
                else "the world was not touched"
                + (
                    ", and the server was left stopped."
                    if stopped
                    else ", and the server may have been stopped: check it."
                    if was_running
                    else "."
                )
            )
        )
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
                # the stop itself failed (nothing else runs before ``stopped``): the server may be
                # stopped (its answer was lost) or still running (the agent refused)
                if stopped or not world.is_live(client.status(instance.id)):
                    starting_again(ctx, lambda: start_server(instance, ctx, span=(0.93, 0.06), admit=False))
                    ctx.log("The world is unchanged; the server was started again")
                elif isinstance(exc, AgentError):
                    ctx.log("The world is unchanged; the server was not stopped and is still running")
                else:
                    ctx.log(
                        "The world is unchanged. The stop got no answer and the server is running at the moment; "
                        "if it stops after all, start it again"
                    )
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
        still_owner("The world was reset. This job was marked as failed meanwhile, so the server was left stopped.")
        ctx.progress(0.93, "Starting server")
        try:
            starting_again(ctx, lambda: start_server(instance, ctx, span=(0.93, 0.06), admit=False))
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
