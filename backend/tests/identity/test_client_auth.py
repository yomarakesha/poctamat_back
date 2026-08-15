from app.modules.identity.service import peek_otp

PHONE = "+99362123456"


async def test_otp_request_then_verify_issues_tokens(client):
    requested = await client.post("/api/v1/auth/otp/request",
                                  json={"phone": PHONE})
    assert requested.status_code == 200
    assert requested.json()["code_length"] == 6

    code = await peek_otp(PHONE)
    verified = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": PHONE, "code": code})
    assert verified.status_code == 200
    body = verified.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["is_new_client"] is True


async def test_wrong_code_is_rejected(client):
    await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    response = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": PHONE, "code": "000000"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OTP_INVALID"
    assert response.json()["error"]["details"]["attempts_left"] == 4


async def test_malformed_phone_is_rejected(client):
    response = await client.post("/api/v1/auth/otp/request", json={"phone": "+7999123456"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_code_never_appears_in_the_response(client):
    requested = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    code = await peek_otp(PHONE)
    assert code is not None
    assert code not in requested.text


async def _authenticate(client) -> dict:
    await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    code = await peek_otp(PHONE)
    verified = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": PHONE, "code": code})
    assert verified.status_code == 200
    return verified.json()


async def test_refresh_rotates_the_token_pair(client):
    first = await _authenticate(client)

    refreshed = await client.post("/api/v1/auth/refresh",
                                  json={"refresh_token": first["refresh_token"]})
    assert refreshed.status_code == 200
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["is_new_client"] is False

    # The new token works, which proves the rotation issued something usable and
    # not merely a string.
    again = await client.post("/api/v1/auth/refresh",
                              json={"refresh_token": second["refresh_token"]})
    assert again.status_code == 200


async def test_rotated_refresh_token_cannot_be_replayed(client):
    first = await _authenticate(client)
    await client.post("/api/v1/auth/refresh",
                      json={"refresh_token": first["refresh_token"]})

    replayed = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": first["refresh_token"]})
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "TOKEN_INVALID"


async def test_unknown_refresh_token_is_rejected(client):
    response = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


async def test_logout_revokes_the_refresh_token(client):
    issued = await _authenticate(client)

    logged_out = await client.post("/api/v1/auth/logout",
                                   json={"refresh_token": issued["refresh_token"]})
    assert logged_out.status_code == 204

    response = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": issued["refresh_token"]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


async def test_expired_code_is_rejected(client):
    response = await client.post("/api/v1/auth/otp/verify",
                                 json={"phone": PHONE, "code": "000000"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OTP_EXPIRED"


async def test_repeated_wrong_codes_exhaust_the_attempt_budget(client):
    await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})

    for _ in range(4):
        rejected = await client.post("/api/v1/auth/otp/verify",
                                     json={"phone": PHONE, "code": "000000"})
        assert rejected.status_code == 400

    exhausted = await client.post("/api/v1/auth/otp/verify",
                                  json={"phone": PHONE, "code": "000000"})
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["code"] == "OTP_TOO_MANY_ATTEMPTS"

    # The budget is spent, so even the right code is gone with it.
    assert await peek_otp(PHONE) is None
