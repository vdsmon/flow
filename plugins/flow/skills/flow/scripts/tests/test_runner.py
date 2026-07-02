"""Tests for _runner.py: shared subprocess-runner factories (three contracts)."""

from __future__ import annotations

import subprocess
import sys

import pytest

import _runner


def test_default_runner_returns_completed_process(tmp_path):
    r = _runner.default_runner()
    cp = r([sys.executable, "-c", "import sys; sys.exit(0)"], tmp_path)
    assert isinstance(cp, subprocess.CompletedProcess)
    assert cp.returncode == 0


def test_default_runner_does_not_raise_on_failure(tmp_path):
    r = _runner.default_runner()
    cp = r([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path)
    assert cp.returncode == 3


def test_default_runner_captures_text(tmp_path):
    r = _runner.default_runner()
    cp = r([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"], tmp_path)
    assert isinstance(cp.stdout, str)
    assert isinstance(cp.stderr, str)
    assert "out" in cp.stdout
    assert "err" in cp.stderr


def test_default_runner_honors_cwd(tmp_path):
    r = _runner.default_runner()
    cp = r([sys.executable, "-c", "import os; print(os.getcwd())"], tmp_path)
    assert cp.stdout.strip() == str(tmp_path)


def test_cwd_default_runner_returns_completed_process(tmp_path):
    r = _runner.cwd_default_runner(tmp_path)
    cp = r([sys.executable, "-c", "import sys; sys.exit(0)"])
    assert isinstance(cp, subprocess.CompletedProcess)
    assert cp.returncode == 0


def test_cwd_default_runner_does_not_raise_on_failure(tmp_path):
    r = _runner.cwd_default_runner(tmp_path)
    cp = r([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert cp.returncode == 3


def test_cwd_default_runner_captures_text(tmp_path):
    r = _runner.cwd_default_runner(tmp_path)
    cp = r([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"])
    assert isinstance(cp.stdout, str)
    assert isinstance(cp.stderr, str)
    assert "out" in cp.stdout
    assert "err" in cp.stderr


def test_cwd_default_runner_honors_bound_cwd(tmp_path):
    r = _runner.cwd_default_runner(tmp_path)
    cp = r([sys.executable, "-c", "import os; print(os.getcwd())"])
    assert cp.stdout.strip() == str(tmp_path)


def test_kw_default_runner_accepts_keyword_cwd(tmp_path):
    r = _runner.kw_default_runner()
    cp = r([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path)
    assert cp.stdout.strip() == str(tmp_path)


def test_kw_default_runner_check_true_raises_on_failure(tmp_path):
    r = _runner.kw_default_runner()
    with pytest.raises(subprocess.CalledProcessError):
        r([sys.executable, "-c", "import sys; sys.exit(1)"], cwd=tmp_path, check=True)


def test_kw_default_runner_default_check_false_does_not_raise(tmp_path):
    r = _runner.kw_default_runner()
    cp = r([sys.executable, "-c", "import sys; sys.exit(1)"], cwd=tmp_path)
    assert cp.returncode == 1


def test_kw_default_runner_works_without_input_or_cwd():
    r = _runner.kw_default_runner()
    cp = r([sys.executable, "-c", "print('hi')"])
    assert cp.returncode == 0
    assert "hi" in cp.stdout
