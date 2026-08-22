### Task 2: Settings

**Files:**
- Create: `backend/app/core/__init__.py`, `backend/app/core/config.py`, `backend/tests/core/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.core.config.Settings` and `get_settings() -> Settings` with fields `database_url: str`, `jwt_secret: str`, `pin_pepper: str`, `access_token_ttl_minutes: int = 30`, `refresh_token_ttl_days: int = 30`, `otp_length: int = 6`, `otp_ttl_seconds: int = 300`, `pin_length: int = 5`, `hold_minutes: int = 10`, `offline_ttl_hours: int = 24`, `rental_durations: tuple[int, ...] = (12, 24, 48)`, `default_language: str = "tk"`.

- [ ] **Step 1: Write the failing test**

```python
from app.core.config import get_settings


def test_settings_expose_pinned_product_constants():
    settings = get_settings()
    assert settings.otp_length == 6
    assert settings.pin_length == 5
    assert settings.hold_minutes == 10
    assert settings.offline_ttl_hours == 24
    assert settings.rental_durations == (12, 24, 48)
    assert settings.default_language == "tk"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
./.venv/Scripts/python.exe -m pytest tests/core/test_config.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.config'`.

- [ ] **Step 3: Write `backend/app/core/config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    pin_pepper: str

    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5

    pin_length: int = 5
    hold_minutes: int = 10
    offline_ttl_hours: int = 24

    rental_durations: tuple[int, ...] = (12, 24, 48)
    default_language: str = "tk"
    supported_languages: tuple[str, ...] = ("tk", "ru", "en")


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run the test and confirm it passes**

```bash
./.venv/Scripts/python.exe -m pytest tests/core/test_config.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: application settings with pinned product constants"
```

---

