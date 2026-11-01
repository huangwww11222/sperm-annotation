"""Runner lifecycle/coverage tests; no browser, service or business data is used."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from .test_browser_regression_runner import runner


def configure(tmp_path, monkeypatch, *arguments):
    module = runner()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run_browser_regression.py", *arguments])
    return module


@pytest.mark.parametrize("arguments", [
    ("--only", "typo-suite"),
    ("--critical-only", "--only", "workbench-consistency"),
])
def test_invalid_selection_does_not_create_work_or_acquire_lock(tmp_path, monkeypatch, arguments):
    module = configure(tmp_path, monkeypatch, *arguments)
    ports = iter([39001, 39002])
    monkeypatch.setattr(module, "free_port", lambda: next(ports))
    monkeypatch.setattr(module.subprocess, "check_output", lambda *_args, **_kwargs: "")
    def refuse_process(*_args, **_kwargs):
        raise AssertionError("invalid selection must not start services")
    monkeypatch.setattr(module.subprocess, "Popen", refuse_process)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code != 0
    assert not (tmp_path / "work").exists()
    assert not (tmp_path / "output").exists()


def test_setup_failure_releases_own_lock(tmp_path, monkeypatch):
    module = configure(tmp_path, monkeypatch)
    def denied_port():
        raise PermissionError("test port unavailable")
    monkeypatch.setattr(module, "free_port", denied_port)
    with pytest.raises(PermissionError, match="test port unavailable"):
        module.main()
    assert not (tmp_path / "work/browser-regression.lock").exists()


def test_existing_lock_is_never_removed(tmp_path, monkeypatch):
    module = configure(tmp_path, monkeypatch)
    lock = tmp_path / "work/browser-regression.lock"
    lock.mkdir(parents=True)
    owner = lock / "owner"
    owner.write_text("another-running-regression")
    with pytest.raises(SystemExit, match="Another browser regression"):
        module.main()
    assert owner.read_text() == "another-running-regression"


def mocked_services(module, monkeypatch):
    """Exercise the actual runner orchestration/reporting without launching processes."""
    ports = iter([39001, 39002])
    monkeypatch.setattr(module, "free_port", lambda: next(ports))
    monkeypatch.setattr(module, "wait_http", lambda *_: None)
    monkeypatch.setattr(module.subprocess, "check_output", lambda command, **_: "fixture-head\n" if "rev-parse" in command else "")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_, **__: SimpleNamespace(poll=lambda: 0))
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "fixture command passed\n")
    monkeypatch.setattr(module.subprocess, "run", run)
    return commands


@pytest.mark.parametrize("arguments,full", [
    ((), True),
    (("--only", "workbench-consistency", "--only", "independent-integrity"), False),
    (("--critical-only",), False),
])
def test_report_distinguishes_full_and_partial_success(tmp_path, monkeypatch, arguments, full):
    module = configure(tmp_path, monkeypatch, *arguments)
    commands = mocked_services(module, monkeypatch)
    module.main()
    reports = list((tmp_path / "work").glob("e2e-confirm-ux-regression-*/run.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["status"] == "passed"
    assert report["fullRegressionPassed"] is full
    assert report["requiredSuites"] == [name for name, _ in module.SUITES]
    assert report["selectedSuites"] == [item["name"] for item in report["suites"]]
    browser_commands = [command for command in commands if command[0] == "node"]
    assert len(browser_commands) == len(report["selectedSuites"])
    if full:
        assert report["scope"] == "mandatory"
        assert len(report["suites"]) == len(module.SUITES)
    else:
        assert report["scope"] != "mandatory"
        assert len(report["suites"]) < len(module.SUITES)
    assert not (tmp_path / "work/browser-regression.lock").exists()


def test_explicitly_selecting_every_suite_still_reports_diagnostic_scope(tmp_path, monkeypatch):
    names = [name for name, _ in runner().SUITES]
    arguments = tuple(value for name in names for value in ("--only", name))
    module = configure(tmp_path, monkeypatch, *arguments)
    mocked_services(module, monkeypatch)
    module.main()
    report = json.loads(next((tmp_path / "work").glob("*/run.json")).read_text())
    assert report["status"] == "passed"
    assert report["selectedSuites"] == names
    assert report["fullRegressionPassed"] is False


def test_failed_suite_preserves_required_coverage_and_releases_lock(tmp_path, monkeypatch):
    module = configure(tmp_path, monkeypatch)
    mocked_services(module, monkeypatch)
    def fail_browser(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[0] == "node" else 0, "injected regression failure\n")
    monkeypatch.setattr(module.subprocess, "run", fail_browser)
    with pytest.raises(RuntimeError, match="critical-workflows failed"):
        module.main()
    report = json.loads(next((tmp_path / "work").glob("*/run.json")).read_text())
    assert report["status"] == "failed"
    assert report["fullRegressionPassed"] is False
    assert len(report["requiredSuites"]) == len(module.SUITES)
    assert report["suites"] == [{"name": "critical-workflows", "status": "failed"}]
    assert not (tmp_path / "work/browser-regression.lock").exists()


def test_cleanup_failure_never_records_full_pass_and_releases_lock(tmp_path, monkeypatch):
    module = configure(tmp_path, monkeypatch)
    mocked_services(module, monkeypatch)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_, **__: SimpleNamespace(poll=lambda: None, pid=12345))
    def fail_cleanup(*_args):
        raise PermissionError("injected cleanup failure")
    monkeypatch.setattr(module.os, "killpg", fail_cleanup)
    with pytest.raises(PermissionError, match="injected cleanup failure"):
        module.main()
    report = json.loads(next((tmp_path / "work").glob("*/run.json")).read_text())
    assert len(report["suites"]) == len(module.SUITES)
    assert report["fullRegressionPassed"] is False
    assert not (tmp_path / "work/browser-regression.lock").exists()
