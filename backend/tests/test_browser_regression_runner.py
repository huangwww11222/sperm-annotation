"""Regression evidence must retain real fault stacks without test credentials."""
import importlib.util
from pathlib import Path


def runner():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'run_browser_regression.py'
    spec = importlib.util.spec_from_file_location('browser_regression_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backend_failure_and_rotated_logs_are_preserved_without_tokens(tmp_path):
    logs = tmp_path / 'data' / 'logs'
    logs.mkdir(parents=True)
    (logs / 'review.log').write_text('annotation.tracking_read_failed\nFileNotFoundError: tracker_results.json\neyJheader.eyJbody.signature\n')
    (logs / 'review.log.1').write_text('tracking.branch_replay actor=3\n')
    (logs / 'unrelated.txt').write_text('must not be copied')
    runner().preserve_review_logs(tmp_path)
    evidence = (tmp_path / 'review.log').read_text()
    assert 'FileNotFoundError: tracker_results.json' in evidence
    assert '[redacted-test-token]' in evidence and 'eyJheader' not in evidence
    assert (tmp_path / 'review.1.log').read_text() == 'tracking.branch_replay actor=3\n'
    assert not (tmp_path / 'unrelated.txt').exists()


def test_missing_optional_backend_log_does_not_mask_regression_result(tmp_path):
    runner().preserve_review_logs(tmp_path)
    assert list(tmp_path.iterdir()) == []
