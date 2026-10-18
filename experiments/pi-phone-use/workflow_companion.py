"""Computer companion for phone-owned workflow requests over authenticated MCP.

The phone queues an explicitly scoped task. This process validates its recording,
asks real PI to verify it, compiles paired evidence, and asks PI to execute the
generated recipe. It never selects or dispatches a timed phone task action.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import inspect
import os
from pathlib import Path
import re
from functools import partial
import uuid
import sys
import time

from config import output_dir
from controller import QUERY, TITLE, private_write
from workflow import compile_recording, draft_android_recording, recipe_digest
from workflow_intents import derive_plan, evidence_matches_plan, validation_binding
from workflow_runner import (
    WorkflowStopped,
    feedback_mcp,
    load_profile,
    profile_mcp,
    run_pi,
    termination_scope,
)

DIRECTORY = Path(__file__).resolve().parent
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
TASK = {"query": QUERY, "expectedFirstTitle": TITLE}
STAGES = ("search", "open_first", "like", "collect", "unlike", "uncollect")
FEEDBACK_BUDGET_SECONDS = 60


class JobError(ValueError):
    """Invalid request; raw exceptions and payloads never cross to the phone."""


class FeedbackError(RuntimeError):
    """Physical completion is retained, but phone acknowledgement is unconfirmed."""


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


def terminal_matches(value, job, public):
    summary = value.get("summary") or {}
    return (
        value.get("id") == job["id"]
        and value.get("status") == public["status"]
        and summary.get("errorCode") != "WORKER_LEASE_EXPIRED"
        and all(summary.get(key) == item for key, item in public["summary"].items())
    )


def tool_json(result):
    """Project tools precede their JSON payload with an untrusted-data notice."""
    try:
        blocks = [item["text"] for item in result["content"] if item.get("type") == "text"]
        value = json.loads(blocks[-1])
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise JobError("Malformed workflow tool result") from None


def validate_claim(value):
    try:
        job, token = value["job"], value["claimToken"]
        raw = job["recording"]
        events = raw["events"]
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 256
            or not SAFE_ID.fullmatch(job["id"])
            or not SAFE_ID.fullmatch(job["recordingId"])
            or job["operation"] != "generate_and_run"
            or job["task"] != TASK
            or raw["id"] != job["recordingId"]
            or raw["schemaVersion"] != 1
            or raw["source"] != "android-accessibility-events"
            or raw["targetPackage"] != "com.xingin.xhs"
            or not isinstance(events, list)
            or not 1 <= len(events) <= 200
            or len(json.dumps(raw, ensure_ascii=False).encode()) > 1_500_000
        ):
            raise ValueError
        derive_plan(raw)
        return copy.deepcopy(job), token
    except (KeyError, TypeError, ValueError, AttributeError):
        raise JobError("Unsupported or incomplete recorded workflow") from None


def verified_result(result):
    state = result.get("finalState", {})
    return (
        result.get("status") == "success"
        and result.get("piAgentLoop") is True
        and state.get("kind") == "detail"
        and state.get("firstTitleVisible") is True
        and state.get("liked") is False
        and state.get("collected") is False
        and [s.get("stage") for s in result.get("milestones", [])] == list(STAGES)
    )


class Companion:
    def __init__(
        self,
        mcp,
        directory,
        *,
        worker_id,
        runner=run_pi,
        publisher=None,
        feedback_client_factory=None,
    ):
        if not SAFE_ID.fullmatch(worker_id):
            raise JobError("Invalid worker identifier")
        self.mcp, self.directory, self.worker_id, self.runner = (
            mcp,
            Path(directory),
            worker_id,
            runner,
        )
        self.publisher = publisher
        self.feedback_client_factory = feedback_client_factory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def heartbeat(self, job=None, token=None):
        args = {"worker_id": self.worker_id}
        if job is not None:
            args.update(job_id=job["id"], claim_token=token)
        value = tool_json(self.mcp.call("android_workflow_status", args))
        if job is not None:
            current = value.get("job") or {}
            if (
                value.get("companionOnline") is not True
                or current.get("id") != job["id"]
                or current.get("status") not in {"validating", "generating", "running"}
                or (current.get("summary") or {}).get("errorCode") == "WORKER_LEASE_EXPIRED"
            ):
                raise JobError("Workflow lease is no longer active")
        return value

    def update(self, job, token, status, summary=None):
        args = {"job_id": job["id"], "claim_token": token, "status": status}
        if summary is not None:
            args["summary"] = summary
        self.mcp.call("android_workflow_update", args)

    async def terminal_delivery(self, job, token, public):
        """Retry only this completed outcome; never claim, execute or publish."""
        deadline = time.monotonic() + FEEDBACK_BUDGET_SECONDS
        receipt = {
            "jobId": job["id"],
            "status": "unconfirmed",
            "attempts": 0,
            "reason": "delivery_failed",
        }

        def check_budget():
            if time.monotonic() >= deadline:
                raise TimeoutError()

        try:
            async with asyncio.timeout(FEEDBACK_BUDGET_SECONDS):
                for index in range(2):
                    client, cancelled = None, False
                    try:
                        check_budget()
                        receipt["attempts"] += 1
                        client = (
                            await resolved(
                                self.feedback_client_factory(
                                    timeout=min(2, deadline - time.monotonic())
                                )
                            )
                            if self.feedback_client_factory
                            else self.mcp
                        )
                        check_budget()
                        if index:
                            state = tool_json(
                                await resolved(
                                    client.call(
                                        "android_workflow_status",
                                        {
                                            "worker_id": self.worker_id,
                                            "job_id": job["id"],
                                            "claim_token": token,
                                        },
                                    )
                                )
                            )
                            check_budget()
                            current = state.get("job") or {}
                            if terminal_matches(current, job, public):
                                receipt.update(status="confirmed", reason="read_confirmed")
                                return receipt
                            if (
                                state.get("companionOnline") is not True
                                or current.get("id") != job["id"]
                                or current.get("status")
                                not in {"validating", "generating", "running"}
                                or (current.get("summary") or {}).get("errorCode")
                                == "WORKER_LEASE_EXPIRED"
                            ):
                                receipt["reason"] = "conflicting_terminal_state"
                                return receipt
                        value = tool_json(
                            await resolved(
                                client.call(
                                    "android_workflow_update",
                                    {
                                        "job_id": job["id"],
                                        "claim_token": token,
                                        "status": public["status"],
                                        "summary": public["summary"],
                                    },
                                )
                            )
                        )
                        check_budget()
                        if terminal_matches(value, job, public):
                            receipt.update(status="confirmed", reason="acknowledged")
                            return receipt
                    except asyncio.CancelledError:
                        cancelled = True
                        raise
                    except TimeoutError:
                        receipt["reason"] = "deadline"
                        return receipt
                    except Exception:
                        pass
                    finally:
                        if client is not None and client is not self.mcp:
                            try:
                                method = (
                                    getattr(client, "abort", None)
                                    if cancelled or time.monotonic() >= deadline
                                    else None
                                )
                                method = method or getattr(client, "close", None)
                                if method:
                                    await resolved(method())
                            except asyncio.CancelledError:
                                if hasattr(client, "abort"):
                                    await resolved(client.abort())
                                raise
                            except Exception:
                                pass
        except TimeoutError:
            receipt["reason"] = "deadline"
        return receipt

    def process(self, claimed):
        job, token = validate_claim(claimed)
        directory = self.directory / job["id"]
        error_code = "INTERRUPTED_JOB"
        created = False
        started = time.perf_counter()
        phases = {}
        try:
            directory.mkdir(mode=0o700, exist_ok=False)
            created = True
            raw = job["recording"]
            raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            raw_hash = hashlib.sha256(raw_json.encode()).hexdigest()
            private_write(directory / "android-recording.json", raw_json)
            draft_android_recording(raw, directory / "draft")
            plan = derive_plan(raw)
            private_write(
                directory / "intent-plan.json", json.dumps(plan, ensure_ascii=False, indent=2)
            )
            self.update(job, token, "validating")
            error_code = "VALIDATION_FAILED"
            verified = self.runner(
                "jev-auto",
                "verify",
                directory,
                recording=True,
                recipe=None,
                plan=directory / "intent-plan.json",
                heartbeat=lambda: self.heartbeat(job, token),
            )
            phases["verify"] = verified
            if not verified_result(verified):
                raise JobError("PI validation failed")
            generation_started = time.perf_counter()
            self.update(job, token, "generating")
            error_code = "GENERATION_FAILED"
            evidence_path = directory / "verify" / "recording.json"
            evidence = json.loads(evidence_path.read_text())
            if evidence.get("task") != TASK:
                raise JobError("Validation task changed")
            binding = validation_binding(plan)
            if verified.get("recordingValidation") != binding or not evidence_matches_plan(
                evidence, plan
            ):
                raise JobError("Paired evidence is not bound to every recorded intent")
            compiled = compile_recording(
                evidence, directory / "bundle", entry_strategy="prepare-feed-v1"
            )
            compiled.update(androidRecordingSha256=raw_hash, intentPlanSha256=plan["planSha256"])
            compiled["recipeSha256"] = recipe_digest(compiled)
            private_write(
                directory / "bundle/recipe.json", json.dumps(compiled, ensure_ascii=False, indent=2)
            )
            phases["generation"] = {"elapsedMs": (time.perf_counter() - generation_started) * 1000}
            self.update(job, token, "running")
            error_code = "EXECUTION_FAILED"
            result = self.runner(
                "skill-auto",
                "execute",
                directory,
                recording=False,
                recipe=directory / "bundle" / "recipe.json",
                heartbeat=lambda: self.heartbeat(job, token),
            )
            phases["execute"] = result
            if not verified_result(result):
                raise JobError("PI execution failed")
            self.heartbeat(job, token)
            error_code = "PUBLISH_FAILED"
            if self.publisher is None:
                raise JobError("Verified bundle publisher is not configured")
            phases["publication"] = self.publisher(directory / "bundle", artifact_id=job["id"])
            summary = {
                "piElapsedMs": sum(value.get("piElapsedMs") or 0 for value in (verified, result)),
                "modelCalls": sum(
                    len(
                        value.get("decisionPolicy", {}).get("attempts", value.get("modelCalls", []))
                    )
                    for value in (verified, result)
                ),
                "fallbackRequests": sum(
                    value.get("decisionPolicy", {}).get("fallbackRequests", 0)
                    for value in (verified, result)
                ),
                "artifactId": job["id"],
            }
            public = {"status": "succeeded", "summary": summary}
        except Exception:
            # A retained directory is never resumed by dispatching actions again.
            public = {
                "status": "failed",
                "summary": {"errorCode": error_code, "artifactId": job["id"]},
            }
        if created:
            try:
                private_write(
                    directory / "companion-result.json",
                    json.dumps(
                        {
                            **public,
                            "phases": phases,
                            "wallElapsedMs": (time.perf_counter() - started) * 1000,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            except OSError:
                public = {
                    "status": "failed",
                    "summary": {"errorCode": "STORAGE_FAILED", "artifactId": job["id"]},
                }
        delivery = asyncio.run(self.terminal_delivery(job, token, public))
        if created:
            private_write(directory / "terminal-delivery.json", json.dumps(delivery, indent=2))
        if delivery["status"] != "confirmed":
            raise FeedbackError(
                "Completed workflow feedback is unconfirmed; phone actions were not repeated"
            )
        return public

    def poll_once(self):
        self.heartbeat()
        value = tool_json(self.mcp.call("android_workflow_claim", {"worker_id": self.worker_id}))
        if value.get("job") is None:
            return None
        try:
            return self.process(value)
        except JobError:
            job, token = value.get("job", {}), value.get("claimToken")
            if (
                not isinstance(job, dict)
                or not SAFE_ID.fullmatch(str(job.get("id", "")))
                or not isinstance(token, str)
                or not 8 <= len(token) <= 256
            ):
                raise
            result = {"status": "failed", "summary": {"errorCode": "UNSUPPORTED_RECORDING"}}
            self.update(job, token, result["status"], result["summary"])
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-id", default="local-pi-companion")
    parser.add_argument("--profile", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=output_dir() / "workflow-jobs")
    args = parser.parse_args(argv)
    if not SAFE_ID.fullmatch(args.worker_id) or len(args.worker_id) > 60:
        return 2
    worker_id = args.worker_id + "-" + uuid.uuid4().hex[:12]
    try:
        profile_path = (
            args.profile
            or Path(os.environ.get("PI_PHONE_PROFILE", "~/.config/orulink-phone/terminal.json"))
        ).expanduser()
        profile = load_profile(profile_path)
        from workflow_skill_install import SkillPublisher

        publisher = SkillPublisher(profile_path)
        with termination_scope():
            return serve(args, profile, worker_id, publisher)
    except WorkflowStopped:
        return 143
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('{"status":"invalid_companion_configuration"}', flush=True)
        return 2


def serve(args, profile, worker_id, publisher):
    while True:
        client = None
        try:
            client = profile_mcp(profile)
            companion = Companion(
                client,
                args.output,
                worker_id=worker_id,
                runner=partial(run_pi, profile=profile),
                publisher=publisher,
                feedback_client_factory=partial(feedback_mcp, profile),
            )
            while True:
                result = companion.poll_once()
                if result:
                    print(json.dumps(result), flush=True)
                if args.once:
                    return 0 if result is None or result["status"] == "succeeded" else 1
                time.sleep(2)
        except KeyboardInterrupt:
            return 130
        except Exception:
            # No URLs, headers, response bodies or raw error messages in daemon logs.
            print('{"status":"waiting_for_phone"}', flush=True)
            if args.once:
                return 1
            time.sleep(5)
        finally:
            if client is not None:
                client.close()


if __name__ == "__main__":
    sys.exit(main())
