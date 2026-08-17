async def test_repeated_otp_requests_are_limited(client):
    payload = {"phone": "+99362123456"}
    for _ in range(3):
        response = await client.post("/api/v1/auth/otp/request", json=payload)
        assert response.status_code == 200

    blocked = await client.post("/api/v1/auth/otp/request", json=payload)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert blocked.json()["error"]["details"]["retry_after_seconds"] > 0


async def test_the_public_catalog_has_a_wider_limit(client):
    # The public reads share one bucket, and it is generous enough that a
    # kiosk refreshing both lists never trips it.
    for _ in range(60):
        assert (await client.get("/api/v1/cities")).status_code == 200
    for _ in range(60):
        assert (await client.get("/api/v1/cell-types")).status_code == 200

    blocked = await client.get("/api/v1/cities")
    assert blocked.status_code == 429


async def test_the_buckets_are_counted_separately(client):
    # Exhausting the OTP bucket must not close the catalogue: they are separate
    # surfaces with separate costs.
    payload = {"phone": "+99362123456"}
    for _ in range(4):
        await client.post("/api/v1/auth/otp/request", json=payload)

    assert (await client.get("/api/v1/cities")).status_code == 200
