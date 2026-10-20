"""Load a 역량 체계 + 과제 dataset from JSON files into Postgres.

Synthetic seed data (ticket 04) and real data use the same files (story 50), so
swapping datasets is just pointing the loader at another directory.

Directory layout
----------------
``<dir>/competencies.json``: a JSON array of ``task_design.domain.Competency``::

    [
      {
        "id": "C1",                       # stable string id, referenced by tasks
        "name": "AI 결과 검증",
        "description": "...",             # optional
        "sub_elements": [                 # 하위 요소, kept in file order
          {
            "id": "C1.1",                 # globally unique string id
            "name": "출처 확인",
            "description": "...",         # optional
            "indicators": {               # 수준 지표: all three levels required
              "high": "...", "medium": "...", "low": "..."
            }
          }
        ]
      }
    ]

``<dir>/tasks.json``: a JSON array of ``task_design.domain.Task``::

    [
      {
        "id": "8f6c...-uuid",             # optional but recommended: makes reloads idempotent
        "scenario": "상황 ...",
        "instruction": "지시문 ...",
        "attachments": [                  # optional, default []
          {"filename": "sales.csv", "format": "csv|memo|email|report", "content": "...",
           "traps": [{"location": "3행 매출 열", "description": "..."}]}
        ],
        "rubric": {"items": [             # 3–5 items, at least one "process"
          {"title": "...", "competency_id": "C1", "type": "process|output", "points": 40,
           "criteria_zero": "...", "criteria_partial": "...", "criteria_full": "..."}
        ]},
        "competency_ids": ["C1"],         # 1–2 ids
        "audience": {"job": "영업 관리", "career_level": "3년차"},
        "difficulty": "high|medium|low"
      }
    ]

Either file may be absent. Competencies are upserted by id (their sub-elements and
indicators are replaced). Tasks are upserted by id with ``source='seed'``, and each
one is embedded through the gateway. Every competency id a task references (in
``competency_ids`` or a rubric item) must exist after the competency file is loaded,
or nothing from ``tasks.json`` is written and ``UnknownTaskCompetency`` is raised.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from task_design.db import Database
from task_design.domain import Competency, Task
from task_design.gateway import ModelGateway
from task_design.repositories import TaskRepository
from task_design.settings import Settings

COMPETENCIES_FILE = "competencies.json"
TASKS_FILE = "tasks.json"

_competencies = TypeAdapter(list[Competency])
_tasks = TypeAdapter(list[Task])


class UnknownTaskCompetency(ValueError):
    """tasks.json references a competency id that is not in the 역량 체계.

    The message is Korean; ``competency_ids`` lists the unknown ids.
    """

    def __init__(self, competency_ids: list[str]) -> None:
        super().__init__(f"과제가 역량 체계에 없는 역량을 참조합니다: {', '.join(competency_ids)}")
        self.competency_ids = competency_ids


def load_dataset(
    directory: Path | str, db: Database, gateway: ModelGateway, settings: Settings
) -> tuple[int, int]:
    """Load ``competencies.json`` then ``tasks.json`` from ``directory``.

    Returns ``(competencies_loaded, tasks_loaded)``.
    """
    directory = Path(directory)
    competencies = _read(directory / COMPETENCIES_FILE, _competencies)
    tasks = _read(directory / TASKS_FILE, _tasks)

    with db.connection() as conn:
        for c in competencies:
            conn.execute(
                """
                INSERT INTO competencies (id, name, description) VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name, description = EXCLUDED.description
                """,
                (c.id, c.name, c.description),
            )
            conn.execute("DELETE FROM competency_sub_elements WHERE competency_id = %s", (c.id,))
            for position, s in enumerate(c.sub_elements):
                conn.execute(
                    "INSERT INTO competency_sub_elements (id, competency_id, position, name, description)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (s.id, c.id, position, s.name, s.description),
                )
                for level, text in s.indicators.items():
                    conn.execute(
                        "INSERT INTO level_indicators (sub_element_id, level, description)"
                        " VALUES (%s, %s, %s)",
                        (s.id, level.value, text),
                    )
        known = {r[0] for r in conn.execute("SELECT id FROM competencies").fetchall()}

    unknown = sorted(
        {cid for t in tasks for cid in _referenced_competencies(t)} - known
    )
    if unknown:
        raise UnknownTaskCompetency(unknown)

    repo = TaskRepository(db, gateway, settings)
    for t in tasks:
        repo.add(t, source="seed")
    return len(competencies), len(tasks)


def _referenced_competencies(task: Task) -> set[str]:
    return set(task.competency_ids) | {i.competency_id for i in task.rubric.items}


def _read(path: Path, adapter: TypeAdapter):
    if not path.exists():
        return []
    return adapter.validate_python(json.loads(path.read_text(encoding="utf-8")))
