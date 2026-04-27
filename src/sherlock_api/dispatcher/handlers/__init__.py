from __future__ import annotations

from sherlock_api.dispatcher.handlers.base import (
    HANDLERS,
    HandlerContext,
    HandlerError,
    HandlerFn,
    HandlerOutcome,
    HandlerPermanentError,
    HandlerRateLimitError,
    HandlerSubscriptionError,
    register_handler,
)
from sherlock_api.dispatcher.handlers.extras import (
    address_search_handler,
    cadastre_search_handler,
    car_plate_search_handler,
    docs_inn_handler,
    docs_passport_handler,
    docs_snils_handler,
    domain_ip_search_handler,
    email_search_handler,
    legal_search_handler,
    tag_search_handler,
    vin_search_handler,
)
from sherlock_api.dispatcher.handlers.noop import (
    noop_handler,
)
from sherlock_api.dispatcher.handlers.sherlock import (
    nick_search_handler,
    phone_search_handler,
    photo_search_handler,
)

__all__ = [
    "HANDLERS",
    "HandlerContext",
    "HandlerError",
    "HandlerFn",
    "HandlerOutcome",
    "HandlerPermanentError",
    "HandlerRateLimitError",
    "HandlerSubscriptionError",
    "address_search_handler",
    "cadastre_search_handler",
    "car_plate_search_handler",
    "docs_inn_handler",
    "docs_passport_handler",
    "docs_snils_handler",
    "domain_ip_search_handler",
    "email_search_handler",
    "legal_search_handler",
    "nick_search_handler",
    "noop_handler",
    "phone_search_handler",
    "photo_search_handler",
    "register_handler",
    "tag_search_handler",
    "vin_search_handler",
]
