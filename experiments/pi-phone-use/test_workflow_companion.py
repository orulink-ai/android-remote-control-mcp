"""One phone tap must bind recording provenance, validate, compile and invoke PI."""

import copy
import json
from pathlib import Path

import pytest

from controller import QUERY, TITLE
from test_workflow_intents import supported_raw
from test_workflow_verification import ACTIONS, screen
from workflow_intents import KINDS, TASK, derive_plan, validation_binding
from workflow_companion import Companion, JobError, STAGES, tool_json, validate_claim
import workflow_companion


def claimed():
    return {
        "job": {
            "id": "job-001",
            "recordingId": "recording-001",
            "name": "手机示教",
            "operation": "generate_and_run",
            "task": {"query": QUERY, "expectedFirstTitle": TITLE},
            "recording": supported_raw(),
        },
        "claimToken": "private-claim-token",
    }


class Phone:
    def __init__(self):
        self.calls = []
        self.status = "validating"

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name.endswith("_update"):
            self.status = args["status"]
            value = {"id": "job-001", "status": self.status, "summary": args.get("summary")}
            return {"content": [{"type": "text", "text": json.dumps(value)}]}
        value = {
            "companionOnline": True,
            "job": {"id": "job-001", "status": self.status, "summary": None},
        }
        return {"content": [{"type": "text", "text": json.dumps(value)}]}


def paired(plan):
    return {
        "schemaVersion": 1,
        "source": "pi-agent-demonstration",
        "status": "success",
        "task": TASK,
        "recordingValidation": validation_binding(plan),
        "steps": [
            {
                "before": screen(i),
                "action": ACTIONS[i],
                "after": screen(i + 1),
                "delivery": "applied",
                "rawEventSequence": i + 1,
                "intentKind": KINDS[i],
            }
            for i in range(8)
        ],
    }


def publish(bundle, *, artifact_id):
    assert (bundle / "recipe.json").is_file()
    return {"artifactId": artifact_id, "published": True}


def runner_calls(*, failure=None, corruption=None):
    calls = []

    def run(provider, trial_id, directory, *, recording, recipe, heartbeat, plan=None):
        calls.append((provider, trial_id, recording, recipe))
        heartbeat()
        directory = Path(directory) / trial_id
        directory.mkdir()
        binding = None
        if recording:
            intent_plan = json.loads(Path(plan).read_text())
            binding = validation_binding(intent_plan)
            evidence = paired(intent_plan)
            if corruption == "binding":
                evidence["recordingValidation"]["androidRecordingSha256"] = "wrong"
            elif corruption == "action":
                evidence["steps"][4]["action"] = ACTIONS[5]
            elif corruption == "after":
                evidence["steps"][4]["after"] = screen(4)
            (directory / "recording.json").write_text(json.dumps(evidence, ensure_ascii=False))
        return {
            "status": "failed" if provider == failure else "success",
            "piElapsedMs": 23000 if recording else 16000,
            "modelCalls": [{}] if recording else [],
            "decisionPolicy": {
                "fallbackRequests": 1 if recording else 0,
                "attempts": [{}] * (2 if recording else 1),
            },
            "milestones": [{"stage": stage} for stage in STAGES],
            "finalState": {
                "kind": "detail",
                "firstTitleVisible": True,
                "liked": False,
                "collected": False,
            },
            "piAgentLoop": True,
            "recordingValidation": binding,
        }

    return run, calls


def test_phone_request_runs_validation_then_generated_recipe_in_real_pi_contract(tmp_path):
    phone = Phone()
    run, calls = runner_calls()
    c = Companion(phone, tmp_path, worker_id="test-worker", runner=run, publisher=publish)
    result = c.process(claimed())
    assert result["status"] == "succeeded"
    assert [x[0] for x in calls] == ["jev-auto", "skill-auto"]
    assert calls[0][2] is True and calls[1][2] is False
    assert Path(calls[1][3]).name == "recipe.json"
    statuses = [a["status"] for n, a in phone.calls if n.endswith("_update")]
    assert statuses == ["validating", "generating", "running", "succeeded"]
    assert any(n.endswith("_status") for n, _ in phone.calls)
    bundle = tmp_path / "job-001" / "bundle"
    assert (bundle / "SKILL.md").is_file()
    recipe = json.loads((bundle / "recipe.json").read_text())
    evidence = json.loads((tmp_path / "job-001" / "verify" / "recording.json").read_text())
    assert evidence["recordingValidation"]["androidRecordingSha256"]
    assert result["summary"]["piElapsedMs"] == 39000
    assert result["summary"]["modelCalls"] == 3
    assert result["summary"]["fallbackRequests"] == 1
    assert result["summary"]["artifactId"] == "job-001"
    assert recipe["sourceSha256"]
    public = json.dumps(phone.calls, ensure_ascii=False)
    assert str(tmp_path) not in public


@pytest.mark.parametrize(
    "change",
    ["operation", "target", "query", "recording_id", "empty", "unbounded", "bad_id", "no_query"],
)
def test_mismatched_recording_never_touches_phone_or_starts_pi(tmp_path, change):
    value = claimed()
    job = value["job"]
    if change == "operation":
        job["operation"] = "run-shell"
    elif change == "target":
        job["recording"]["targetPackage"] = "other.app"
    elif change == "query":
        job["task"]["query"] = "other"
    elif change == "recording_id":
        job["recording"]["id"] = "other"
    elif change == "empty":
        job["recording"]["events"] = []
    elif change == "unbounded":
        job["recording"]["events"] *= 201
    elif change == "bad_id":
        job["id"] = "../escape"
    else:
        job["recording"]["events"][1]["value"] = "other"
    phone = Phone()
    run, calls = runner_calls()
    with pytest.raises(JobError):
        validate_claim(value)
    with pytest.raises(JobError):
        Companion(phone, tmp_path, worker_id="w", runner=run, publisher=publish).process(value)
    assert phone.calls == calls == []


def test_failed_validation_never_generates_or_executes(tmp_path):
    phone = Phone()
    run, calls = runner_calls(failure="jev-auto")
    result = Companion(phone, tmp_path, worker_id="w", runner=run, publisher=publish).process(
        claimed()
    )
    assert result["status"] == "failed"
    assert result["summary"]["errorCode"] == "VALIDATION_FAILED"
    assert len(calls) == 1
    assert not (tmp_path / "job-001" / "bundle").exists()


def test_failed_pi_replay_is_not_reported_as_success(tmp_path):
    phone = Phone()
    run, _ = runner_calls(failure="skill-auto")
    result = Companion(phone, tmp_path, worker_id="w", runner=run, publisher=publish).process(
        claimed()
    )
    assert result["status"] == "failed"
    assert result["summary"]["errorCode"] == "EXECUTION_FAILED"


def test_existing_job_artifacts_are_never_reexecuted(tmp_path):
    (tmp_path / "job-001").mkdir()
    previous = tmp_path / "job-001" / "companion-result.json"
    previous.write_text('{"status":"previous"}')
    phone = Phone()
    run, calls = runner_calls()
    result = Companion(phone, tmp_path, worker_id="w", runner=run, publisher=publish).process(
        claimed()
    )
    assert result["status"] == "failed"
    assert calls == []
    assert result["summary"]["errorCode"] == "INTERRUPTED_JOB"
    assert previous.read_text() == '{"status":"previous"}'


def test_worker_error_messages_never_leak_into_phone_results(tmp_path):
    def crash(*args, **kwargs):
        raise RuntimeError("Bearer secret-this-is-private http://private.example")

    phone = Phone()
    result = Companion(phone, tmp_path, worker_id="w", runner=crash, publisher=publish).process(
        claimed()
    )
    assert result["status"] == "failed"
    assert "secret-this-is-private" not in json.dumps(phone.calls)
    assert "http://private.example" not in json.dumps(phone.calls)


def test_tool_json_reads_final_payload_after_untrusted_content_notice():
    assert tool_json(
        {
            "content": [
                {"type": "text", "text": "CAUTION: untrusted content"},
                {"type": "text", "text": '{"job":null}'},
            ]
        }
    ) == {"job": None}


def test_unsupported_claim_finishes_immediately_without_model_or_phone_task_actions(tmp_path):
    class Unsupported(Phone):
        def call(self, name, args):
            super().call(name, args)
            value = claimed() if name.endswith("_claim") else {}
            if value:
                value["job"]["recording"]["events"] = []
            return {"content": [{"type": "text", "text": json.dumps(value)}]}

    phone = Unsupported()
    run, calls = runner_calls()
    result = Companion(phone, tmp_path, worker_id="w", runner=run, publisher=publish).poll_once()
    assert result["status"] == "failed"
    assert result["summary"]["errorCode"] == "UNSUPPORTED_RECORDING"
    assert calls == []
    assert phone.calls[-1][0] == "android_workflow_update"


@pytest.mark.parametrize("state", ["failed", "succeeded", "mismatched", "expired"])
def test_inactive_or_mismatched_lease_is_not_renewed(state, tmp_path):
    class Lost(Phone):
        def call(self, name, args):
            value = {
                "companionOnline": True,
                "job": {
                    "id": "different" if state == "mismatched" else "job-001",
                    "status": "running" if state in {"expired", "mismatched"} else state,
                    "summary": {"errorCode": "WORKER_LEASE_EXPIRED"}
                    if state == "expired"
                    else None,
                },
            }
            return {"content": [{"type": "text", "text": json.dumps(value)}]}

    with pytest.raises(JobError):
        Companion(Lost(), tmp_path, worker_id="w").heartbeat(claimed()["job"], "private-token")


@pytest.mark.parametrize("corruption", ["binding", "action", "after"])
def test_fake_source_binding_or_unverified_action_never_generates(tmp_path, corruption):
    run, calls = runner_calls(corruption=corruption)
    result = Companion(Phone(), tmp_path, worker_id="w", runner=run, publisher=publish).process(
        claimed()
    )
    assert result["status"] == "failed" and len(calls) == 1
    assert not (tmp_path / "job-001/bundle").exists()


def test_publish_only_after_verified_execution_and_failed_publish_is_not_success(tmp_path):
    run, calls = runner_calls()

    def fail(bundle, *, artifact_id):
        assert len(calls) == 2
        raise RuntimeError("private publisher details")

    result = Companion(Phone(), tmp_path, worker_id="w", runner=run, publisher=fail).process(
        claimed()
    )
    assert result["status"] == "failed" and result["summary"]["errorCode"] == "PUBLISH_FAILED"


@pytest.mark.parametrize(
    "back_count,raw_mode,fault",
    [
        (0, "strict", "none"),
        (4, "strict", "none"),
        (5, "strict", "none"),
        (5, "semantic", "none"),
        (5, "semantic-recorded-submit", "none"),
        (5, "semantic", "jev"),
        (5, "semantic", "both"),
    ],
)
def test_actual_new_runner_two_pi_loops_consume_raw_compile_and_replay(
    tmp_path, monkeypatch, back_count, raw_mode, fault
):
    """Real PI, worker, policies and new runner; only base phone/model are fake."""
    import shutil
    import sys
    from functools import partial
    import workflow_runner
    from workflow import compile_recording
    from workflow_skill_install import SkillPublisher

    if shutil.which("pi") is None:
        pytest.skip("PI not installed")
    source = Path(workflow_companion.__file__).parent
    compile_recording(paired(derive_plan(supported_raw())), tmp_path / "initial")
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"phone": {"url": "http://offline.invalid/mcp"}}}))
    profile = {
        "python": sys.executable,
        "pi": shutil.which("pi"),
        "mcpConfig": str(config),
        "mcpServer": "phone",
        "recipe": str(tmp_path / "initial/recipe.json"),
    }
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps(profile))
    verify_wrapper = tmp_path / "offline-verify.py"
    verify_wrapper.write_text("""import argparse,asyncio,os
from providers import ProviderError
from test_workflow_entry import install_transport_double
from workflow_verification import DraftIntentRuntime,load_plan
import worker
install_transport_double()
class Model:
    mode='warm';decisionStyle='heads'
    def __init__(self,*a,**kw): self.transition_calls=0
    async def decide(self,backend,state,options):
        if state.get('runtimeTransition') and state['observation']['kind']=='search_input' and state['observation'].get('queryMatches'):
            self.transition_calls+=1
            limit={'none':0,'jev':1,'both':2}[os.environ['OFFLINE_TRANSITION_FAULT']]
            if self.transition_calls<=limit:
                raise ProviderError('offline',network={},latency_ms=0,exception_type='ConnectError',stage='tcp_connect')
        return {'choice':next((key for key in ('OPEN_APP','CLICK:terminal-back','NEXT','WAIT') if key in options)),'latencyMs':0,'usage':{},'cost':0}
    async def close(self): pass
p=argparse.ArgumentParser();p.add_argument('--provider');p.add_argument('--trial-id');a=p.parse_args()
plan=load_plan(os.environ['PHONE_WORKFLOW_PLAN'])
asyncio.run(worker.serve('jev-auto',a.trial_id,runtime_factory=lambda t:DraftIntentRuntime(t,plan),client_factory=Model,key_reader=lambda:'offline'))
""")
    execute_wrapper = tmp_path / "offline-execute.py"
    execute_wrapper.write_text(
        verify_wrapper.read_text().split("p=argparse.ArgumentParser()")[0]
        + "asyncio.run(worker.main(client_factory=Model,key_reader=lambda:'offline'))\n"
    )
    original_launch = workflow_runner.phase_launch

    def launch(profile, phase, directory, **kwargs):
        command, env = original_launch(profile, phase, directory, **kwargs)
        env.update(
            PHONE_BENCH_WORKER=str(verify_wrapper if phase == "verify" else execute_wrapper),
            PYTHONPATH=str(source),
            PI_OFFLINE="1",
            PI_TELEMETRY="0",
            OFFLINE_BACKS=str(back_count),
            OFFLINE_TRANSITION_FAULT=fault,
        )
        return command, env

    monkeypatch.setattr(workflow_runner, "phase_launch", launch)
    publisher = SkillPublisher(profile_file, skills_directory=tmp_path / "installed-skills")
    request = claimed()
    if raw_mode != "strict":
        from test_workflow_semantic import semantic_raw, with_recorded_submit

        request["job"]["recording"] = (
            semantic_raw() if raw_mode == "semantic" else with_recorded_submit()
        )
    result = Companion(
        Phone(),
        tmp_path / "jobs",
        worker_id="offline-unique",
        runner=partial(workflow_runner.run_pi, profile=profile),
        publisher=publisher,
    ).process(request)
    job = tmp_path / "jobs/job-001"
    if fault == "both":
        assert (
            result["status"] == "failed" and result["summary"]["errorCode"] == "VALIDATION_FAILED"
        )
        assert not (job / "bundle").exists()
        verify = json.loads((job / "verify/result.json").read_text())
        evidence = json.loads((job / "verify/recording.json").read_text())
        assert verify["decisionPolicy"]["fallbackRequests"] == 1
        assert [
            step["rawEventSequence"]
            for step in evidence["steps"]
            if step.get("rawEventSequence") is not None
        ] == [1, 4]
        assert all(step["action"]["role"] not in {"like", "collect"} for step in evidence["steps"])
        assert not any(
            step.get("origin") == "runtime-required-transition" for step in evidence["steps"]
        )
        return
    assert result["status"] == "succeeded"
    verify = json.loads((job / "verify/result.json").read_text())
    execute = json.loads((job / "execute/result.json").read_text())
    assert verify["piAgentLoop"] is execute["piAgentLoop"] is True
    assert len(verify["decisionPolicy"]["attempts"]) == 9 + back_count + (fault == "jev")
    if raw_mode != "strict":
        from workflow_intents import evidence_matches_plan

        plan = json.loads((job / "intent-plan.json").read_text())
        evidence = json.loads((job / "verify/recording.json").read_text())
        assert evidence_matches_plan(evidence, plan)
        assert [
            step["rawEventSequence"]
            for step in evidence["steps"]
            if step.get("rawEventSequence") is not None
        ] == [item["eventSequence"] for item in plan["intents"]]
        recipe = json.loads((job / "bundle/recipe.json").read_text())
        assert recipe["rawIntentCount"] == (7 if raw_mode == "semantic" else 8)
        assert recipe["runtimeTransitionCount"] == (1 if raw_mode == "semantic" else 0)
        if fault == "jev":
            assert verify["decisionPolicy"]["fallbackSuccesses"] == 1
            transitions = [
                step
                for step in evidence["steps"]
                if step.get("origin") == "runtime-required-transition"
            ]
            assert len(transitions) == 1 and transitions[0]["rawEventSequence"] is None
            assert transitions[0]["betweenRawEvents"] == [4, 7]
    assert execute["decisionPolicy"]["recipeHits"] == 8
    assert execute["decisionPolicy"]["preparationRequests"] == back_count + 1
    assert execute["decisionPolicy"]["recoveryRequests"] == 0
    assert execute["decisionPolicy"]["recoveryActive"] is False
    installed = Path(json.loads(profile_file.read_text())["recipe"])
    assert installed.read_bytes() == (job / "bundle/recipe.json").read_bytes()
    assert json.loads(installed.read_text())["entryStrategy"] == "prepare-feed-v1"
    next_directory = tmp_path / "next-session"
    next_directory.mkdir()
    next_result = workflow_runner.run_pi(
        "skill-auto",
        "execute",
        next_directory,
        recording=False,
        recipe=installed,
        heartbeat=lambda: None,
        profile=workflow_runner.load_profile(profile_file),
    )
    assert next_result["decisionPolicy"]["recipeHits"] == 8
    assert next_result["decisionPolicy"]["preparationRequests"] == back_count + 1
    assert next_result["decisionPolicy"]["recoveryRequests"] == 0
    assert (
        json.loads((job / "companion-result.json").read_text())["phases"]["publication"]["status"]
        == "installed"
    )


def test_cli_worker_identity_is_unique_per_process_and_explicit_profile_is_bound(
    tmp_path, monkeypatch
):
    import workflow_skill_install

    profile_file = tmp_path / "profile.json"
    profile_file.write_text('{"python":"/absolute/python","recipe":"/old/recipe.json"}')
    owners = []
    monkeypatch.setattr(workflow_skill_install, "SkillPublisher", lambda path: publish)

    def serve(args, profile, worker_id, publisher):
        owners.append(worker_id)
        assert profile["python"] == "/absolute/python" and publisher is publish
        return 0

    monkeypatch.setattr(workflow_companion, "serve", serve)
    for _ in range(2):
        assert (
            workflow_companion.main(
                ["--once", "--profile", str(profile_file), "--output", str(tmp_path / "jobs")]
            )
            == 0
        )
    assert owners[0] != owners[1] and all(
        owner.startswith("local-pi-companion-") for owner in owners
    )


def test_no_publication_after_lease_is_lost_following_successful_execution(tmp_path):
    run, calls = runner_calls()

    class Expired(Phone):
        execution_beats = 0

        def call(self, name, args):
            if name.endswith("_status") and len(calls) == 2:
                self.execution_beats += 1
            if self.execution_beats >= 2:
                return {
                    "content": [{"type": "text", "text": '{"companionOnline":false,"job":null}'}]
                }
            return super().call(name, args)

    publications = []
    with pytest.raises(workflow_companion.FeedbackError):
        Companion(
            Expired(),
            tmp_path,
            worker_id="w",
            runner=run,
            publisher=lambda *a, **kw: publications.append(True),
        ).process(claimed())
    saved = json.loads((tmp_path / "job-001/companion-result.json").read_text())
    assert saved["status"] == "failed" and publications == []


class TerminalPhone(Phone):
    def __init__(self, behavior):
        super().__init__()
        self.behavior = behavior
        self.final_calls = 0
        self.final_summary = None

    def call(self, name, args):
        if name.endswith("_update") and args["status"] in {"succeeded", "failed"}:
            self.calls.append((name, copy.deepcopy(args)))
            self.final_calls += 1
            if self.final_calls == 1 and self.behavior == "not_delivered":
                raise RuntimeError("offline response with private details")
            if self.behavior == "offline":
                raise RuntimeError("offline")
            self.status, self.final_summary = args["status"], args["summary"]
            if self.final_calls == 1 and self.behavior == "ack_lost":
                raise RuntimeError("ack lost")
            value = {"id": "job-001", "status": self.status, "summary": self.final_summary}
            return {"content": [{"type": "text", "text": json.dumps(value)}]}
        result = super().call(name, args)
        if name.endswith("_status"):
            value = json.loads(result["content"][0]["text"])
            value["job"]["summary"] = self.final_summary
            return {"content": [{"type": "text", "text": json.dumps(value)}]}
        return result


@pytest.mark.parametrize("behavior,expected_updates", [("not_delivered", 2), ("ack_lost", 1)])
def test_terminal_feedback_recovers_without_replaying_or_republishing(
    tmp_path, behavior, expected_updates
):
    phone = TerminalPhone(behavior)
    run, calls = runner_calls()
    publications = []

    def publisher(bundle, *, artifact_id):
        publications.append(artifact_id)
        return publish(bundle, artifact_id=artifact_id)

    connections = []

    def reconnect(**kwargs):
        assert 0 < kwargs["timeout"] <= 2
        connections.append(True)
        return phone

    result = Companion(
        phone,
        tmp_path,
        worker_id="w",
        runner=run,
        publisher=publisher,
        feedback_client_factory=reconnect,
    ).process(claimed())
    assert result["status"] == "succeeded"
    assert len(calls) == 2 and publications == ["job-001"]
    assert phone.final_calls == expected_updates and len(connections) == 2
    receipt = json.loads((tmp_path / "job-001/terminal-delivery.json").read_text())
    assert receipt["status"] == "confirmed"
    assert "private" not in json.dumps(receipt)
    assert all(not name.endswith("_claim") for name, _ in phone.calls)


def test_terminal_feedback_continuous_disconnect_is_bounded_and_retains_physical_success(tmp_path):
    phone = TerminalPhone("offline")
    run, calls = runner_calls()

    class Offline:
        def call(self, *args):
            raise RuntimeError("private network data")

        def close(self):
            pass

    clients = iter([phone, Offline()])
    with pytest.raises(workflow_companion.FeedbackError):
        Companion(
            phone,
            tmp_path,
            worker_id="w",
            runner=run,
            publisher=publish,
            feedback_client_factory=lambda **kw: next(clients),
        ).process(claimed())
    assert len(calls) == 2 and phone.final_calls == 1
    saved = json.loads((tmp_path / "job-001/companion-result.json").read_text())
    delivery = json.loads((tmp_path / "job-001/terminal-delivery.json").read_text())
    assert saved["status"] == "succeeded" and delivery["status"] == "unconfirmed"
    assert "private network" not in json.dumps(delivery)


def test_expired_terminal_job_is_never_revived_by_feedback_retry(tmp_path):
    phone = TerminalPhone("not_delivered")
    run, calls = runner_calls()

    class Expired:
        def call(self, name, args):
            assert name.endswith("_status"), "A failed terminal job must never receive succeeded"
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "companionOnline": True,
                                "job": {
                                    "id": "job-001",
                                    "status": "failed",
                                    "summary": {"errorCode": "WORKER_LEASE_EXPIRED"},
                                },
                            }
                        ),
                    }
                ]
            }

        def close(self):
            pass

    clients = iter([phone, Expired()])
    with pytest.raises(workflow_companion.FeedbackError):
        Companion(
            phone,
            tmp_path,
            worker_id="w",
            runner=run,
            publisher=publish,
            feedback_client_factory=lambda **kw: next(clients),
        ).process(claimed())
    assert len(calls) == 2 and phone.final_calls == 1


def test_feedback_absolute_budget_is_checked_after_reconnect_before_any_status_or_update(
    tmp_path, monkeypatch
):
    phone = TerminalPhone("not_delivered")
    run, calls = runner_calls()
    clock = [0]
    monkeypatch.setattr(workflow_companion.time, "monotonic", lambda: clock[0])

    class NeverCalled:
        def call(self, *_):
            pytest.fail("No request after overall budget exhausted")

        def close(self):
            pass

    connections = []

    def reconnect(**kw):
        if not connections:
            connections.append(True)
            return phone
        clock[0] += 61
        return NeverCalled()

    with pytest.raises(workflow_companion.FeedbackError):
        Companion(
            phone,
            tmp_path,
            worker_id="w",
            runner=run,
            publisher=publish,
            feedback_client_factory=reconnect,
        ).process(claimed())
    assert len(calls) == 2 and phone.final_calls == 1


def test_terminal_feedback_total_deadline_includes_creation_request_and_close(
    tmp_path, monkeypatch
):
    import asyncio
    import time

    monkeypatch.setattr(workflow_companion, "FEEDBACK_BUDGET_SECONDS", 0.05)
    aborted, updates = [], []

    class Slow:
        async def call(self, name, args):
            updates.append(name)
            await asyncio.sleep(0.02)
            raise RuntimeError("offline")

        async def close(self):
            await asyncio.sleep(0.02)

        async def abort(self):
            aborted.append(True)

    async def factory(**kwargs):
        await asyncio.sleep(0.02)
        return Slow()

    companion = Companion(Phone(), tmp_path, worker_id="w", feedback_client_factory=factory)
    start = time.perf_counter()
    receipt = asyncio.run(
        companion.terminal_delivery(
            claimed()["job"],
            claimed()["claimToken"],
            {"status": "succeeded", "summary": {"artifactId": "job-001"}},
        )
    )
    assert time.perf_counter() - start < 0.3
    assert receipt["status"] == "unconfirmed" and receipt["reason"] == "deadline"
    assert updates == ["android_workflow_update"] and aborted == [True]
