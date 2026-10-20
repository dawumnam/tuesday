from task_design.db import Database
from task_design.service import TaskDesignService


def test_migrated_database_has_extensions_staff_and_a_working_service(
    db: Database, service: TaskDesignService
) -> None:
    with db.connection() as conn:
        extensions = {row[0] for row in conn.execute("SELECT extname FROM pg_extension")}
    assert {"vector", "pg_trgm"} <= extensions

    staff = service.list_staff()
    assert len(staff) == 2
    assert len({s.id for s in staff}) == 2
    assert all(s.name for s in staff)
