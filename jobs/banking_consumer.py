"""Durable local consumer; no transport exactly-once, provider or bank-tool claims.

Dispatcher metadata is a capability for one lease. User accepted work remains durable
past logout; it grants no authority to read bank facts. Handler v1 records a local
processing result only. Preference extraction and application are T02.3.
"""
from dataclasses import dataclass
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from prometheus_client import Counter

SOURCE_READS = Counter('bank_consumer_source_reads_total', 'Durable consumer source reads including repeated computation')
DUPLICATES = Counter('bank_consumer_duplicate_deliveries_total', 'Already committed local deliveries reconciled')


@dataclass(frozen=True)
class Lease:
    event_id: UUID
    customer_id: UUID
    generation: int
    owner: UUID


class Consumer:
    def __init__(self, dispatcher_dsn, worker_dsn):
        self._dispatcher_dsn = dispatcher_dsn
        self._worker_dsn = worker_dsn

    def claim(self, *, limit=16, lease_seconds=15, event_id=None):
        # Independent connection/transaction closes before dispatch or computation.
        with psycopg.connect(self._dispatcher_dsn, row_factory=dict_row) as connection:
            rows = connection.execute('SELECT * FROM banking.claim_outbox(%s,%s,%s,%s)',
                                      (uuid4(), limit, lease_seconds, event_id)).fetchall()
        return [Lease(**row) for row in rows]

    def _connection(self, lease):
        connection = psycopg.connect(self._worker_dsn, row_factory=dict_row)
        connection.execute("SELECT set_config('bank.customer_id',%s,true)", (str(lease.customer_id),))
        return connection

    def source(self, lease):
        SOURCE_READS.inc()
        with self._connection(lease) as connection:
            return connection.execute('SELECT * FROM banking.read_consumer_source(%s,%s,%s)',
                                      (lease.event_id, lease.owner, lease.generation)).fetchone()

    def heartbeat(self, lease, seconds=15):
        with self._connection(lease) as connection:
            return connection.execute('SELECT banking.heartbeat_consumer(%s,%s,%s,%s)',
                                      (lease.event_id, lease.owner, lease.generation, seconds)).fetchone()

    def checkpoint(self, lease, state):
        with self._connection(lease) as connection:
            return connection.execute('SELECT banking.checkpoint_consumer(%s,%s,%s,%s)',
                                      (lease.event_id, lease.owner, lease.generation, Jsonb(state))).fetchone()

    def complete(self, lease, result=None):
        with self._connection(lease) as connection:
            return connection.execute('SELECT banking.complete_consumer(%s,%s,%s,%s)',
                                      (lease.event_id, lease.owner, lease.generation, Jsonb(result) if result is not None else None)).fetchone()['complete_consumer']

    def fail(self, lease, code):
        with self._connection(lease) as connection:
            return connection.execute('SELECT banking.fail_consumer(%s,%s,%s,%s)',
                                      (lease.event_id, lease.owner, lease.generation, code)).fetchone()['fail_consumer']

    def lookup(self, customer_id, event_id):
        with psycopg.connect(self._worker_dsn, row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('bank.customer_id',%s,true)", (str(customer_id),))
            # Missing means unknown/possibly in flight, never permission to mint a new key.
            return connection.execute('SELECT * FROM banking.lookup_consumer(%s)', (event_id,)).fetchone()

    def run_one(self, lease):
        """Bounded handler allowlist; failures retain only a classified code.

        No arbitrary callback, model, external request or database credential leaves
        this consumer. SQL transaction is the local receipt/effect boundary.
        """
        try:
            known = self.lookup(lease.customer_id, lease.event_id)
            if known and known['status'] == 'completed':
                DUPLICATES.inc()
                return 'duplicate'
            if known and known['status'] == 'dead_letter':
                return 'dead_letter'
            if lease.generation > 3:
                return self.fail(lease, 'retry_exhausted')
            source = self.source(lease)
            if source['event_type'] != 'user.accepted':
                return self.fail(lease, 'unsupported_type')
            if source['schema_version'] != 1:
                return self.fail(lease, 'unsupported_schema')
            if source['payload'].get('handler') == 'preferences.v1':
                from store.preference_grammar import extract
                payload=source['payload']
                proposals=payload.get('proposals',[]) if source['source_type']=='panel' else extract(payload.get('text'),payload.get('conversation_id'))
                with self._connection(lease) as connection:
                    return connection.execute('SELECT banking.complete_preference(%s,%s,%s,%s)',
                        (lease.event_id,lease.owner,lease.generation,Jsonb(proposals))).fetchone()['complete_preference']
            return self.complete(lease)
        except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
            try:
                return self.fail(lease, 'transient_unavailable')
            except (psycopg.OperationalError, psycopg.InterfaceError):
                return 'unknown'

        except (psycopg.OperationalError, psycopg.InterfaceError):
            # Commit may have succeeded. Reconcile same event, never make a new event.
            try:
                state = self.lookup(lease.customer_id, lease.event_id)
                return 'committed' if state and state['status'] == 'completed' else 'unknown'
            except (psycopg.OperationalError, psycopg.InterfaceError):
                return 'unknown'

    def run_preference(self, customer_id, event_id):
        """Bounded, owner-checked exact-event dispatch; no generic HTTP worker trigger."""
        with psycopg.connect(self._worker_dsn,row_factory=dict_row) as connection:
            connection.execute("SELECT set_config('bank.customer_id',%s,true)",(str(customer_id),))
            rows=connection.execute('SELECT * FROM banking.claim_preference(%s,%s)',(event_id,uuid4())).fetchall()
        return [self.run_one(Lease(**row)) for row in rows]
