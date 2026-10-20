"""Registry read/write failures must not silently erase existing accounts."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aistudio_api.infrastructure.account import account_store as module, storage_state


@pytest.fixture
def store_files(tmp_path, monkeypatch):
    # Never discover or migrate a real legacy session during an isolated test.
    monkeypatch.setattr(module, "_resolve_legacy_auth_file", lambda: None)
    accounts = tmp_path / "synthetic-accounts"
    accounts.mkdir()
    registry_path = accounts / "registry.json"
    registry_path.write_text('{"accounts":{},"active_account_id":null}', encoding="utf-8")
    store = module.AccountStore(accounts)
    for account_id in ("first", "second"):
        store.save_account(
            name=account_id, email=None, account_id=account_id,
            storage_state={"cookies": [], "origins": []},
        )
    return store, registry_path


@pytest.mark.parametrize("invalid", [
    '{"accounts": "SYNTHETIC_PRIVATE_DATA"}',
    '{"accounts": {"first": null}}',
    '{"accounts": {"first": {"id": "second"}}}',
    '{"accounts": {',
])
def test_invalid_registry_blocks_import_without_overwriting_existing_bytes(store_files, invalid):
    store, path = store_files
    path.write_text(invalid, encoding="utf-8")
    store._registry = None
    with pytest.raises(RuntimeError, match="Account registry is unavailable") as caught:
        store.save_account(
            name="new", email=None, account_id="new",
            storage_state={"cookies": [], "origins": []},
        )
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert path.read_text(encoding="utf-8") == invalid
    assert not (path.parent / "new").exists()
    assert store._registry is None


def test_transient_registry_read_error_does_not_cache_an_empty_registry(store_files, monkeypatch):
    store, path = store_files
    original = path.read_bytes()
    store._registry = None
    read_text = Path.read_text

    def fail_read(target, *args, **kwargs):
        if target == path:
            raise PermissionError("SYNTHETIC_PRIVATE_IO_FAILURE")
        return read_text(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail_read)
        with pytest.raises(RuntimeError, match="Account registry is unavailable") as caught:
            store.list_accounts()
        assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert {account.id for account in store.list_accounts()} == {"first", "second"}
    assert path.read_bytes() == original


@pytest.mark.parametrize("operation", ["rename", "email", "activate", "delete"])
def test_failed_registry_commit_keeps_published_state_and_account_credentials(
    store_files, monkeypatch, operation,
):
    store, path = store_files
    before = [account.to_dict() for account in store.list_accounts()]
    original = path.read_bytes()
    auth_path = path.parent / "first" / "auth.json"
    auth_before = auth_path.read_bytes()
    meta_path = auth_path.with_name("meta.json")
    meta_before = meta_path.read_bytes()
    atomic_write = module._atomic_write_json

    def fail_registry(target, data):
        if target == path:
            raise OSError("SYNTHETIC_REGISTRY_WRITE_FAILURE")
        atomic_write(target, data)

    monkeypatch.setattr(module, "_atomic_write_json", fail_registry)
    with pytest.raises(OSError, match="SYNTHETIC_REGISTRY_WRITE_FAILURE"):
        if operation == "rename":
            store.update_account("first", "new name")
        elif operation == "email":
            store.update_account_email("first", "offline@example.invalid")
        elif operation == "activate":
            store.set_active_account("second")
        else:
            store.delete_account("first")
    assert [account.to_dict() for account in store.list_accounts()] == before
    assert path.read_bytes() == original
    assert auth_path.read_bytes() == auth_before
    assert meta_path.read_bytes() == meta_before
    assert store.get_active_account().id == json.loads(original)["active_account_id"]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure_file", ["meta.json", "registry.json"])
def test_import_write_failure_preserves_original_session_and_metadata(
    store_files, monkeypatch, existing, failure_file,
):
    store, registry = store_files
    account_id = "first" if existing else "new"
    account_dir = registry.parent / account_id
    paths = [account_dir / "auth.json", account_dir / "meta.json"]
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    registry_before = registry.read_bytes()
    accounts_before = [account.to_dict() for account in store.list_accounts()]
    atomic_write = module._atomic_write_json

    def fail_file(target, data):
        if target.name == failure_file:
            raise OSError("SYNTHETIC_IMPORT_WRITE_FAILURE")
        atomic_write(target, data)

    monkeypatch.setattr(module, "_atomic_write_json", fail_file)
    with pytest.raises(OSError, match="SYNTHETIC_IMPORT_WRITE_FAILURE"):
        store.save_account(
            name="replacement", email="offline@example.invalid", account_id=account_id,
            storage_state={"cookies": [{"name": "SID", "value": "synthetic-new-session"}]},
        )
    assert registry.read_bytes() == registry_before
    assert [account.to_dict() for account in store.list_accounts()] == accounts_before
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not list(account_dir.glob(".*")), "Rollback must not retain session backups"


def test_selecting_current_account_publishes_and_persists_last_used(store_files, monkeypatch):
    store, path = store_files
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(module, "datetime", clock)
    selected = store.set_active_account("first")
    assert selected.last_used == "2026-10-21T12:00:00+00:00"
    assert store.get_active_account().last_used == selected.last_used
    assert json.loads(path.read_text(encoding="utf-8"))["accounts"]["first"]["last_used"] == selected.last_used


def test_concurrent_independent_account_updates_survive_restart(store_files):
    store, path = store_files
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(store.update_account, account_id, f"new {account_id}")
                   for account_id in ("first", "second")]
        for future in futures:
            assert future.result(timeout=2) is not None
    store._registry = None
    assert {account.id: account.name for account in store.list_accounts()} == {
        "first": "new first", "second": "new second",
    }
    assert {key: value["name"] for key, value in json.loads(path.read_text(encoding="utf-8"))["accounts"].items()} == {
        "first": "new first", "second": "new second",
    }


@pytest.mark.parametrize("import_fails", [False, True])
def test_checkpoint_waits_for_import_commit_or_rollback(store_files, monkeypatch, import_fails):
    store, registry = store_files
    auth = registry.parent / "first" / "auth.json"
    old_version = storage_state.auth_file_version(auth)
    new_import = {"cookies": [{"name": "SID", "value": "synthetic-import"}]}
    checkpoint = {"cookies": [{"name": "SID", "value": "synthetic-checkpoint"}]}
    at_commit = threading.Event()
    checkpoint_waiting = threading.Event()
    finish_commit = threading.Event()
    checkpoint_thread = []
    path_lock = storage_state._path_lock
    write_json = module._atomic_write_json

    @contextmanager
    def observed_lock(path):
        if checkpoint_thread and threading.get_ident() == checkpoint_thread[0]:
            checkpoint_waiting.set()
        with path_lock(path):
            yield

    def delayed_commit(path, data):
        if path == registry:
            at_commit.set()
            assert finish_commit.wait(timeout=3)
            if import_fails:
                raise OSError("synthetic commit failure")
        write_json(path, data)

    def checkpoint_request():
        checkpoint_thread.append(threading.get_ident())
        return storage_state.atomic_write_json(auth, checkpoint, expected_version=old_version)

    monkeypatch.setattr(storage_state, "_path_lock", observed_lock)
    monkeypatch.setattr(module, "_atomic_write_json", delayed_commit)
    with ThreadPoolExecutor(max_workers=2) as executor:
        importing = executor.submit(
            store.save_account, name="new", email=None, account_id="first",
            storage_state=new_import,
        )
        try:
            assert at_commit.wait(timeout=3)
            checkpointing = executor.submit(checkpoint_request)
            assert checkpoint_waiting.wait(timeout=3)
            assert not checkpointing.done(), "Checkpoint must not see a partial import"
        finally:
            finish_commit.set()
        if import_fails:
            with pytest.raises(OSError, match="synthetic commit failure"):
                importing.result(timeout=3)
        else:
            assert importing.result(timeout=3).id == "first"
        assert checkpointing.result(timeout=3) is import_fails
    assert json.loads(auth.read_bytes()) == (checkpoint if import_fails else new_import)
    assert not list(auth.parent.glob(".*")), "No session snapshots may persist"


def test_unrecoverable_rollback_reports_fixed_summary_without_session_backups(
    store_files, monkeypatch,
):
    store, registry = store_files
    write_json = module._atomic_write_json

    def fail_commit(path, data):
        if path == registry:
            raise OSError("SYNTHETIC_PRIVATE_COMMIT_ERROR")
        write_json(path, data)

    def fail_restore(*args, **kwargs):
        raise OSError("SYNTHETIC_PRIVATE_RESTORE_ERROR")

    monkeypatch.setattr(module, "_atomic_write_json", fail_commit)
    monkeypatch.setattr(module, "atomic_write_bytes", fail_restore)
    with pytest.raises(RuntimeError, match="file rollback could not complete") as caught:
        store.save_account(
            name="new", email=None, account_id="first",
            storage_state={"cookies": [{"name": "SID", "value": "synthetic-session"}]},
        )
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert not list((registry.parent / "first").glob(".*"))
