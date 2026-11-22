from __future__ import annotations

import subprocess
import sys

import pytest

from apps.executor.com import NOT_WINDOWS_TEXT, PYWIN32_MISSING_TEXT, load_pywin32
from libs.core.exceptions import ConfigurationError

CI_SAFE_MODULES = [
    "apps.executor",
    "apps.executor.com",
    "apps.executor.sta",
    "apps.executor.excel",
    "apps.executor.excel.validation",
    "apps.executor.excel.backend",
    "apps.executor.excel.controller",
    "apps.executor.excel.com_backend",
]
PYWIN32_PACKAGES = (
    "win32com",
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32event",
    "win32process",
    "win32con",
)

PROBE = """
import sys

blocked = {packages}
for name in list(sys.modules):
    if name.split(".")[0] in blocked:
        raise SystemExit("preloaded: " + name)


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise SystemExit("imported: " + name)
        return None


sys.meta_path.insert(0, Blocker())
__import__({module!r})
{extra}
"""

CONSTRUCT_BACKEND = """
from apps.executor.excel.com_backend import Win32ExcelBackend
from apps.executor.excel.controller import ExcelController

ExcelController(Win32ExcelBackend())
"""


def _probe(module: str, extra: str = "") -> subprocess.CompletedProcess[str]:
    source = PROBE.format(packages=repr(set(PYWIN32_PACKAGES)), module=module, extra=extra)
    return subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("module", CI_SAFE_MODULES)
def test_module_imports_without_pywin32(module: str) -> None:
    result = _probe(module)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_real_backend_and_controller_are_constructed_without_touching_pywin32() -> None:
    result = _probe("apps.executor.excel.com_backend", CONSTRUCT_BACKEND)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_loading_pywin32_off_windows_is_a_configuration_error(platform: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_pywin32(platform)

    assert str(raised.value) == NOT_WINDOWS_TEXT.format(platform=platform)


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows pywin32 может быть установлен")
def test_a_missing_pywin32_names_the_dependency_group() -> None:
    with pytest.raises(ConfigurationError) as raised:
        load_pywin32("win32")

    assert str(raised.value) == PYWIN32_MISSING_TEXT
    assert "uv sync --group executor" in str(raised.value)
