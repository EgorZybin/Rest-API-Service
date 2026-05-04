from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from sherlock_api.api.v1.schemas.tasks import TaskEnqueueCommon, TaskEnqueueResponse
from sherlock_api.auth import require_api_key
from sherlock_api.config import get_settings
from sherlock_api.db.session import get_session
from sherlock_api.dispatcher.handlers.schemas import (
    AddressSearchInput,
    CadastreSearchInput,
    CarPlateSearchInput,
    DocsInnSearchInput,
    DocsPassportSearchInput,
    DocsSnilsSearchInput,
    DomainIpSearchInput,
    EmailSearchInput,
    LegalSearchInput,
    NickSearchInput,
    PhoneSearchInput,
    PhotoSearchInput,
    TagSearchInput,
    VinSearchInput,
)
from sherlock_api.dispatcher.queue import TaskQueue
from sherlock_api.logging import get_logger

router = APIRouter(prefix="/search", tags=["search"])
log = get_logger(__name__)
_queue = TaskQueue()


class PhoneSearchRequest(PhoneSearchInput, TaskEnqueueCommon):
    pass


class NickSearchRequest(NickSearchInput, TaskEnqueueCommon):
    pass


class EmailSearchRequest(EmailSearchInput, TaskEnqueueCommon):
    pass


class CarPlateSearchRequest(CarPlateSearchInput, TaskEnqueueCommon):
    pass


class VinSearchRequest(VinSearchInput, TaskEnqueueCommon):
    pass


class AddressSearchRequest(AddressSearchInput, TaskEnqueueCommon):
    pass


class CadastreSearchRequest(CadastreSearchInput, TaskEnqueueCommon):
    pass


class DocsInnSearchRequest(DocsInnSearchInput, TaskEnqueueCommon):
    pass


class DocsSnilsSearchRequest(DocsSnilsSearchInput, TaskEnqueueCommon):
    pass


class DocsPassportSearchRequest(DocsPassportSearchInput, TaskEnqueueCommon):
    pass


class LegalSearchRequest(LegalSearchInput, TaskEnqueueCommon):
    pass


class DomainIpSearchRequest(DomainIpSearchInput, TaskEnqueueCommon):
    pass


class TagSearchRequest(TagSearchInput, TaskEnqueueCommon):
    pass


def _enqueue_response(task, scenario: str) -> TaskEnqueueResponse:
    return TaskEnqueueResponse(
        id=task.id,
        scenario=scenario,
        status=task.status,
        priority=task.priority,
        created_at=task.created_at,
        status_url=f"/v1/tasks/{task.id}",
    )


_COMMON_OPTION_FIELDS = {"priority", "max_attempts", "webhook_url"}


async def _enqueue_text_search(
    *,
    scenario: str,
    payload: TaskEnqueueCommon,
    session: AsyncSession,
) -> TaskEnqueueResponse:
    body = payload.model_dump()
    scenario_input = {k: v for k, v in body.items() if k not in _COMMON_OPTION_FIELDS}
    task = await _queue.enqueue(
        session,
        scenario=scenario,
        input_payload=scenario_input,
        priority=payload.priority,
        max_attempts=payload.max_attempts,
        webhook_url=payload.webhook_url,
    )
    await session.commit()
    return _enqueue_response(task, scenario)


@router.post(
    "/phone",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по номеру телефона",
)
async def enqueue_phone_search(
    payload: PhoneSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    scenario_input = {
        "phone": payload.phone,
        "max_pages": payload.max_pages,
    }
    task = await _queue.enqueue(
        session,
        scenario="phone_search",
        input_payload=scenario_input,
        priority=payload.priority,
        max_attempts=payload.max_attempts,
        webhook_url=payload.webhook_url,
    )
    await session.commit()
    return _enqueue_response(task, "phone_search")


@router.post(
    "/nick",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по нику / user-id / ссылке VK",
)
async def enqueue_nick_search(
    payload: NickSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    scenario_input = {
        "nick": payload.nick,
        "max_pages": payload.max_pages,
        "search_in": payload.search_in,
    }
    task = await _queue.enqueue(
        session,
        scenario="nick_search",
        input_payload=scenario_input,
        priority=payload.priority,
        max_attempts=payload.max_attempts,
        webhook_url=payload.webhook_url,
    )
    await session.commit()
    return _enqueue_response(task, "nick_search")


@router.post(
    "/photo",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по фото",
)
async def enqueue_photo_search(
    photo: Annotated[UploadFile, File(description="Портрет в формате JPEG/PNG")],
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
    max_pages: Annotated[int, Form(ge=1, le=100)] = 20,
    priority: Annotated[int, Form(ge=-100, le=100)] = 0,
    max_attempts: Annotated[int, Form(ge=1, le=10)] = 3,
    webhook_url: Annotated[str | None, Form(max_length=512)] = None,
) -> TaskEnqueueResponse:
    settings = get_settings()
    upload_id = uuid.uuid4().hex
    orig = Path(photo.filename or "photo.jpg").name
    target_dir = Path(settings.storage_dir).resolve() / "uploads" / upload_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / orig

    try:
        contents = await photo.read()
        if not contents:
            raise HTTPException(status_code=400, detail="empty photo payload")
        target.write_bytes(contents)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"failed to store upload: {type(e).__name__}",
        ) from e

    inp = PhotoSearchInput(photo_path=str(target.resolve()), max_pages=max_pages)

    task = await _queue.enqueue(
        session,
        scenario="photo_search",
        input_payload=inp.model_dump(),
        priority=priority,
        max_attempts=max_attempts,
        webhook_url=webhook_url,
    )
    await session.commit()
    log.info(
        "search.photo.enqueued",
        task_id=str(task.id),
        upload_path=str(target),
    )
    return _enqueue_response(task, "photo_search")


@router.post(
    "/email",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по email",
)
async def enqueue_email_search(
    payload: EmailSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="email_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/car-plate",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по госномеру",
)
async def enqueue_car_plate_search(
    payload: CarPlateSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="car_plate_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/vin",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по VIN",
)
async def enqueue_vin_search(
    payload: VinSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="vin_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/address",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по адресу (/adr)",
)
async def enqueue_address_search(
    payload: AddressSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="address_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/cadastre",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по кадастровому номеру",
)
async def enqueue_cadastre_search(
    payload: CadastreSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="cadastre_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/docs/inn",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по ИНН физлица",
)
async def enqueue_docs_inn(
    payload: DocsInnSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="docs_inn",
        payload=payload,
        session=session,
    )


@router.post(
    "/docs/snils",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по СНИЛС",
)
async def enqueue_docs_snils(
    payload: DocsSnilsSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="docs_snils",
        payload=payload,
        session=session,
    )


@router.post(
    "/docs/passport",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск по паспорту",
)
async def enqueue_docs_passport(
    payload: DocsPassportSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="docs_passport",
        payload=payload,
        session=session,
    )


@router.post(
    "/legal",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск юрлица по ИНН/ОГРН/ОГРНИП",
)
async def enqueue_legal_search(
    payload: LegalSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="legal_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/domain-ip",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь поиск информации по домену/IP",
)
async def enqueue_domain_ip_search(
    payload: DomainIpSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="domain_ip_search",
        payload=payload,
        session=session,
    )


@router.post(
    "/tag",
    response_model=TaskEnqueueResponse,
    summary="Поставить в очередь /tag <name>",
)
async def enqueue_tag_search(
    payload: TagSearchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(require_api_key)],
) -> TaskEnqueueResponse:
    return await _enqueue_text_search(
        scenario="tag_search",
        payload=payload,
        session=session,
    )
