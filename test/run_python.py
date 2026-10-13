"""Keep unexpected coverage gaps fatal, with explicit local-runtime exceptions."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import unittest

MANAGED_LAYA = Path.home() / '.local/share/orc/laya/bin/python'
SANDBOX_PROBE = ('codex_permissions_test.CodexPermissionsTest.'
                 'test_real_sandbox_allows_git_in_repos_and_worktrees_but_protects_other_paths')
BROWSER_PROBE = ('codex_permissions_test.CodexPermissionsTest.'
                 'test_real_browser_reader_binds_loopback_and_writes_temp_but_not_the_workspace')
SANDBOX_PROBES = {SANDBOX_PROBE, BROWSER_PROBE}
AGY_PROFILE_PROBE = 'agy_sandbox_test.AgySandboxTest.test_real_profile_writes_workspace_and_run_temp_only'


def sandbox_exec_applies():
    """Whether sandbox-exec can apply a profile here; it cannot inside another sandbox."""
    try:
        return subprocess.run(['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)', '/usr/bin/true'],
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def expected_skip(test, reason):
    if test.id() == AGY_PROFILE_PROBE:
        return reason == 'sandbox-exec cannot apply a profile here' and (sys.platform != 'darwin' or not sandbox_exec_applies())
    if test.id() not in SANDBOX_PROBES:
        return False
    if reason == 'requires the macOS Codex sandbox':
        return sys.platform != 'darwin' or not shutil.which('codex')
    return reason == 'installed Codex does not support named permission profiles'


def check_result(result, stream=sys.stderr):
    valid = result.wasSuccessful() and result.testsRun > 0 and not result.expectedFailures
    for test, reason in result.skipped:
        expected = expected_skip(test, reason)
        print(f"{'Expected integration skip' if expected else 'UNEXPECTED SKIP'}: {test.id()}: {reason}", file=stream)
        valid = valid and expected
    if not valid:
        print('Python suite failed, ran no tests, or has unexpected missing coverage.', file=stream)
    return valid


def record_runtimes(module):
    """Record every interpreter the Laya runtime resolves during the run."""
    resolved, original = [], module.runtime_python

    def runtime_python(options):
        python = original(options)
        resolved.append(python)
        return python
    module.runtime_python = runtime_python
    return resolved


def managed_runtimes(resolved, stream=sys.stderr):
    managed = sorted({python for python in resolved if Path(python).expanduser().absolute() == MANAGED_LAYA})
    for python in managed:
        print(f'Tests resolved the installed Laya runtime: {python}', file=stream)
    return managed


if __name__ == '__main__':
    os.environ['FUSION_LAYA_PYTHON'] = shutil.which('false') or '/usr/bin/false'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import fusion_decisions
    resolved = record_runtimes(fusion_decisions)
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='*_test.py')
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if check_result(result) and not managed_runtimes(resolved) else 1)
