from workpanda.application.task_service import InvalidTaskTransition, TaskService
from workpanda.domain.models import TaskCreate, TaskStatus
from workpanda.infrastructure.memory_store import InMemoryTaskStore


def service() -> TaskService:
    return TaskService(InMemoryTaskStore())


def test_create_task_emits_accepted_event() -> None:
    task_service = service()
    task = task_service.create_task(
        TaskCreate(conversation_id="conversation-1", flow_id="generic", flow_version="1")
    )

    events = task_service.events(task.task_id)
    assert task.status == TaskStatus.CREATED
    assert len(events) == 1
    assert events[0].type == "accepted"
    assert events[0].sequence == 1


def test_cancel_task_is_idempotency_guarded() -> None:
    task_service = service()
    task = task_service.create_task(
        TaskCreate(conversation_id="conversation-1", flow_id="generic", flow_version="1")
    )

    cancelled = task_service.cancel_task(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED

    try:
        task_service.cancel_task(task.task_id)
    except InvalidTaskTransition:
        pass
    else:
        raise AssertionError("cancelling a cancelled task must be rejected")
