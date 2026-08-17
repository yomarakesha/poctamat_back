from app.modules.identity.service import peek_otp

PHONE = "+99362123456"


async def _request(client, phone=PHONE) -> str:
    response = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert response.status_code == 200, response.text
    return response.json()["request_id"]


async def test_otp_request_then_verify_issues_tokens(client):
    requested = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    assert requested.status_code == 200
    body = requested.json()
    assert body["code_length"] == 6
    assert body["expires_at"].endswith("Z")
    assert body["resend_after"] == 60

    verified = await client.post("/api/v1/auth/otp/verify", json={
        "request_id": body["request_id"], "code": await peek_otp(body["request_id"]),
    })
    assert verified.status_code == 200
    tokens = verified.json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] > 0
    # A brand-new client has no name yet, so the app shows the registration form.
    assert tokens["profile_complete"] is False


async def test_wrong_code_is_rejected(client):
    request_id = await _request(client)
    response = await client.post("/api/v1/auth/otp/verify",
                                 json={"request_id": request_id, "code": "000000"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "OTP_INVALID"
    assert response.json()["error"]["details"]["attempts_left"] == 4


async def test_malformed_phone_is_rejected(client):
    response = await client.post("/api/v1/auth/otp/request",
                                 json={"phone": "+7999123456"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_code_never_appears_in_the_response(client):
    requested = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    code = await peek_otp(requested.json()["request_id"])
    assert code is not None
    assert code not in requested.text


async def test_asking_again_too_soon_is_refused(client):
    await _request(client)
    again = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    assert again.status_code == 429
    assert again.json()["error"]["code"] == "OTP_REQUEST_TOO_SOON"
    assert again.json()["error"]["details"]["resend_after"] == 60


async def test_two_devices_on_one_number_keep_separate_attempt_budgets(client):
    # Codes are filed under the request id, so a second request does not silently
    # invalidate the first device's code — it just cannot be made too soon.
    first = await _request(client)
    from app.core.kvstore import get_kvstore

    await get_kvstore().delete(f"otp:phone:{PHONE}")
    second = await _request(client)

    assert first != second
    assert await peek_otp(first) is not None
    assert await peek_otp(second) is not None


async def test_a_blocked_client_cannot_ask_for_a_code(client, session):
    from app.modules.identity.models import Client

    session.add(Client(phone=PHONE, is_blocked=True))
    await session.commit()

    response = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CLIENT_BLOCKED"


async def _authenticate(client) -> dict:
    request_id = await _request(client)
    verified = await client.post("/api/v1/auth/otp/verify", json={
        "request_id": request_id, "code": await peek_otp(request_id),
    })
    assert verified.status_code == 200
    return verified.json()


async def test_refresh_rotates_the_token_pair(client):
    first = await _authenticate(client)

    refreshed = await client.post("/api/v1/auth/refresh",
                                  json={"refresh_token": first["refresh_token"]})
    assert refreshed.status_code == 200
    second = refreshed.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["token_type"] == "Bearer"

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
    assert replayed.json()["error"]["code"] == "REFRESH_TOKEN_INVALID"


async def test_unknown_refresh_token_is_rejected(client):
    response = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "REFRESH_TOKEN_INVALID"


async def test_logout_revokes_the_refresh_token(client):
    issued = await _authenticate(client)

    logged_out = await client.post("/api/v1/auth/logout",
                                   json={"refresh_token": issued["refresh_token"]})
    assert logged_out.status_code == 204

    response = await client.post("/api/v1/auth/refresh",
                                 json={"refresh_token": issued["refresh_token"]})
    assert response.status_code == 401


async def test_an_unknown_request_id_is_rejected(client):
    import uuid

    response = await client.post("/api/v1/auth/otp/verify",
                                 json={"request_id": str(uuid.uuid4()),
                                       "code": "000000"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "OTP_NOT_FOUND"


async def test_repeated_wrong_codes_exhaust_the_attempt_budget(client):
    request_id = await _request(client)

    for _ in range(4):
        rejected = await client.post("/api/v1/auth/otp/verify",
                                     json={"request_id": request_id, "code": "000000"})
        assert rejected.status_code == 401

    exhausted = await client.post("/api/v1/auth/otp/verify",
                                  json={"request_id": request_id, "code": "000000"})
    assert exhausted.status_code == 429
    assert exhausted.json()["error"]["code"] == "OTP_ATTEMPTS_EXCEEDED"

    # The budget is spent, so even the right code is gone with it.
    assert await peek_otp(request_id) is None
