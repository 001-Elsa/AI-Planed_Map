# Worker Fault Recovery

This document defines the evidence boundary for MapGo's durable Worker path. The tests in
`backend/tests/integration/test_worker_fault_recovery_real.py` require both PostgreSQL and Redis.
They skip instead of substituting SQLite, Fake Redis or the in-memory runtime store.

## Delivery and commit boundary

Trip events use Redis reliable-list reservation (`BRPOPLPUSH`) and remain in
`mapgo:trip-events:processing` until ACK. The database event row is the idempotency boundary. A
crash after `worker_processed` commits but before `LREM` ACK leaves the Redis payload recoverable;
startup moves it back to the primary queue, and the replay observes the committed terminal status
without repeating side effects.

The Worker ACKs a failed delivery only after retry or DLQ persistence returns successfully. If that
Redis write also fails, the original item remains in the processing list for startup recovery.

Agent role messages use Redis Streams consumer groups. Unacknowledged entries stay in the Pending
Entries List. A recovery consumer uses `XAUTOCLAIM`, receives an incremented delivery count and
ACKs the same stream entry after successful handling.

## Lease fencing

Each successful Redis lock acquisition atomically increments `lock-fence:<name>` and stores an
opaque `<fence>:<owner>` lock value. `trip_events.worker_fencing_token` records the highest Worker
that claimed the event. Claiming uses a database compare-and-set and cannot replace an equal or
higher fence.

After the claim, every commit through the Worker AsyncSession runs a guard that:

1. rejects a lease-renewal failure signalled by the renewer task;
2. locks and reads the current `TripEvent` fence;
3. rejects the commit when a newer Worker has superseded it.

The database row lock closes the check-to-commit race. Redis lock counters are persistent and the
bundled Redis configuration uses `noeviction`; an external Redis deployment must provide the same
non-evicting durability boundary for queues, locks and fence counters.

## Atomic retry promotion

Due retries live in `<queue>:retry` as a sorted set. Promotion is one Redis Lua execution that
selects due members and performs `ZREM` plus `LPUSH` atomically. Competing promoters therefore
cannot lose a job in the former command gap or publish the same member twice.

## Reproduce

```bash
export DATABASE_URL=postgresql+asyncpg://mapgo:mapgo_test@localhost:5432/mapgo_test
export REDIS_URL=redis://localhost:6379/15
alembic upgrade head
python -m pytest -c backend/pytest.ini backend/tests/integration/test_worker_fault_recovery_real.py -vv
```

CI runs the same command against PostgreSQL 16 and Redis 7 and uploads
`artifacts/worker-fault-recovery.xml`.
