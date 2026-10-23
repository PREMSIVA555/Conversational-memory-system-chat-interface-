"""Allowlisted operational metadata and a pre-model secret minimization seam."""
import json
import logging
import os
import re
from collections import deque
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from prometheus_client import Counter

DROPPED = Counter('bank_telemetry_dropped_total', 'Optional telemetry records dropped')
REQUESTS = Counter('bank_requests_total', 'Banking requests by bounded route and outcome', ['route','method','outcome'])
ROUTES = frozenset({'/health/live','/health/ready','/metrics','/api/v1/auth/login','/api/v1/auth/callback',
                    '/api/v1/auth/session','/api/v1/auth/rotate','/api/v1/auth/logout',
                    '/api/v1/conversations','/api/v1/conversations/{resource_id}','unknown'})
METHODS = frozenset({'GET','POST','DELETE','PATCH','PUT','OPTIONS','HEAD'})
SAFE_EVENTS = frozenset({'request','failure','queued_access','queue_rejected','queue_full'})
SAFE_ATTRIBUTES = frozenset({'http.route','http.method','outcome','operation'})
safe_log = logging.getLogger('bank.safe')
safe_log.propagate = False
if not safe_log.handlers:
    safe_log.addHandler(logging.StreamHandler())
safe_log.setLevel(logging.INFO)


class LibraryMetadata(logging.Filter):
    """Third-party errors must not export exception messages, SQL or callback URLs."""
    def filter(self,record):
        record.msg='server event'
        record.args=()
        record.exc_info=record.exc_text=record.stack_info=None
        return True


def configure_security_logging():
    access=logging.getLogger('uvicorn.access')
    access.disabled=True
    access.propagate=False
    for name in ('uvicorn.error','httpx','httpcore','authlib','psycopg.pool'):
        logger=logging.getLogger(name)
        if not any(isinstance(f,LibraryMetadata) for f in logger.filters):
            logger.addFilter(LibraryMetadata())
    # Handlers also cover future library children propagating to the root.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f,LibraryMetadata) for f in handler.filters):
            handler.addFilter(LibraryMetadata())


def event(name, route='unknown', method='GET', outcome='error'):
    if name not in SAFE_EVENTS:
        return
    record = dict(event=name, route=route if route in ROUTES else 'unknown',
                  method=method if method in METHODS else 'OTHER',
                  outcome=outcome if outcome in {'ok','denied','error','dropped'} else 'error')
    try:
        safe_log.info(json.dumps(record, sort_keys=True))
    except Exception:
        DROPPED.inc()


def model_input(text):
    """Explicit future model boundary, never an authorization mechanism."""
    for key, value in os.environ.items():
        if any(part in key for part in ('PASSWORD','SECRET','API_KEY','TOKEN')) and value:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)bearer\s+[\w.\-]+', '[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[REDACTED]', text)
    text = re.sub(r'\b(?:\d[ -]?){12,19}\b', '[REDACTED]', text)
    return text


class SafeExporter(SpanExporter):
    def __init__(self):
        self.records = deque(maxlen=128)
        self.sink = None

    def export(self, spans):
        try:
            for span in spans:
                record = dict(name=span.name if span.name in {'bank.request','bank.queued'} else 'bank.operation',
                              trace_id=format(span.context.trace_id, '032x'),
                              span_id=format(span.context.span_id,'016x'),
                              links=[format(link.context.trace_id,'032x') for link in span.links],
                              attributes={k:v for k,v in span.attributes.items() if k in SAFE_ATTRIBUTES})
                # Attribute values also require bounded vocabulary.
                record['attributes'] = {k:v for k,v in record['attributes'].items()
                    if v in ROUTES | METHODS | {'ok','denied','error','queued','read_conversation'}}
                self.records.append(record)
                if self.sink:
                    self.sink(record)
            return SpanExportResult.SUCCESS
        except Exception:
            DROPPED.inc()
            return SpanExportResult.FAILURE


class Telemetry:
    def __init__(self):
        self.exporter = SafeExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(BatchSpanProcessor(self.exporter, max_queue_size=64,
            max_export_batch_size=16, schedule_delay_millis=100, export_timeout_millis=500))
        self.tracer = self.provider.get_tracer('banking-foundation')

    def close(self):
        self.provider.shutdown()
