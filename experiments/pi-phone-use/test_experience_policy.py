"""Every experience-assisted choice still comes from the normal model policy."""

import asyncio
import copy

import pytest

from decision_policy import ObservationRefreshRequired
from experience_policy import ExperiencePolicy
from providers import ProviderError
from test_experience_skill import write_skill


class Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def decide(self, backend, state, options):
        self.calls.append((backend, copy.deepcopy(state), dict(options)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"choice": outcome, "servedModel": "actual-test-model"}


def snapshot(query="新的请求词", suffix="a"):
    return {
        "state": {
            "goal": "搜索" + query,
            "query": query,
            "stage": "search",
            "observation": {"kind": "search_input"},
        },
        "options": {f"TYPE:node_{suffix}": "当前输入框", "BLOCKED": "停止"},
        "done": False,
    }


def failure():
    return ProviderError(
        "omitted", exception_type="ConnectError", stage="send_request", network={}, latency_ms=1
    )


def test_each_step_calls_jev_and_preserves_all_current_candidates_and_state(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    client = Client("TYPE:node_a", "BLOCKED")
    policy = ExperiencePolicy(client, skill_path=path)
    current = snapshot()
    before = copy.deepcopy(current)
    first = asyncio.run(policy.decide(current, 1))
    second = asyncio.run(policy.decide(current, 2))
    assert first["choice"] == "TYPE:node_a" and second["choice"] == "BLOCKED"
    assert [call[0] for call in client.calls] == ["jev", "jev"]
    assert all(call[2] == before["options"] for call in client.calls)
    assert current == before
    sent = client.calls[0][1]
    assert sent.pop("skillExperience")["advisoryOnly"]
    assert sent == current["state"]
    summary = policy.summary()
    assert summary["mode"] == "experience-assisted" and summary["decisionMode"] == "jev-auto"
    assert summary["experienceLoaded"] and summary["sourceSha256"] == "a" * 64
    assert summary["hintDecisions"] == 2 and summary["modelRequests"] == 2
    assert summary["recipeHits"] == 0 and len(summary["attempts"]) == 2


def test_none_is_the_unmodified_jev_control_without_experience_context():
    client = Client("TYPE:node_a")
    policy = ExperiencePolicy(client)
    current = snapshot()
    asyncio.run(policy.decide(current, 1))
    assert client.calls[0][1] == current["state"]
    assert "skillExperience" not in client.calls[0][1]
    summary = policy.summary()
    assert summary["mode"] == "jev-auto" and not summary["experienceLoaded"]
    assert summary["sourceSha256"] is None and summary["hintDecisions"] == 0
    assert summary["modelRequests"] == 1 and summary["recipeHits"] == 0


def test_fallback_waits_for_pi_fresh_observation_and_uses_new_state_same_semantic_hint(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    client = Client(failure(), "TYPE:node_b", "BLOCKED")
    policy = ExperiencePolicy(client, skill_path=path)

    async def scenario():
        assert await policy.decide(snapshot(), 4) == {
            "kind": "refresh_required",
            "fallbackPending": True,
        }
        with pytest.raises(ObservationRefreshRequired):
            await policy.decide(snapshot(), 4)
        result = await policy.decide(snapshot("完全不同的当前词", "b"), 5)
        assert result["selectedBackend"] == "deepseek" and result["fallbackUsed"]
        await policy.decide(snapshot(), 6)

    asyncio.run(scenario())
    assert [call[0] for call in client.calls] == ["jev", "deepseek", "jev"]
    assert client.calls[1][1]["query"] == "完全不同的当前词"
    assert client.calls[1][2] == snapshot(suffix="b")["options"]
    assert client.calls[0][1]["skillExperience"] == client.calls[1][1]["skillExperience"]
    summary = policy.summary()
    assert summary["hintDecisions"] == 2 and summary["modelRequests"] == 3
    assert summary["primaryFailures"] == 1 and summary["fallbackSuccesses"] == 1


def test_bad_path_is_rejected_before_any_model_request(tmp_path):
    client = Client()
    with pytest.raises(ValueError):
        ExperiencePolicy(client, skill_path=tmp_path / "missing")
    assert client.calls == []


@pytest.mark.parametrize("compact", [False, True])
def test_experience_reaches_real_jev_and_deepseek_http_payloads_after_refresh(tmp_path, compact):
    """Mock HTTP requests use the actual provider serializers and validators."""
    import json
    import httpx
    from providers import DecisionClient

    path = tmp_path / "recipe.json"
    write_skill(path)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(503, json={"error": "offline gateway failure"})
        if "questions" in body:
            answers = {}
            for name, question in body["questions"].items():
                choices = list(question["criteria"])
                answers[name] = {
                    "type": "choice",
                    "choice": choices[0],
                    "confidence": 1,
                    "probabilities": {key: float(key == choices[0]) for key in choices},
                }
            return httpx.Response(200, json={"model": "typesafe/offline-test", "answers": answers})
        return httpx.Response(
            200,
            json={
                "model": "deepseek/offline-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"choice": "TYPE:node_b"})},
                    }
                ],
            },
        )

    async def scenario():
        async with DecisionClient(
            "offline-key",
            mode="cold",
            compact=compact,
            decisionStyle="heads",
            transport=httpx.MockTransport(respond),
        ) as client:
            policy = ExperiencePolicy(client, skill_path=path)
            assert (await policy.decide(snapshot(), 1))["kind"] == "refresh_required"
            with pytest.raises(ObservationRefreshRequired):
                await policy.decide(snapshot(), 1)
            assert len(requests) == 1
            fallback = await policy.decide(snapshot("本次新搜索词", "b"), 2)
            assert fallback["choice"] == "TYPE:node_b" and fallback["selectedBackend"] == "deepseek"
            assert (await policy.decide(snapshot(), 3))["selectedBackend"] == "jev"
            assert policy.summary()["modelRequests"] == 3

    asyncio.run(scenario())
    jev_state = requests[0]["state"] if compact else requests[0]["state"]["state"]
    deepseek = json.loads(requests[1]["messages"][1]["content"])
    assert jev_state["skillExperience"] == deepseek["state"]["skillExperience"]
    assert jev_state["skillExperience"]["demonstratedTransitions"] == [
        {
            "beforeKind": "search_input",
            "method": "type",
            "role": "search_field",
            "afterKind": "search_results",
            "exampleOnly": True,
        }
    ]
    assert deepseek["state"]["query"] == "本次新搜索词"
    assert deepseek["options"] == snapshot(suffix="b")["options"]
    assert requests[0]["questions"]["type_target"]["criteria"] == {"TYPE:node_a": "当前输入框"}
    assert "旧示教词" not in json.dumps(requests, ensure_ascii=False)
    assert "node_deadbeef" not in json.dumps(requests, ensure_ascii=False)


def test_experience_cannot_authorize_a_recorded_or_invented_choice(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    policy = ExperiencePolicy(Client("CLICK:node_deadbeef"), skill_path=path)
    result = asyncio.run(policy.decide(snapshot(), 1))
    assert result == {"kind": "refresh_required", "fallbackPending": True}
    assert policy.summary()["attempts"][0]["status"] == "invalid_choice"
    assert policy.summary()["hintDecisions"] == 0
