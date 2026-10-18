"""The installed PI entry routes varied goals to model-guided experience use."""

import asyncio
import io
import json

import pytest

from terminal_launcher import build_launch
from terminal_tasks import resolve_task
from test_worker_policy import ModelDouble, PhoneDouble, api_failure
import worker


GOAL = '在小红书搜索“露营装备”，打开第一篇，只收藏'


def test_new_goal_routes_to_experience_without_changing_the_user_target():
    task = resolve_task(GOAL)
    assert task['kind'] == 'experience'
    assert task['query'] == '露营装备'
    assert task['target'] == {'kind': 'first'}
    assert task['desired'] == {'collected': True}
    assert task['restoreAfter'] is False


def test_launcher_allows_unrecorded_model_control_and_discards_stale_recipe(tmp_path):
    mcp = tmp_path / 'mcp.json'
    mcp.write_text(json.dumps({'mcpServers': {'phone': {'url': 'http://phone:8080/mcp'}}}))
    _, env = build_launch(
        {'mcpConfig': str(mcp), 'mcpServer': 'phone', 'python': '/fixture/python'},
        [], {'PHONE_BENCH_RECIPE': '/stale/recipe.json'},
    )
    assert env.get('PHONE_BENCH_RECIPE', '') == ''
    assert env['PHONE_EXPERIENCE_ENABLED'] == '1'


def test_experience_runtime_failure_keeps_safe_diagnostic():
    from experience_runtime import ExperienceRuntimeError

    message, details = worker.safe_error(ExperienceRuntimeError('TARGET_CHANGED'), 'act')
    assert details == {
        'exceptionType': 'ExperienceRuntimeError', 'stage': 'experience_observation',
        'code': 'TARGET_CHANGED',
    }
    assert message == ExperienceRuntimeError.MESSAGES['TARGET_CHANGED']


@pytest.mark.parametrize('enabled', [True, False])
def test_actual_worker_uses_same_model_loop_with_or_without_experience(tmp_path, monkeypatch, enabled):
    from experience_tasks import parse_experience_task

    task = parse_experience_task(GOAL)
    monkeypatch.setenv('PHONE_TERMINAL_TASK', json.dumps(task, ensure_ascii=False))
    monkeypatch.setenv('PHONE_EXPERIENCE_ENABLED', '1' if enabled else '0')
    monkeypatch.delenv('PHONE_BENCH_RECIPE', raising=False)
    PhoneDouble.instances, ModelDouble.instances = [], []
    ModelDouble.outcomes = []
    output = io.StringIO()
    requests = [
        {'command': 'observe'}, {'command': 'decide'},
        {'command': 'act', 'choice': 'CLICK:node-1'},
        {'command': 'finish', 'piElapsedMs': 21},
    ]
    asyncio.run(worker.serve(
        'terminal-auto', 'experience-entry',
        input_stream=io.StringIO('\n'.join(json.dumps({'id': i, **r}) for i, r in enumerate(requests))),
        output_stream=output, runtime_factory=PhoneDouble, client_factory=ModelDouble,
        key_reader=lambda: 'fixture-key', base=tmp_path,
    ))
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert not any('error' in reply for reply in replies)
    result = replies[-1]['result']
    assert result['provider'] == 'experience-auto'
    assert result['terminalTask'] == task
    assert result['decisionPolicy']['recipeHits'] == 0
    assert ModelDouble.instances[0].calls == [('jev', 1)]
    assert PhoneDouble.instances[0].actions == ['CLICK:node-1']


def test_experience_worker_refreshes_before_deepseek_and_keeps_recorded_hints(tmp_path, monkeypatch):
    from experience_tasks import parse_experience_task
    from test_terminal_entry import profile

    settings = profile(tmp_path)
    monkeypatch.setenv('PHONE_TERMINAL_TASK', json.dumps(parse_experience_task(GOAL)))
    monkeypatch.setenv('PHONE_BENCH_RECIPE', settings['recipe'])
    monkeypatch.setenv('PHONE_EXPERIENCE_ENABLED', '1')
    PhoneDouble.instances, ModelDouble.instances = [], []
    ModelDouble.outcomes = [api_failure(), None]
    output = io.StringIO()
    commands = [
        {'command': 'observe'}, {'command': 'decide'}, {'command': 'observe'},
        {'command': 'decide'}, {'command': 'act', 'choice': 'CLICK:node-2'},
        {'command': 'finish'},
    ]
    asyncio.run(worker.serve(
        'terminal-auto', 'experience-fallback',
        input_stream=io.StringIO('\n'.join(json.dumps({'id': i, **r}) for i, r in enumerate(commands))),
        output_stream=output, runtime_factory=PhoneDouble, client_factory=ModelDouble,
        key_reader=lambda: 'fixture-key', base=tmp_path,
    ))
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[1]['result']['kind'] == 'refresh_required'
    assert replies[3]['result']['selectedBackend'] == 'deepseek'
    assert ModelDouble.instances[0].calls == [('jev', 1), ('deepseek', 2)]
    assert PhoneDouble.instances[0].actions == ['CLICK:node-2']
    policy = replies[-1]['result']['decisionPolicy']
    assert policy['experienceLoaded'] is True
    assert policy['modelRequests'] == 2
    assert policy['hintDecisions'] == 1
    assert policy['recipeHits'] == 0
