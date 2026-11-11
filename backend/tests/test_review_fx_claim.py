"""Satisfactory after the real-container gate of 2026-11-11 (items 1, 2, 5 and 7 of its review), on the panel.

* 1: the claim marker: ``mark_unclaimed`` asks the agent to remove it, the publish step has the agent
  verify the claim on the unpublished container first, the start wrapper's refusal (exit 78) closes the
  ports (reconciler and Start job), the restart policy is "no" and the Start job retries SteamCMD's
  failures itself; settings changes pass the admin password so the agent can verify the claim again.
* 2: after a lost claim the server comes back with no game loaded: the claim steps include new_game,
  which loads the newest save of the remembered session (else the newest save, else a new game); the
  saves page shows "current" from the API (none while no game is loaded).
* 5: the backup job's log says what actually happened (stopped server, no game loaded).
* 7: ``ports_open`` is false while the publish step still runs.
"""

from __future__ import annotations

from typing import Any

from apps import games
from apps.backups.handlers import run_backup
from apps.backups.models import BackupKind
from apps.instances.spec import build_spec, rcon_password
from apps.jobs.models import Job, JobStatus

from . import test_games_satisfactory as _sf
from .test_games_satisfactory import _claimed_instance, run

game = _sf.game

ALL = ("claim", "access", "new_game", "publish")


def _open(make_instance, fake_agent, **kw: Any):
    inst = _claimed_instance(make_instance, fake_agent, steps=ALL, **kw)
    state = dict(inst.setup_state)
    state["ports_open"] = True
    inst.setup_state = state
    inst.save(update_fields=["setup_state"])
    fake_agent.apply(inst.id, build_spec(inst), start=kw.get("running", True))
    fake_agent.claim_markers[str(inst.id)] = True
    return inst


# --- item 1 ------------------------------------------------------------------------------------------------
def test_mark_unclaimed_removes_the_agents_claim_marker_and_resets_new_game(make_instance, fake_agent):
    inst = _open(make_instance, fake_agent)
    definition = games.for_instance(inst)
    assert definition.mark_unclaimed(inst, "a restore") is True
    inst.refresh_from_db()
    assert fake_agent.called("forget_claim") and str(inst.id) not in fake_agent.claim_markers
    steps = inst.setup_state["steps"]
    assert all(steps[s]["status"] == "pending" for s in ALL)  # new_game too (item 2)
    assert build_spec(inst)["ports"] == []


def test_mark_unclaimed_with_an_older_agent_does_not_call_it(make_instance, fake_agent):
    inst = _open(make_instance, fake_agent)
    fake_agent.features = [f for f in fake_agent.features if f != "claim_marker"]
    from django.core.cache import cache

    cache.clear()
    games.for_instance(inst).mark_unclaimed(inst, "a restore")
    assert not fake_agent.called("forget_claim")


def test_publish_verifies_the_claim_on_the_unpublished_container_first(
    make_instance,
    fake_agent,
    game,
    django_capture_on_commit_callbacks,
):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game"))
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert game.actions() == ["verify_claim"]
    assert game.calls[0][1] == {"admin_password": rcon_password(inst)}
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True and fake_agent.claim_markers[str(inst.id)]
    # a claim that does not verify publishes nothing
    inst2 = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game"))
    game.lost_claim = True
    job = run("setup", inst2, django_capture_on_commit_callbacks)
    inst2.refresh_from_db()
    assert job.status == JobStatus.FAILED and not inst2.setup_state.get("ports_open")
    assert build_spec(inst2)["ports"] == []


def test_settings_changes_send_the_admin_password(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim",), access="password")
    inst.set_secret_values({**inst.secret_values, "client_password": "join-me"})
    inst.save(update_fields=["secrets"])
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    by_action = dict(game.calls)
    assert by_action["client_password"]["admin_password"] == rcon_password(inst)
    assert by_action["new_game"]["admin_password"] == rcon_password(inst)


def test_the_start_wrappers_refusal_closes_the_ports(make_instance, fake_agent):
    """A hand ``docker start`` of a published container without a verified claim: the wrapper exits 78;
    the reconciler's view of it makes the next start verify the claim on an unpublished container."""
    inst = _open(make_instance, fake_agent)
    c = fake_agent.containers[str(inst.id)]
    fake_agent._set_exited(c, 78)
    definition = games.for_instance(inst)
    definition.observed(inst, fake_agent.status(inst.id))
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is False and definition.must_apply_before_start(inst)
    assert "start guard" in inst.setup_state["steps"]["claim"]["message"]
    # an ordinary exit does not
    inst = _open(make_instance, fake_agent)
    fake_agent._set_exited(fake_agent.containers[str(inst.id)], 0)
    definition.observed(inst, fake_agent.status(inst.id))
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True


def test_start_job_handles_the_wrappers_refusal(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _open(make_instance, fake_agent, running=False)
    refused: list = []

    def wrapper_refuses_published(instance_id: Any) -> None:
        c = fake_agent.containers.get(str(instance_id))
        if c and c["running"] and c["spec"]["ports"] and not refused:
            refused.append(True)  # the wrapper ran and exited at once
            fake_agent._set_exited(c, 78)

    fake_agent.hooks["status"] = wrapper_refuses_published
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    c = fake_agent.containers[str(inst.id)]
    assert refused and c["running"]
    assert any("start guard refused" in line for line in job.log)
    # recovered the designed way: started unpublished, the setup job (run here at once) claimed it on the
    # unpublished container, verified the claim, then published again
    setup = Job.objects.get(instance=inst, kind="setup")
    assert setup.status == JobStatus.SUCCEEDED, setup.error
    assert game.ports_seen_at_claim == [[]] and game.actions()[0] == "claim" and game.actions()[-1] == "verify_claim"
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True and len(c["spec"]["ports"]) == 3


def test_start_job_retries_steamcmds_failure(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _open(make_instance, fake_agent, running=False)
    failed: list = []

    def steamcmd_fails_once(instance_id: Any) -> None:
        c = fake_agent.containers.get(str(instance_id))
        if c and c["running"] and not failed:
            failed.append(True)  # SteamCMD: Missing configuration
            fake_agent._set_exited(c, 8)

    fake_agent.hooks["status"] = steamcmd_fails_once
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert failed and fake_agent.containers[str(inst.id)]["running"]
    assert fake_agent.called("start")  # started again by the job (Docker's policy is "no")
    assert any("exit code 8" in line and "starting it again" in line for line in job.log)


def test_restart_policy_is_no(make_instance, fake_agent):
    inst = _open(make_instance, fake_agent)
    inst.restart_policy = "unless-stopped"
    assert build_spec(inst)["restart_policy"] == "no"


# --- item 2 ------------------------------------------------------------------------------------------------
def _lost(make_instance, fake_agent, game, remembered: str = ""):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access"))
    if remembered:
        opts = dict(inst.game_options)
        opts["session_name"] = remembered
        inst.game_options = opts
        inst.save(update_fields=["game_options"])
    game.current = None  # state 1: no game loaded
    return inst


def test_new_game_step_loads_the_newest_save_of_the_remembered_session(
    make_instance,
    fake_agent,
    game,
    django_capture_on_commit_callbacks,
):
    game.sessions = {"Mine": ["Mine_mcpanel", "Mine_autosave_1"], "Other": ["Other_autosave_0"]}
    game.saved_at = {"Mine_mcpanel": "2026.11.10-20.00.00", "Mine_autosave_1": "2026.11.11-01.00.00",
                     "Other_autosave_0": "2026.11.11-03.00.00"}  # fmt: skip
    inst = _lost(make_instance, fake_agent, game, remembered="Mine")
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    load = [p for a, p in game.calls if a == "load_game"]
    assert load == [{"save_name": "Mine_autosave_1", "admin_password": rcon_password(inst)}]
    assert "new_game" not in game.actions() and game.current == "Mine"
    inst.refresh_from_db()
    assert "loaded the newest save 'Mine_autosave_1'" in inst.setup_state["steps"]["new_game"]["message"]


def test_new_game_step_loads_the_newest_save_overall_without_a_remembered_session(
    make_instance,
    fake_agent,
    game,
    django_capture_on_commit_callbacks,
):
    game.sessions = {"A": ["A_autosave_0"], "B": ["B_autosave_0"]}
    game.saved_at = {"A_autosave_0": "2026.11.11-01.00.00", "B_autosave_0": "2026.11.11-02.00.00"}
    inst = _lost(make_instance, fake_agent, game, remembered="Gone")
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert [p["save_name"] for a, p in game.calls if a == "load_game"] == ["B_autosave_0"]
    inst.refresh_from_db()
    assert inst.game_options["session_name"] == "B"


def test_new_game_step_creates_a_game_only_without_saves(
    make_instance,
    fake_agent,
    game,
    django_capture_on_commit_callbacks,
):
    inst = _lost(make_instance, fake_agent, game)
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert "load_game" not in game.actions() and "new_game" in game.actions()


def test_saves_show_current_from_the_api(make_instance, fake_agent, game):
    inst = _lost(make_instance, fake_agent, game, remembered="Mine")
    game.sessions = {"Mine": ["Mine_autosave_0"]}
    fake_agent.put_file(inst.id, "saved/server/Mine_autosave_0.sav", b"SAVE")
    from apps.nodes.client import AgentClient

    listed = games.for_instance(inst).saves.list(AgentClient(inst.node), inst)
    # no game loaded: nothing is current (not the session the panel remembers)
    assert listed["from_api"] is True and listed["current"] == ""
    assert not any(s["current"] for s in listed["saves"])
    game.current = "Mine"
    listed = games.for_instance(inst).saves.list(AgentClient(inst.node), inst)
    assert listed["current"] == "Mine" and [s["current"] for s in listed["saves"]] == [True]


# --- item 5 ------------------------------------------------------------------------------------------------
class _Ctx:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)

    def agent_progress(self, *_a: Any) -> Any:
        return lambda *_x, **_k: None


def test_backup_log_says_what_happened(make_instance, fake_agent):
    inst = _open(make_instance, fake_agent, running=False)
    fake_agent.prepare(inst.id, {})
    ctx = _Ctx()
    run_backup(inst, ctx, name="b", kind=BackupKind.MANUAL)
    text = " ".join(ctx.lines)
    assert "not running" in text and "SaveGame answered" not in text
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.backup_saving = "no_game"
    ctx = _Ctx()
    run_backup(inst, ctx, name="b", kind=BackupKind.MANUAL)
    text = " ".join(ctx.lines)
    assert "no game loaded" in text and "SaveGame answered" not in text
    fake_agent.backup_saving = "saved"
    ctx = _Ctx()
    run_backup(inst, ctx, name="b", kind=BackupKind.MANUAL)
    assert "SaveGame answered" in " ".join(ctx.lines)


# --- item 7 ------------------------------------------------------------------------------------------------
def test_ports_open_is_false_while_publishing(make_instance, fake_agent):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game"))
    definition = games.for_instance(inst)
    definition._open_ports(inst)  # what the publish step does first
    from apps.instances.game_handlers import record_setup

    record_setup(inst, "publish", "running")
    inst.refresh_from_db()
    assert definition.setup_status(inst)["ports_open"] is False
    record_setup(inst, "publish", "done", "Ports published")
    inst.refresh_from_db()
    assert definition.setup_status(inst)["ports_open"] is True


def test_restart_refused_by_the_agent_queues_the_setup(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    """The real-container run: a file operation under saved/ removed the claim marker; the restart is
    refused (server_unclaimed), the container is recreated unpublished, and the setup must follow (it
    did not: the server stayed unpublished until somebody pressed Start)."""
    from apps.nodes.client import AgentError

    inst = _open(make_instance, fake_agent)
    fake_agent.failures["restart"] = AgentError(
        "This Satisfactory server is not claimed ... (.mcpanel/claim-verified is missing or does not match).",
        "server_unclaimed",
        409,
    )
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    setup = Job.objects.get(instance=inst, kind="setup")
    assert setup.status == JobStatus.SUCCEEDED, setup.error
    assert game.ports_seen_at_claim == [[]]  # claimed again on the unpublished container
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True


def test_publish_retries_steamcmds_failure_of_the_published_container(
    make_instance, fake_agent, game, django_capture_on_commit_callbacks
):
    """The real-container run: the container the publish step created exited 8 (SteamCMD "Missing
    configuration") 20 s after its start; with restart policy "no" nobody started it again."""
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game"))
    failed: list = []

    def steamcmd_fails_once(instance_id: Any) -> None:
        c = fake_agent.containers.get(str(instance_id))
        if c and c["running"] and c["spec"]["ports"] and not failed:
            failed.append(True)
            fake_agent._set_exited(c, 8)

    fake_agent.hooks["status"] = steamcmd_fails_once
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert failed and fake_agent.containers[str(inst.id)]["running"] and fake_agent.called("start")
    assert any("exit code 8" in line for line in job.log)
