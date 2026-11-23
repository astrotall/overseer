from __future__ import annotations

import uuid
from pathlib import PureWindowsPath

import pytest
from pydantic import ValidationError

from apps.executor import paths as paths_module
from apps.executor.config import ExecutorSettings
from apps.executor.paths import (
    PathNotAllowedError,
    PathPolicy,
    build_path_policy,
    default_root,
    local_path,
)
from libs.core.exceptions import ConfigurationError, OfficeInputError
from tests.executor_support import (
    ALLOWED_ROOT,
    FakeExcelBackend,
    excel_dispatcher,
    open_request,
    save_request,
)

ROOT = ALLOWED_ROOT
INSIDE = f"{ROOT}\\report.xlsx"


@pytest.fixture
def policy() -> PathPolicy:
    return PathPolicy([ROOT])


@pytest.mark.parametrize(
    "path",
    [
        INSIDE,
        f"{ROOT}\\sub\\deeper\\report.xlsm",
        "c:\\users\\ME\\documents\\overseer\\report.xlsx",
        "C:/Users/me/Documents/Overseer/report.xlsx",
        f"{ROOT}\\.\\report.xlsx",
        f"{ROOT}\\PROGRA~1\\report.xlsx",
        f"{ROOT}\\console.xlsx",
        f"{ROOT}\\com10.xlsx",
    ],
)
def test_paths_inside_the_allowed_folder_pass(policy: PathPolicy, path: str) -> None:
    assert PureWindowsPath(policy.check(path)) == PureWindowsPath(path)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("\\\\server\\share\\Overseer\\report.xlsx", id="unc"),
        pytest.param("//server/share/report.xlsx", id="unc-forward-slashes"),
        pytest.param(f"\\\\?\\{INSIDE}", id="extended"),
        pytest.param(f"\\\\.\\{INSIDE}", id="device"),
        pytest.param("\\\\?\\UNC\\server\\share\\report.xlsx", id="extended-unc"),
        pytest.param(f"\\??\\{INSIDE}", id="nt-namespace"),
        pytest.param(f"{INSIDE}:hidden", id="ads"),
        pytest.param(f"{INSIDE}::$DATA", id="ads-default-stream"),
        pytest.param(f"{ROOT}\\folder:stream\\report.xlsx", id="ads-in-a-folder"),
        pytest.param(f"{ROOT}\\CON.xlsx", id="con"),
        pytest.param(f"{ROOT}\\nul.xlsx", id="nul"),
        pytest.param(f"{ROOT}\\COM1.xlsx", id="com1"),
        pytest.param(f"{ROOT}\\lpt¹.xlsx", id="lpt-superscript"),
        pytest.param(f"{ROOT}\\CONIN$.xlsx", id="conin"),
        pytest.param(f"{ROOT}\\aux .xlsx", id="aux-with-space"),
        pytest.param(f"{ROOT}\\con\\report.xlsx", id="device-folder"),
        pytest.param("report.xlsx", id="relative"),
        pytest.param("Documents\\Overseer\\report.xlsx", id="relative-folder"),
        pytest.param("C:report.xlsx", id="drive-relative"),
        pytest.param("\\Users\\me\\Documents\\Overseer\\report.xlsx", id="root-relative"),
        pytest.param(f"{ROOT}\\..\\secret.xlsx", id="dotdot"),
        pytest.param(f"{ROOT}\\sub\\..\\..\\secret.xlsx", id="dotdot-deeper"),
        pytest.param(f"{INSIDE}.", id="trailing-dot"),
        pytest.param(f"{ROOT}\\sub \\report.xlsx", id="trailing-space-folder"),
        pytest.param(f"{ROOT}\\re<po>rt.xlsx", id="angle-brackets"),
        pytest.param(f"{ROOT}\\re|port.xlsx", id="pipe"),
        pytest.param(f"{ROOT}\\re\x01port.xlsx", id="control-char"),
        pytest.param("C:\\Users\\me\\Desktop\\report.xlsx", id="outside"),
        pytest.param("D:\\Documents\\Overseer\\report.xlsx", id="other-drive"),
        pytest.param("C:\\Users\\me\\Documents\\OverseerEvil\\report.xlsx", id="prefix-sibling"),
        pytest.param("C:\\Users\\me\\Documents\\report.xlsx", id="parent"),
    ],
)
def test_paths_outside_or_unsafe_are_refused(policy: PathPolicy, path: str) -> None:
    with pytest.raises(PathNotAllowedError) as caught:
        policy.check(path)

    assert caught.value.code == "path_not_allowed"
    assert path not in str(caught.value)


@pytest.mark.parametrize(
    "path",
    [f"{ROOT}\\report.docx", f"{ROOT}\\report", f"{ROOT}\\{'a' * 220}.xlsx"],
)
def test_the_controller_rules_still_apply_inside_the_folder(policy: PathPolicy, path: str) -> None:
    with pytest.raises(OfficeInputError):
        policy.check(path)


def test_a_link_escaping_the_folder_is_caught_after_resolution() -> None:
    links = {f"{ROOT}\\link\\report.xlsx": "C:\\Windows\\System32\\report.xlsx"}
    policy = PathPolicy([ROOT], resolver=lambda path: links.get(path, path))

    assert policy.check(INSIDE) == INSIDE
    with pytest.raises(PathNotAllowedError) as caught:
        policy.check(f"{ROOT}\\link\\report.xlsx")
    assert caught.value.reason == "escapes_after_resolution"


@pytest.mark.parametrize(
    "resolved",
    [
        pytest.param("\\\\?\\UNC\\server\\share\\report.xlsx", id="to-a-share"),
        pytest.param("\\\\server\\share\\report.xlsx", id="to-a-share-plain"),
        pytest.param(f"{ROOT}\\report.xlsx:stream", id="to-a-stream"),
    ],
)
def test_resolution_into_forbidden_shapes_is_refused(resolved: str) -> None:
    policy = PathPolicy([ROOT], resolver=lambda path: resolved if path == INSIDE else path)

    with pytest.raises(PathNotAllowedError):
        policy.check(INSIDE)


def test_an_extended_prefix_from_the_resolver_is_understood() -> None:
    policy = PathPolicy([ROOT], resolver=lambda path: f"\\\\?\\{path}")

    assert policy.check(INSIDE) == INSIDE


def test_a_root_that_is_itself_a_junction_is_compared_resolved() -> None:
    target = "D:\\Data\\Overseer"

    def resolve(path: str) -> str:
        pure = PureWindowsPath(path)
        root = PureWindowsPath(ROOT)
        if pure == root or pure.is_relative_to(root):
            return str(PureWindowsPath(target, pure.relative_to(root)))
        return path

    policy = PathPolicy([ROOT], resolver=resolve)

    assert policy.check(INSIDE) == INSIDE


@pytest.mark.parametrize(
    "root",
    ["\\\\server\\share", "Documents", "C:\\Users\\..\\Windows", "C:\\con", "\\\\?\\C:\\x"],
)
def test_unsafe_roots_are_refused_at_startup(root: str) -> None:
    with pytest.raises(ConfigurationError):
        PathPolicy([root])
    with pytest.raises(ValidationError):
        ExecutorSettings(allowed_roots=[root])


def test_at_least_one_root_is_required() -> None:
    with pytest.raises(ConfigurationError):
        PathPolicy([])


def test_the_default_root_is_a_dedicated_folder_in_the_documents() -> None:
    root = default_root({"USERPROFILE": "C:\\Users\\me"})

    assert root == PureWindowsPath("C:\\Users\\me\\Documents\\Overseer")


@pytest.mark.parametrize("profile", [None, "", "\\\\server\\profiles\\me", "relative"])
def test_without_a_usable_profile_the_default_root_is_refused(profile: str | None) -> None:
    environ = {} if profile is None else {"USERPROFILE": profile}

    with pytest.raises(ConfigurationError):
        default_root(environ)


def test_on_windows_the_default_folder_is_created_and_configured_ones_must_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []
    existing = {"C:\\Users\\me\\Documents\\Overseer"}
    monkeypatch.setattr(paths_module.os, "makedirs", lambda path, exist_ok: created.append(path))
    monkeypatch.setattr(paths_module.os.path, "isdir", lambda path: path in existing)

    policy = build_path_policy([], environ={"USERPROFILE": "C:\\Users\\me"}, on_windows=True)
    assert created == ["C:\\Users\\me\\Documents\\Overseer"]
    assert policy.roots == (PureWindowsPath("C:\\Users\\me\\Documents\\Overseer"),)

    with pytest.raises(ConfigurationError):
        build_path_policy(["D:\\Missing"], on_windows=True)
    assert created == ["C:\\Users\\me\\Documents\\Overseer"]


def test_off_windows_nothing_is_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        paths_module.os, "makedirs", lambda *_: pytest.fail("вне Windows папки не создаются")
    )

    policy = build_path_policy([ROOT], on_windows=False)

    assert policy.roots == (PureWindowsPath(ROOT),)


def test_local_path_rejects_non_strings() -> None:
    assert local_path(None) is None  # type: ignore[arg-type]  # проверка рантайм-входа


async def test_the_dispatcher_refuses_a_path_before_excel_sees_it() -> None:
    backend = FakeExcelBackend()
    dispatcher, _ = excel_dispatcher(backend, roots=[ROOT])
    await dispatcher.start()
    conversation = uuid.uuid4()
    try:
        with pytest.raises(PathNotAllowedError):
            await dispatcher.dispatch(
                open_request("\\\\server\\share\\x.xlsx", conversation_id=conversation)
            )
        await dispatcher.dispatch(open_request(None, conversation_id=conversation))
        with pytest.raises(PathNotAllowedError):
            await dispatcher.dispatch(
                save_request("C:\\Windows\\x.xlsx", conversation_id=conversation)
            )
    finally:
        await dispatcher.aclose()

    assert "open_workbook" not in backend.names
    assert "save_as" not in backend.names
    assert "path_exists" not in backend.names
