from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apps.executor.paths import PathNotAllowedError, PathPolicy, platform_resolver

pytestmark = pytest.mark.windows


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    allowed = tmp_path / "Overseer"
    outside = tmp_path / "Outside"
    allowed.mkdir()
    outside.mkdir()
    return allowed, outside


def policy(root: Path) -> PathPolicy:
    resolver = platform_resolver()
    assert resolver is not None
    return PathPolicy([str(root)], resolver=resolver)


def test_a_junction_out_of_the_folder_is_refused(layout: tuple[Path, Path]) -> None:
    allowed, outside = layout
    link = allowed / "escape"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True)

    with pytest.raises(PathNotAllowedError):
        policy(allowed).check(str(link / "report.xlsx"))


def test_a_symlinked_file_out_of_the_folder_is_refused(layout: tuple[Path, Path]) -> None:
    allowed, outside = layout
    target = outside / "report.xlsx"
    target.write_bytes(b"")
    link = allowed / "report.xlsx"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("создание симлинков требует режима разработчика или прав администратора")

    with pytest.raises(PathNotAllowedError):
        policy(allowed).check(str(link))


def test_case_differences_on_a_real_filesystem_still_match(layout: tuple[Path, Path]) -> None:
    allowed, _ = layout

    checked = policy(allowed).check(str(allowed).upper() + "\\report.xlsx")

    assert checked.casefold() == (str(allowed) + "\\report.xlsx").casefold()


def test_a_root_that_is_a_junction_accepts_paths_through_it(layout: tuple[Path, Path]) -> None:
    allowed, outside = layout
    root_link = allowed.parent / "Linked"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root_link), str(outside)], check=True)

    assert policy(root_link).check(str(root_link / "report.xlsx"))
