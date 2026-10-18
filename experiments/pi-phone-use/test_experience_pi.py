"""Real PI extension/worker/policy loop with explicit phone/model test doubles."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_terminal_entry import profile


@pytest.mark.skipif(shutil.which('pi') is None, reason='PI CLI not installed')
@pytest.mark.parametrize('enabled', [True, False])
def test_real_pi_uses_current_task_and_optional_experience_each_model_step(tmp_path, enabled):
    settings = profile(tmp_path)
    audit = tmp_path / 'audit.json'
    child = tmp_path / 'experience-child.py'
    child.write_text('''import asyncio,json,os
from pathlib import Path
import worker
from test_worker_policy import PhoneDouble,ModelDouble
from terminal_tasks import resolve_task
task=resolve_task(os.environ['PHONE_TERMINAL_GOAL'])
class Phone(PhoneDouble):
 def observe(self):
  value=super().observe()
  value['state'].update(goal=task['goal'],task=task,stage='collect')
  return value
 def act(self,choice):
  self.actions.append(choice)
  self.completed=len(self.actions)==2
  return self.observe()
 def finish(self,extra=None):
  result=super().finish(extra)
  Path(os.environ['EXPERIENCE_TEST_AUDIT']).write_text(json.dumps(result))
  return result
class Client(ModelDouble):
 async def decide(self,backend,state,options):
  assert state['task']['query']=='露营装备'
  assert state['task']['desired']=={'collected':True}
  assert state['task']['restoreAfter'] is False
  assert ('skillExperience' in state)==(os.environ['PHONE_EXPERIENCE_ENABLED']=='1')
  assert '小狮日记' not in json.dumps(state,ensure_ascii=False)
  return await super().decide(backend,state,options)
asyncio.run(worker.main(client_factory=Client,runtime_factory=Phone,key_reader=lambda:'fixture-key'))
''')
    env = {
        **os.environ,
        'PYTHONPATH': str(Path(__file__).resolve().parent),
        'PHONE_BENCH_PYTHON': os.sys.executable,
        'PHONE_BENCH_WORKER': str(child),
        'PHONE_BENCH_RECIPE': settings['recipe'],
        'PHONE_EXPERIENCE_ENABLED': '1' if enabled else '0',
        'EXPERIENCE_TEST_AUDIT': str(audit),
        'PI_OFFLINE': '1',
    }
    result = subprocess.run([
        'pi', '--no-extensions', '-e', str(Path(__file__).with_name('terminal-extension.ts')),
        '--no-mcp', '--no-skills', '--no-context-files', '--no-builtin-tools',
        '--tools', 'phone_observe,phone_action', '--provider', 'phone-control', '--model', 'auto',
        '--offline', '--thinking', 'off', '--mode', 'json', '-p',
        '在小红书搜索“露营装备”，打开第一篇，只收藏',
    ], env=env, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0
    assert '每步实时判断' in result.stdout
    evidence = json.loads(audit.read_text())
    assert evidence['status'] == 'success'
    assert evidence['provider'] == 'experience-auto'
    assert evidence['terminalTask']['query'] == '露营装备'
    policy = evidence['decisionPolicy']
    assert policy['recipeHits'] == 0
    assert policy['experienceLoaded'] is enabled
    assert policy['modelRequests'] == 2
    assert policy['hintDecisions'] == 2 * int(enabled)
