"""Launch validation must run before any device action."""

import json
import pytest
import run_pi_trial


def test_existing_trial_is_rejected_before_device_access(tmp_path, monkeypatch):
    (tmp_path / "existing").mkdir()
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    with pytest.raises(FileExistsError):
        run_pi_trial.launch("jev", "existing", base=tmp_path)


def test_explicit_device_is_required_before_phone_access(tmp_path, monkeypatch):
    monkeypatch.delenv("PHONE_BENCH_DEVICE", raising=False)
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    with pytest.raises(ValueError, match="PHONE_BENCH_DEVICE"):
        run_pi_trial.launch("jev", "new", base=tmp_path)


def test_invalid_trial_never_touches_device(tmp_path, monkeypatch):
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    with pytest.raises(ValueError):
        run_pi_trial.launch("jev", "../escape", base=tmp_path)


def test_launch_inherits_private_configuration_without_putting_key_in_metadata(
    tmp_path, monkeypatch
):
    key = "fixture-secret-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setenv("PI_BIN", "fixture-pi")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda serial: 3)
    observed = {}

    class FakeProcess:
        pid = 123

        def __init__(self, command, **kwargs):
            observed.update(command=command, **kwargs)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(run_pi_trial.subprocess, "Popen", FakeProcess)
    result = run_pi_trial.launch("jev", "safe", base=tmp_path)
    assert observed["env"]["OPENROUTER_API_KEY"] == key
    assert observed["env"]["PHONE_BENCH_OUTPUT_DIR"] == str(tmp_path)
    assert observed["command"][0] == "fixture-pi"
    assert key not in json.dumps(result)
    assert key not in (tmp_path / "safe-launch.json").read_text()
    assert result["decisionPolicyDetails"] == "single-model"
    assert "decisionTimeoutsSeconds" not in result


@pytest.mark.parametrize("name", ["PHONE_BENCH_JEV_TIMEOUT", "PHONE_BENCH_DEEPSEEK_TIMEOUT"])
def test_invalid_auto_budget_is_rejected_before_phone_reset(tmp_path, monkeypatch, name):
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setenv(name, "invalid-private-value")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    with pytest.raises(ValueError) as error:
        run_pi_trial.launch("jev-auto", "invalid-budget", base=tmp_path)
    assert str(error.value) == name


def test_auto_launch_passes_model_and_records_auditable_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setenv("PHONE_BENCH_JEV_TIMEOUT", "4")
    monkeypatch.setenv("PHONE_BENCH_DEEPSEEK_TIMEOUT", "12")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: 0)
    observed = {}

    class FakeProcess:
        pid = 123

        def __init__(self, command, **kwargs):
            observed.update(command=command, **kwargs)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(run_pi_trial.subprocess, "Popen", FakeProcess)
    result = run_pi_trial.launch("jev-auto", "auto", base=tmp_path)
    assert observed["command"][observed["command"].index("--model") + 1] == "jev-auto"
    assert result["decisionPolicyDetails"] == "jev-first-deepseek-once"
    assert result["decisionTimeoutsSeconds"] == {"jev": 4.0, "deepseek": 12.0}
    assert (
        json.loads((tmp_path / "auto-launch.json").read_text())["decisionMode"]
        == result["decisionMode"]
        == "jev-auto"
    )


def test_independent_baseline_does_not_apply_auto_deadlines(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setenv("PHONE_BENCH_JEV_TIMEOUT", "invalid-auto-only-value")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: 0)

    class FakeProcess:
        pid = 123

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(run_pi_trial.subprocess, "Popen", FakeProcess)
    assert (
        run_pi_trial.launch("deepseek", "baseline", base=tmp_path)["decisionPolicyDetails"]
        == "single-model"
    )


def test_cli_accepts_explicit_auto_mode(monkeypatch):
    selected = []
    monkeypatch.setattr(
        run_pi_trial,
        "launch",
        lambda provider, trial: selected.append(provider) or {"exitCode": 0, "status": "success"},
    )
    assert run_pi_trial.main(["jev-auto", "offline-auto"]) == 0
    assert selected == ["jev-auto"]


@pytest.mark.parametrize(
    "status,process_exit,expected",
    [
        ("success", 0, 0),
        ("failed", 0, 1),
        ("missing_result", 0, 1),
        ("success", 1, 1),
        (None, 0, 1),
    ],
)
def test_cli_exit_requires_verified_phone_task_success(monkeypatch, status, process_exit, expected):
    monkeypatch.setattr(
        run_pi_trial,
        "launch",
        lambda *a, **kw: {
            "exitCode": process_exit,
            "status": status,
        },
    )
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    assert run_pi_trial.main(["jev", "offline-status"]) == expected
