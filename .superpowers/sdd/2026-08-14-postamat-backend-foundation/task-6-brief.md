### Task 6: Shared field types

**Files:**
- Create: `backend/app/core/types.py`, `backend/tests/core/test_types.py`

**Interfaces:**
- Produces: `PhoneNumber` (annotated `str`), `Money` (Pydantic model with `amount_minor: int`, `currency: str = "TMT"`), `utc_isoformat(dt) -> str`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from datetime import datetime, timezone
from pydantic import BaseModel, ValidationError

from app.core.types import Money, PhoneNumber, utc_isoformat


class Sample(BaseModel):
    phone: PhoneNumber


def test_valid_turkmen_number_is_accepted():
    assert Sample(phone="+99362123456").phone == "+99362123456"


@pytest.mark.parametrize("value", ["+7999123456", "99362123456", "+9936212345", "+993621234567"])
def test_bad_numbers_are_rejected(value):
    with pytest.raises(ValidationError):
        Sample(phone=value)


def test_money_defaults_to_manat():
    assert Money(amount_minor=1200).model_dump() == {"amount_minor": 1200, "currency": "TMT"}


def test_utc_isoformat_uses_z_suffix():
    moment = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)
    assert utc_isoformat(moment) == "2026-08-14T09:30:00Z"
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
docker compose run --rm api pytest tests/core/test_types.py -v
```

- [ ] **Step 3: Write `backend/app/core/types.py`**

```python
from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

PhoneNumber = Annotated[str, StringConstraints(pattern=r"^\+993[0-9]{8}$")]


class Money(BaseModel):
    amount_minor: int = Field(ge=0)
    currency: str = "TMT"


def utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 4: Run the test and confirm it passes**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: shared phone, money and timestamp types"
```

---

