"""Conversation follow-ups must not create a phone task or hide missing evidence."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

MODULE = Path(__file__).with_name('terminal-conversation.ts')


def route(goal, previous=None):
    script = f'''import {{conversationRoute}} from {json.dumps(MODULE.as_uri())};
console.log(JSON.stringify(conversationRoute({json.dumps(goal)}, {json.dumps(previous)}) ?? null));'''
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def previous(status='success'):
    return {'goal': '在小红书搜索“露营装备”，打开第一篇，只收藏',
            'result': {'status': status, 'elapsedMs': 17947.5, 'piElapsedMs': 18100.8}}


def test_duration_question_uses_previous_pi_elapsed_without_phone_access():
    value = route('你得告诉我大概花费了多久呀？', previous())
    assert value['kind'] == 'reply'
    assert '18.1 秒' in value['text'] and '17.9 秒' in value['text']


def test_missing_timing_does_not_claim_zero_seconds():
    value = route('花了多久？', {'goal': '读取手机屏幕', 'result': {'status': 'success'}})
    assert '没有' in value['text'] and '0.0' not in value['text']


def test_repeat_reuses_exact_previous_successful_goal():
    assert route('再来一次吧', previous()) == {'kind': 'repeat', 'goal': previous()['goal']}


@pytest.mark.parametrize('prior', [None, previous('failed')])
def test_repeat_without_verified_success_does_not_retry_uncertain_actions(prior):
    assert route('再来一次吧', prior)['kind'] == 'reply'


@pytest.mark.parametrize('goal', ['打开小红书并告诉我花了多久', '再来一次，但改成点赞', '小红书搜索“多久”'])
def test_new_or_changed_task_is_not_interpreted_as_local_followup(goal):
    assert route(goal, previous()) is None


def test_duration_without_previous_task_has_honest_reply():
    assert '没有' in route('耗时多少？')['text']


@pytest.mark.skipif(shutil.which('pi') is None, reason='PI CLI not installed')
def test_real_pi_restores_timing_and_repeat_without_starting_worker_for_question(tmp_path):
    from test_phone_extension_lifecycle import WORKER

    worker = tmp_path / 'worker.py'
    worker.write_text(WORKER.replace('"status":"success",', '"status":"success","elapsedMs":17947.5,"piElapsedMs":18100.8,'))
    log = tmp_path / 'worker.jsonl'
    command = [
        'pi', '--no-extensions', '-e', str(MODULE.with_name('terminal-extension.ts')),
        '--no-mcp', '--no-skills', '--no-context-files', '--no-builtin-tools',
        '--tools', 'phone_observe,phone_action', '--provider', 'phone-control', '--model', 'auto',
        '--offline', '--thinking', 'off', '--session', str(tmp_path / 'session.jsonl'),
        '--mode', 'json', '-p',
    ]
    env = {**os.environ, 'PI_OFFLINE': '1', 'PHONE_BENCH_PYTHON': os.sys.executable,
           'PHONE_BENCH_WORKER': str(worker), 'LIFECYCLE_LOG': str(log)}
    for goal in ('读取当前手机屏幕', '你得告诉我大概花费了多久呀？', '再来一次吧'):
        process = subprocess.run(command + [goal], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
        assert process.returncode == 0, process.stderr
        assert '18.1 秒' in process.stdout
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(calls) == (4 if goal == '再来一次吧' else 2)
    assert [call['command'] for call in calls] == ['observe', 'finish'] * 2
    assert all(call['goal'] == '读取当前手机屏幕' for call in calls)
    assert len({call['trial'] for call in calls}) == 2


@pytest.mark.skipif(shutil.which('pi') is None, reason='PI CLI not installed')
def test_real_pi_reports_unsupported_goal_without_uncertain_delivery(tmp_path):
    command = [
        'pi', '--no-extensions', '-e', str(MODULE.with_name('terminal-extension.ts')),
        '--no-mcp', '--no-skills', '--no-context-files', '--no-builtin-tools',
        '--tools', 'phone_observe,phone_action', '--provider', 'phone-control', '--model', 'auto',
        '--offline', '--thinking', 'off', '--mode', 'json', '-p', '你好，给我讲个故事',
    ]
    env = {**os.environ, 'PI_OFFLINE': '1', 'PHONE_BENCH_PYTHON': os.sys.executable,
           'PHONE_BENCH_OUTPUT_DIR': str(tmp_path / 'results')}
    env.pop('PHONE_BENCH_WORKER', None)
    process = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
    assert process.returncode == 0, process.stderr
    assert '不在当前手机控制器支持范围内' in process.stdout
    assert '未执行手机操作' in process.stdout
    assert 'action delivery may be uncertain' not in process.stdout + process.stderr
    results = list((tmp_path / 'results').glob('*/result.json'))
    assert len(results) == 1
    evidence = json.loads(results[0].read_text())
    assert evidence['errorDetails']['code'] == 'UNSUPPORTED_TASK'
    assert evidence['operations'] == [] and evidence['modelCalls'] == []
