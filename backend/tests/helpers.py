import uuid

DEFAULT_PRICES = ((12, 1200), (24, 1800), (48, 2600))


async def set_tariffs(client, admin_token, city, cell_type, prices=DEFAULT_PRICES):
    """PUT the whole price matrix, in the shape the contract gives it.

    Nearly every booking test needs prices in place first, and the endpoint takes
    `Money` objects behind an idempotency key. Keeping that shape in one place
    makes the next contract change one edit rather than twenty.
    """
    return await client.put("/api/v1/admin/tariffs", headers={
        "Authorization": f"Bearer {admin_token}",
        "Idempotency-Key": str(uuid.uuid4()),
    }, json={"items": [
        {"city_id": str(city.id), "cell_type_id": str(cell_type.id),
         "duration_hours": hours,
         "price": {"amount_minor": amount, "currency": "TMT"}}
        for hours, amount in prices
    ]})
