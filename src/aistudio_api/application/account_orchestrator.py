"""Account failover and concurrency orchestration."""

from __future__ import annotations

import asyncio
import math
import time
from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException

from aistudio_api.application.account_rotator import (
    AccountStats,
    get_seconds_until_pacific_midnight,
)
from aistudio_api.application.runtime import runtime_state
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("orchestrator")
MAX_RETRIES = 5
AUTH_GENERATION_UNTRACKED = object()

_switch_lock: asyncio.Lock | None = None
_switch_lock_loop: asyncio.AbstractEventLoop | None = None
_last_switch_time: float = 0.0


class AccountPreparationError(Exception):
    """Preserve the identity attempted before a request obtained a session lease."""

    def __init__(
        self, account_id: str, original_error: Exception,
        *, auth_generation: object = AUTH_GENERATION_UNTRACKED,
    ) -> None:
        super().__init__("Could not prepare account session")
        self.account_id = account_id
        self.original_error = original_error
        self.auth_generation = auth_generation


def _get_switch_lock() -> asyncio.Lock:
    global _switch_lock, _switch_lock_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _switch_lock is None or _switch_lock_loop is not loop:
        _switch_lock = asyncio.Lock()
        _switch_lock_loop = loop
    return _switch_lock


def account_change_lock() -> asyncio.Lock:
    """Management changes share inference's registry/context switch boundary."""
    return _get_switch_lock()


def note_account_changed() -> None:
    """A completed owner change supersedes failures waiting on the old context."""
    global _last_switch_time
    _last_switch_time = time.time()


async def try_switch_account(
    model: str | None = None,
    failed_account_id: str | None = None,
    *,
    is_auth_error: bool = False,
    is_session_expired: bool = False,
    failure_recorded: bool = False,
    failed_context_id: int | None = None,
    failed_auth_generation: object = AUTH_GENERATION_UNTRACKED,
) -> bool:
    """尝试切换到下一个对目标 model 可用的账号。防止并发级联切号。"""
    global _last_switch_time
    entry_time = time.time()

    # 乐观快速预检：若无需记录永久失效/短时隔离，且当前活跃账号已被并发请求切换至健康状态，直接免锁复用
    if failed_account_id and not is_session_expired and not is_auth_error:
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        if rotator is not None and account_service is not None:
            cur_active = account_service.get_active_account()
            if (
                cur_active
                and (
                    cur_active.id != failed_account_id
                    or _last_switch_time >= entry_time
                )
                and rotator.is_account_available(cur_active.id, model=model)
            ):
                return True

    async with _get_switch_lock():
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        client = runtime_state.client

        if (
            rotator is None
            or account_service is None
            or client is None
            or client.session is None
        ):
            return False

        current_active = account_service.get_active_account()
        current_id = current_active.id if current_active else None

        # An owner import can complete while a 403 waits for this lock. Check
        # again before recording a quarantine or starting any recovery work.
        if (
            failed_account_id is not None
            and failed_auth_generation is not AUTH_GENERATION_UNTRACKED
            and not account_generation_is_current(failed_account_id, failed_auth_generation)
        ):
            return bool(current_id and rotator.is_account_available(current_id, model=model))

        active_context = client.session.active_context
        active_context_id = active_context.context_id if active_context else None
        same_account_recovered = bool(
            current_id
            and current_id == failed_account_id
            and not is_session_expired
            and (
                _last_switch_time >= entry_time
                or (
                    failed_context_id is not None
                    and isinstance(active_context_id, int)
                    and active_context_id != failed_context_id
                )
            )
        )
        if same_account_recovered and current_id is not None:
            # Old concurrent 403s belong to the retired session. A caller may
            # already have marked one before entering the lock; remove only
            # that obsolete auth lockout, preserving model quotas/cooldowns.
            if is_auth_error and failure_recorded:
                rotator.clear_auth_lockout(current_id)
            if rotator.is_account_available(current_id, model=model):
                return True

        # A concurrent switch must not prevent attribution to the failed lease.
        # Auth errors from a retired context must not lock a refreshed session.
        if failed_account_id and not failure_recorded and not same_account_recovered:
            if is_session_expired:
                rotator.record_session_expired(failed_account_id, model=model)
            elif is_auth_error:
                rotator.record_auth_error(failed_account_id, model=model)

        # 双重检查 1：如果已有并发协程在 entry_time 之后完成切号或在位自愈刷新，且当前账号对该模型可用，则直接复用
        if (
            _last_switch_time >= entry_time
            and current_id
            and (not is_session_expired or current_id != failed_account_id)
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "已有并发请求完成切号或会话自愈，直接复用当前健康账号: %s (model=%s)",
                current_id,
                model,
            )
            return True

        # 双重检查 2：如果已经由并发协程切换到了新账号，且新账号对当前 model 可用，则直接复用
        if (
            failed_account_id
            and current_id
            and current_id != failed_account_id
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "已有并发请求完成切号，直接复用当前健康账号: %s (model=%s)",
                current_id,
                model,
            )
            return True

        next_account = await rotator.get_next_account(
            model=model,
            current_account_id=current_id,
            failed_account_id=failed_account_id,
            is_session_expired=is_session_expired,
        )
        if next_account is None:
            if is_session_expired:
                logger.error(
                    "账号 %s 登录态失效且无其他可用备用账号，禁止原地重试",
                    failed_account_id,
                )
            return False

        if current_id is None or next_account.id != current_id:
            try:
                result = await account_service.activate_account(
                    next_account.id,
                    client.session,
                )
            except AccountPreparationError:
                raise
            except Exception as exc:
                raise AccountPreparationError(
                    next_account.id, exc, auth_generation=next_account.auth_generation,
                ) from exc
            if result is not None:
                rotator.clear_auth_lockout(next_account.id)
                _last_switch_time = time.time()
            return result is not None
        # 登录态失效严禁在原账号原地重试
        if is_session_expired and (
            failed_account_id == current_id or next_account.id == failed_account_id
        ):
            logger.error("账号 %s 登录态已失效，禁止在原账号重试", failed_account_id)
            return False

        # 单账号模式或所有其他账号均不可用时（非登录态失效，如偶发 403 鉴权波动），如果指定了 failed_account_id，强制刷新当前会话与 BotGuard
        if failed_account_id and failed_account_id == current_id:
            logger.info(
                "无其他可用备用账号，重新刷新当前账号会话与 BotGuard: %s",
                current_id,
            )
            client.clear_templates()
            try:
                result = await account_service.activate_account(
                    current_id,
                    client.session,
                )
            except AccountPreparationError:
                raise
            except Exception as exc:
                raise AccountPreparationError(
                    current_id, exc, auth_generation=current_active.auth_generation if current_active else None,
                ) from exc
            if result is not None:
                rotator.clear_auth_lockout(current_id)
                _last_switch_time = time.time()
            return result is not None
        return bool(
            current_id and rotator.is_account_available(current_id, model=model)
        )


def quota_unavailable_error(model: str | None = None) -> HTTPException:
    """Describe current cooldowns without assuming all 429s last until midnight."""
    delays: list[int] = []
    service, rotator = runtime_state.account_service, runtime_state.rotator
    if service is not None and rotator is not None:
        for account in service.list_accounts():
            current = rotator.get_account_stats(account.id)
            if not isinstance(current, AccountStats) or current.session_expired:
                continue
            snapshot = deepcopy(current)
            # Admission allows isolated auth recovery; this hint concerns quota.
            snapshot.auth_cooldown = 0
            if snapshot.is_available(model, ignore_auth_cooldown=True):
                remaining = 0.0
            elif snapshot.rate_limited_date_la:
                remaining = get_seconds_until_pacific_midnight()
            else:
                remaining = snapshot.get_cooldown_remaining(model)
            if math.isfinite(remaining):
                delays.append(max(1, math.ceil(remaining)))
    label = f"model '{model}'" if model else "the requested model"
    return HTTPException(
        429,
        detail={
            "message": f"All accounts are temporarily rate limited for {label}. Retry after the current cooldown.",
            "type": "rate_limit_exceeded",
        },
        headers={"Retry-After": str(min(delays))} if delays else None,
    )


def validate_account_availability(model: str | None = None) -> None:
    """Check admission without starting a browser or changing daily counters."""
    account_service = runtime_state.account_service
    rotator = runtime_state.rotator
    if account_service is None or rotator is None:
        raise HTTPException(503, detail="Account service is not ready")
    accounts = account_service.list_accounts()
    if not accounts:
        raise HTTPException(503, detail="No accounts configured. Please import an account.")
    stats = [rotator.get_account_stats(account.id) for account in accounts]
    # A temporary auth failure may still use the existing isolated refresh
    # path. Expired credentials and model cooldowns must never bypass admission.
    if any(
        item is None
        or deepcopy(item).is_available(model, ignore_auth_cooldown=True)
        for item in stats
    ):
        return
    if all(item is not None and item.session_expired for item in stats):
        raise HTTPException(
            503,
            detail={
                "message": "The API key is valid, but all upstream Google sessions have expired. Visit the management page to import fresh cookies; keep the current API key.",
                "type": "upstream_auth_expired",
            },
        )
    raise quota_unavailable_error(model)


async def ensure_active_account(attempt: int, model: str | None = None) -> None:
    """Select a usable account before every attempt, including retry races."""
    logger.debug("Preparing an available account: attempt=%d", attempt + 1)
    validate_account_availability(model)
    account_svc = runtime_state.account_service
    rotator = runtime_state.rotator
    current = account_svc.get_active_account() if account_svc else None
    if not current:
        switched = await try_switch_account(model=model)
        if not switched:
            raise HTTPException(503, detail="Could not prepare an available account")
    elif rotator and not rotator.is_account_available(current.id, model=model):
        # 当前账号对该模型已限流，提前切号
        switched = await try_switch_account(model=model, failed_account_id=current.id)
        if not switched:
            validate_account_availability(model)
            raise HTTPException(503, detail="Could not prepare an available account")

    # Registry selection and browser initialization are separate. Resolve the
    # active path lazily so accounts imported after process startup also work.
    async with _get_switch_lock():
        account_svc = runtime_state.account_service
        client = runtime_state.client
        rotator = runtime_state.rotator
        current = account_svc.get_active_account() if account_svc else None
        if account_svc is None or client is None or current is None or rotator is None:
            raise HTTPException(503, detail="Account service is not ready")
        if not rotator.is_account_available(current.id, model=model):
            validate_account_availability(model)
            raise HTTPException(503, detail="Selected account is no longer available")
        session = client.session
        auth_path = account_svc.get_account_auth_path(current.id)
        if auth_path is None:
            raise RuntimeError("Active account credentials are unavailable")
        target_auth = str(auth_path)
        context = getattr(session, "active_context", None)
        context_generation = getattr(context, "auth_generation", AUTH_GENERATION_UNTRACKED)
        if (
            session._auth_file != target_auth or not session.is_ready
            or (
                context_generation is not AUTH_GENERATION_UNTRACKED
                and context_generation != current.auth_generation
            )
        ):
            try:
                await session.transition_to_account(target_auth)
            except AccountPreparationError:
                raise
            except Exception as exc:
                raise AccountPreparationError(
                    current.id, exc, auth_generation=current.auth_generation,
                ) from exc
        # Browser preparation yields to other requests. A concurrent failure
        # can quarantine this account while its context is being prepared.
        if not rotator.is_account_available(current.id, model=model):
            validate_account_availability(model)
            raise HTTPException(503, detail="Selected account is no longer available")


def account_id_for_auth_file(auth_file: str | None) -> str | None:
    """Identify the account actually leased by a request, without active fallback."""
    account_service = runtime_state.account_service
    if not auth_file or account_service is None:
        return None
    target = Path(auth_file).resolve()
    for account in account_service.list_accounts():
        auth_path = account_service.get_account_auth_path(account.id)
        if auth_path is not None and Path(auth_path).resolve() == target:
            return account.id
    return None


def record_rotator_event(
    event: str,
    model: str | None = None,
    *,
    account_id: str | None,
    auth_generation: object = AUTH_GENERATION_UNTRACKED,
) -> None:
    """Record an event against its leased account, never the later active account."""
    rotator = runtime_state.rotator
    if not rotator or account_id is None:
        return
    if (
        auth_generation is not AUTH_GENERATION_UNTRACKED
        and not account_generation_is_current(account_id, auth_generation)
    ):
        return
    if event == "success":
        rotator.record_success(account_id, model=model)
    elif event == "rate_limited":
        rotator.record_rate_limited(account_id, model=model)
    elif event == "auth_error":
        rotator.record_auth_error(account_id, model=model)
    elif event == "session_expired":
        rotator.record_session_expired(account_id, model=model)
    elif event == "error":
        rotator.record_error(account_id, model=model)


def account_generation_is_current(account_id: str, auth_generation: object) -> bool:
    """Retired request outcomes cannot change reimported/deleted account state."""
    service = runtime_state.account_service
    if service is None:
        return False
    account = service.get_account(account_id)
    return account is not None and account.auth_generation == auth_generation


__all__ = [
    "MAX_RETRIES",
    "AccountPreparationError",
    "account_id_for_auth_file",
    "ensure_active_account",
    "record_rotator_event",
    "try_switch_account",
    "validate_account_availability",
]
