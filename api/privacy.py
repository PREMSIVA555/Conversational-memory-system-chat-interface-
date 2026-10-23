"""Allowlisted operational metadata and a pre-model secret minimization seam."""
import json
import importlib.util
import logging
import os
import re
from pathlib import Path
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
    """Local defense in depth before model dispatch; never authorization.

    Names use installed English NER plus explicit name labels. This cannot
    promise all names/languages. Scanner failure raises without raw payloads.
    """
    secrets={value for key,value in os.environ.items()
        if value and any(part in key.upper() for part in ('PASSWORD','SECRET','API_KEY','TOKEN'))}
    for value in sorted(secrets,key=len,reverse=True):
        text = text.replace(value, '[REDACTED]')
    text = text.replace('\u200b','').replace('\ufeff','')
    # Normalize common pasted numeric separators before local recognizers.
    text = re.sub(r'(?<=\d)[\u2010-\u2015\u2212](?=\d)', '-', text)
    text = re.sub(r'(?i)bearer\s+[\w.\-]+', '[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', '[REDACTED]', text)
    # Mask typed planning tokens by offset, not a reversible placeholder sent
    # to the model. Secret matching above still wins over these exceptions.
    protected=[]
    for pattern in (
        r'(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b',
        r'\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?',
        r'\b(?:INR|USD|EUR|GBP|JPY|KWD)\s+-?\d+(?:\.\d+)?\b',
        r'(?i)\bamount(?:\s+(?:of|is))?\s*[:=]?\s*-?\d+(?:\.\d+)?\b',
    ):
        protected.extend((m.start(),m.end()) for m in re.finditer(pattern,text))
    chars=list(text)
    for start,end in protected:chars[start:end]=' '* (end-start)
    masked=''.join(chars)
    spans=[]
    for pattern in (
        r'(?<!\w)(?:\d[\s-]?){11,18}\d(?!\w)',
        r'(?<!\w)\d{3}[-.\s]?\d{2}[-.\s]?\d{4}(?!\w)',
        r'(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|\d{5}[\s.-]\d{5})(?!\w)',
        r'(?i)\b(?:my name is|name\s*:)\s+[^\W\d_]+(?:[ \t]+[^\W\d_]+){0,3}',
    ):
        spans.extend((m.start(),m.end()) for m in re.finditer(pattern,masked))
    try:
        from capture.pii import get_engines, TARGET_ENTITIES, spacy_model_name
        model=spacy_model_name()
        # Presidio may try downloading a missing spaCy model. Requests must
        # fail closed instead of installing software in a background thread.
        if not Path(model).is_dir() and importlib.util.find_spec(model) is None:
            raise ValueError('Local privacy model missing')
        results=get_engines()['analyzer'].analyze(text=masked,language='en',
            entities=TARGET_ENTITIES+['PERSON'],score_threshold=.4)
        spans.extend((r.start,r.end) for r in results)
    except Exception:
        raise ValueError('Local privacy scan unavailable') from None
    # A recognizer may span across a masked token: redact the surrounding
    # characters but preserve only the deliberately masked token itself.
    redact=[False]*len(text)
    for start,end in spans:
        for index in range(start,end):redact[index]=True
    for start,end in protected:
        for index in range(start,end):redact[index]=False
    parts=[];inside=False
    for char,remove in zip(text,redact):
        if remove:
            if not inside:parts.append('[REDACTED]')
        else:parts.append(char)
        inside=remove
    text=''.join(parts)
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
