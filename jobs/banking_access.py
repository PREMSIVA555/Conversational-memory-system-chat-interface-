"""P01 queued-access guard; not the P02 durable worker/outbox implementation."""
import asyncio
from collections import deque
from opentelemetry.trace import Link
from api.privacy import event
from prometheus_client import Counter

REJECTED = Counter('bank_queue_rejected_total','Bounded access queue submissions rejected')
FAILED = Counter('bank_queue_failed_total','Access queue items rejected or failed before publication')


async def run_queued(sessions, context, telemetry, parent_span, operation):
    # Caller carries an opaque session reference, not authority from a trace ID.
    await sessions.revalidate(context)
    with telemetry.tracer.start_as_current_span('bank.queued', links=[Link(parent_span)],
              record_exception=False, set_status_on_exception=False):
        event('queued_access', outcome='ok')
        return await asyncio.wait_for(operation(context), timeout=3)


class AccessQueue:
    """Bounded P01 access/alert seam. No durability or retry claim (P02)."""
    def __init__(self, sessions, telemetry, capacity=16):
        self.sessions,self.telemetry=sessions,telemetry
        self.queue=asyncio.Queue(maxsize=capacity)
        self.failures=deque(maxlen=16)

    def submit(self,context,parent,operation):
        try:
            self.queue.put_nowait((context,parent,operation))
            return True
        except asyncio.QueueFull:
            REJECTED.inc()
            event('queue_full',outcome='dropped')
            return False

    async def drain_one(self):
        context,parent,operation=await self.queue.get()
        try:
            return await run_queued(self.sessions,context,self.telemetry,parent,operation)
        except Exception:
            FAILED.inc()
            self.failures.append('access-rejected-or-failed')
            event('queue_rejected',outcome='denied')
        finally:
            self.queue.task_done()
