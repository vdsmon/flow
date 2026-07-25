"""Tests for _runner.py: shared subprocess-runner factories (three contracts)."""

from __future__ import annotations

import subprocess
import sys

import pytest

import _runner


# default_runner takes cwd positionally per call; cwd_default_runner binds it at
# construction. Same subprocess.run call underneath, so one contract, two entry points.
def _invoke_positional_cwd(argv, cwd):
    return _runner.default_runner()(argv, cwd)


def _invoke_bound_cwd(argv, cwd):
    return _runner.cwd_default_runner(cwd)(argv)


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(_invoke_positional_cwd, id="positional_cwd"),
        pytest.param(_invoke_bound_cwd, id="bound_cwd"),
    ],
)
def test_runner_contract(tmp_path, invoke):
    cp = invoke([sys.executable, "-c", "import sys; sys.exit(0)"], tmp_path)
    assert isinstance(cp, subprocess.CompletedProcess)
    assert cp.returncode == 0

    cp = invoke([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path)
    assert cp.returncode == 3

    cp = invoke(
        [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"], tmp_path
    )
    assert isinstance(cp.stdout, str)
    assert isinstance(cp.stderr, str)
    assert "out" in cp.stdout
    assert "err" in cp.stderr

    cp = invoke([sys.executable, "-c", "import os; print(os.getcwd())"], tmp_path)
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
