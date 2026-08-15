from sqlalchemy import select

from app.modules.audit.models import AuditEntry, Severity, Source
from app.modules.audit.service import record


async def test_record_persists_an_entry(session):
    await record(session, event="cell.remote_open", source=Source.ADMIN,
                 message="Manual open of cell 12", actor="admin_ivanov",
                 severity=Severity.WARNING, details={"reason": "stuck door"})
    await session.commit()

    entry = await session.scalar(select(AuditEntry))
    assert entry.event == "cell.remote_open"
    assert entry.actor == "admin_ivanov"
    assert entry.details == {"reason": "stuck door"}
