"""Customer lookup tool — finds an existing customer or creates a minimal one."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import CustomerRepository
from app.db.schemas import LookupCustomerInput, LookupCustomerOutput
from app.observability.logger import log_dataflow
from app.utils.phone import normalize_e164


async def lookup_customer(
    session: AsyncSession, payload: LookupCustomerInput
) -> LookupCustomerOutput:
    try:
        phone = normalize_e164(payload.phone_number)
    except ValueError as e:
        return LookupCustomerOutput(
            success=False,
            message=f"invalid phone number: {e}",
        )

    repo = CustomerRepository(session)
    customer, created = await repo.get_or_create(phone=phone)
    log_dataflow(
        "tool.lookup_customer",
        f"{'created new' if created else 'found existing'} customer",
        payload={"phone": phone, "customer_id": str(customer.id)},
    )
    return LookupCustomerOutput(
        success=True,
        customer_id=str(customer.id),
        name=customer.name,
        preferred_language=customer.preferred_language,
        is_new_customer=created,
        message="ok",
    )
