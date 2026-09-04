//! Bounded, off-path persistence for optimizer plan-audit rows.
//!
//! `optimize_plan` is allowed to construct an audit value and try to enqueue it,
//! but it never waits for SQLite. One worker owns the database connection and
//! commits rows in batches. The queue deliberately drops the newest row when it
//! is full: audit is diagnostic, while delaying a user-visible LLM call is not.
//!
//! A flush is an ordered barrier. When it succeeds, every row accepted before
//! the barrier has either committed or contributed to `write_failed_rows`.
//! Normal lifecycle shutdown flushes and then drops the writer. Abrupt process
//! termination can lose the worker's current batch plus the bounded queue.

use std::fmt;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use agentc_optimizer::audit::{insert_batch, PlanAudit};
use rusqlite::Connection;

pub(crate) const AUDIT_QUEUE_CAPACITY_ROWS: usize = 4_096;
pub(crate) const AUDIT_BATCH_SIZE_ROWS: usize = 128;
pub(crate) const AUDIT_FLUSH_TIMEOUT: Duration = Duration::from_secs(5);

enum AuditMessage {
    Row(PlanAudit),
    Flush(SyncSender<()>),
    Shutdown,
}

#[derive(Debug, Default)]
struct AuditCounters {
    attempted_rows: AtomicU64,
    accepted_rows: AtomicU64,
    written_rows: AtomicU64,
    dropped_full_rows: AtomicU64,
    dropped_disconnected_rows: AtomicU64,
    write_failed_rows: AtomicU64,
}

/// Process-local observability for the bounded audit queue.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct AuditWriterStats {
    pub(crate) attempted_rows: u64,
    pub(crate) accepted_rows: u64,
    pub(crate) written_rows: u64,
    pub(crate) pending_rows: u64,
    pub(crate) dropped_full_rows: u64,
    pub(crate) dropped_disconnected_rows: u64,
    pub(crate) write_failed_rows: u64,
    pub(crate) queue_capacity_rows: usize,
    pub(crate) batch_size_rows: usize,
}

/// Why an explicit audit flush could not establish its durability barrier.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum AuditFlushError {
    Disconnected,
    TimedOut,
}

impl fmt::Display for AuditFlushError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Disconnected => formatter.write_str("audit writer disconnected"),
            Self::TimedOut => formatter.write_str("audit flush timed out"),
        }
    }
}

/// Deep-module boundary around the queue, worker, batching, and lifecycle.
pub(crate) struct AuditWriter {
    sender: SyncSender<AuditMessage>,
    counters: Arc<AuditCounters>,
    worker: Mutex<Option<JoinHandle<()>>>,
    queue_capacity_rows: usize,
    batch_size_rows: usize,
}

impl AuditWriter {
    pub(crate) fn start(connection: Connection) -> std::io::Result<Self> {
        Self::start_inner(
            connection,
            AUDIT_QUEUE_CAPACITY_ROWS,
            AUDIT_BATCH_SIZE_ROWS,
            None,
        )
    }

    fn start_inner(
        connection: Connection,
        queue_capacity_rows: usize,
        batch_size_rows: usize,
        start_gate: Option<Receiver<()>>,
    ) -> std::io::Result<Self> {
        assert!(queue_capacity_rows > 0, "audit queue must be non-empty");
        assert!(batch_size_rows > 0, "audit batch must be non-empty");
        let (sender, receiver) = mpsc::sync_channel(queue_capacity_rows);
        let counters = Arc::new(AuditCounters::default());
        let worker_counters = Arc::clone(&counters);
        let worker = thread::Builder::new()
            .name("agentc-plan-audit".to_string())
            .spawn(move || {
                if let Some(gate) = start_gate {
                    let _ = gate.recv();
                }
                run_writer(connection, receiver, &worker_counters, batch_size_rows);
            })?;
        Ok(Self {
            sender,
            counters,
            worker: Mutex::new(Some(worker)),
            queue_capacity_rows,
            batch_size_rows,
        })
    }

    /// Try once and return immediately. `false` means the row was dropped and
    /// the corresponding loss counter was incremented.
    pub(crate) fn try_enqueue(&self, row: PlanAudit) -> bool {
        self.counters.attempted_rows.fetch_add(1, Ordering::SeqCst);
        match self.sender.try_send(AuditMessage::Row(row)) {
            Ok(()) => {
                self.counters.accepted_rows.fetch_add(1, Ordering::SeqCst);
                true
            }
            Err(TrySendError::Full(_)) => {
                self.counters
                    .dropped_full_rows
                    .fetch_add(1, Ordering::SeqCst);
                false
            }
            Err(TrySendError::Disconnected(_)) => {
                self.counters
                    .dropped_disconnected_rows
                    .fetch_add(1, Ordering::SeqCst);
                false
            }
        }
    }

    /// Wait until all rows ordered before this barrier have committed or been
    /// counted as write failures.
    pub(crate) fn flush(&self, timeout: Duration) -> Result<(), AuditFlushError> {
        let deadline = Instant::now() + timeout;
        let (ack_sender, ack_receiver) = mpsc::sync_channel(0);
        let mut message = AuditMessage::Flush(ack_sender);
        loop {
            match self.sender.try_send(message) {
                Ok(()) => break,
                Err(TrySendError::Full(returned)) => {
                    if Instant::now() >= deadline {
                        return Err(AuditFlushError::TimedOut);
                    }
                    message = returned;
                    thread::yield_now();
                }
                Err(TrySendError::Disconnected(_)) => {
                    return Err(AuditFlushError::Disconnected);
                }
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        ack_receiver
            .recv_timeout(remaining)
            .map_err(|error| match error {
                mpsc::RecvTimeoutError::Timeout => AuditFlushError::TimedOut,
                mpsc::RecvTimeoutError::Disconnected => AuditFlushError::Disconnected,
            })
    }

    pub(crate) fn stats(&self) -> AuditWriterStats {
        let accepted_rows = self.counters.accepted_rows.load(Ordering::SeqCst);
        let written_rows = self.counters.written_rows.load(Ordering::SeqCst);
        let write_failed_rows = self.counters.write_failed_rows.load(Ordering::SeqCst);
        AuditWriterStats {
            attempted_rows: self.counters.attempted_rows.load(Ordering::SeqCst),
            accepted_rows,
            written_rows,
            pending_rows: accepted_rows.saturating_sub(written_rows + write_failed_rows),
            dropped_full_rows: self.counters.dropped_full_rows.load(Ordering::SeqCst),
            dropped_disconnected_rows: self
                .counters
                .dropped_disconnected_rows
                .load(Ordering::SeqCst),
            write_failed_rows,
            queue_capacity_rows: self.queue_capacity_rows,
            batch_size_rows: self.batch_size_rows,
        }
    }
}

impl Drop for AuditWriter {
    fn drop(&mut self) {
        // The writer is owned by OptimizerState. Reaching Drop means the last
        // state Arc is gone, so no producer can race a row behind Shutdown.
        let _ = self.sender.send(AuditMessage::Shutdown);
        let worker = self
            .worker
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(worker) = worker {
            if worker.join().is_err() {
                eprintln!("[agentc-profiler] plan-audit writer panicked");
            }
        }
        let stats = self.stats();
        if stats.dropped_full_rows > 0
            || stats.dropped_disconnected_rows > 0
            || stats.write_failed_rows > 0
        {
            eprintln!(
                "[agentc-profiler] plan-audit loss: dropped_full={}, \
                 dropped_disconnected={}, write_failed={}",
                stats.dropped_full_rows, stats.dropped_disconnected_rows, stats.write_failed_rows,
            );
        }
    }
}

fn run_writer(
    mut connection: Connection,
    receiver: Receiver<AuditMessage>,
    counters: &AuditCounters,
    batch_size_rows: usize,
) {
    let mut batch = Vec::with_capacity(batch_size_rows);
    while let Ok(message) = receiver.recv() {
        match message {
            AuditMessage::Row(row) => {
                batch.push(row);
                let mut barrier = None;
                let mut shutdown = false;
                while batch.len() < batch_size_rows {
                    match receiver.try_recv() {
                        Ok(AuditMessage::Row(row)) => batch.push(row),
                        Ok(AuditMessage::Flush(ack)) => {
                            barrier = Some(ack);
                            break;
                        }
                        Ok(AuditMessage::Shutdown) => {
                            shutdown = true;
                            break;
                        }
                        Err(TryRecvError::Empty) => break,
                        Err(TryRecvError::Disconnected) => {
                            shutdown = true;
                            break;
                        }
                    }
                }
                persist_batch(&mut connection, &mut batch, counters);
                if let Some(ack) = barrier {
                    let _ = ack.send(());
                }
                if shutdown {
                    break;
                }
            }
            AuditMessage::Flush(ack) => {
                let _ = ack.send(());
            }
            AuditMessage::Shutdown => break,
        }
    }
}

fn persist_batch(
    connection: &mut Connection,
    batch: &mut Vec<PlanAudit>,
    counters: &AuditCounters,
) {
    if batch.is_empty() {
        return;
    }
    let rows = batch.len() as u64;
    match insert_batch(connection, batch) {
        Ok(written) => {
            counters
                .written_rows
                .fetch_add(written as u64, Ordering::SeqCst);
        }
        Err(error) => {
            counters.write_failed_rows.fetch_add(rows, Ordering::SeqCst);
            eprintln!("[agentc-profiler] plan-audit batch insert failed: {error}");
        }
    }
    batch.clear();
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentc_optimizer::audit::PlanKind;
    use agentc_optimizer::schema::ensure_audit_schema;
    use std::sync::mpsc::Sender;

    fn sample_row(index: i64) -> PlanAudit {
        PlanAudit {
            ts_us: index,
            call_site_id: "audit-writer-test".to_string(),
            span_id: (index as u64).to_be_bytes(),
            plan_kind: PlanKind::PassThrough,
            rule: None,
            projected_savings_usd: None,
            measured_savings_usd: None,
            overhead_us: 42,
            shadow_sampled: false,
            shadow_divergence: None,
            planner_diagnostics_json: None,
        }
    }

    fn file_connection(path: &std::path::Path) -> Connection {
        let connection = Connection::open(path).unwrap();
        ensure_audit_schema(&connection).unwrap();
        connection
    }

    fn release_gate(sender: Sender<()>) {
        sender.send(()).unwrap();
    }

    #[test]
    fn flush_makes_every_accepted_row_queryable() {
        let directory = tempfile::TempDir::new().unwrap();
        let path = directory.path().join("audit.db");
        let writer = AuditWriter::start(file_connection(&path)).unwrap();
        for index in 0..300 {
            assert!(writer.try_enqueue(sample_row(index)));
        }
        writer.flush(Duration::from_secs(2)).unwrap();

        let count: i64 = Connection::open(&path)
            .unwrap()
            .query_row("SELECT COUNT(*) FROM plan_audit", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 300);
        assert_eq!(
            writer.stats(),
            AuditWriterStats {
                attempted_rows: 300,
                accepted_rows: 300,
                written_rows: 300,
                pending_rows: 0,
                dropped_full_rows: 0,
                dropped_disconnected_rows: 0,
                write_failed_rows: 0,
                queue_capacity_rows: AUDIT_QUEUE_CAPACITY_ROWS,
                batch_size_rows: AUDIT_BATCH_SIZE_ROWS,
            }
        );
    }

    #[test]
    fn full_queue_drops_newest_without_blocking() {
        let directory = tempfile::TempDir::new().unwrap();
        let path = directory.path().join("audit.db");
        let (gate_sender, gate_receiver) = mpsc::channel();
        let writer =
            AuditWriter::start_inner(file_connection(&path), 1, 1, Some(gate_receiver)).unwrap();

        assert!(writer.try_enqueue(sample_row(1)));
        assert!(!writer.try_enqueue(sample_row(2)));
        assert_eq!(writer.stats().dropped_full_rows, 1);

        release_gate(gate_sender);
        writer.flush(Duration::from_secs(2)).unwrap();
        let stats = writer.stats();
        assert_eq!(stats.attempted_rows, 2);
        assert_eq!(stats.accepted_rows, 1);
        assert_eq!(stats.written_rows, 1);
        assert_eq!(stats.pending_rows, 0);
    }

    #[test]
    fn drop_drains_the_bounded_queue() {
        let directory = tempfile::TempDir::new().unwrap();
        let path = directory.path().join("audit.db");
        {
            let writer = AuditWriter::start(file_connection(&path)).unwrap();
            for index in 0..64 {
                assert!(writer.try_enqueue(sample_row(index)));
            }
        }
        let count: i64 = Connection::open(&path)
            .unwrap()
            .query_row("SELECT COUNT(*) FROM plan_audit", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 64);
    }

    #[test]
    fn missing_schema_counts_failed_rows_and_keeps_worker_alive() {
        let connection = Connection::open_in_memory().unwrap();
        let writer = AuditWriter::start(connection).unwrap();
        assert!(writer.try_enqueue(sample_row(1)));
        writer.flush(Duration::from_secs(2)).unwrap();
        assert_eq!(writer.stats().write_failed_rows, 1);
        assert!(writer.try_enqueue(sample_row(2)));
        writer.flush(Duration::from_secs(2)).unwrap();
        assert_eq!(writer.stats().write_failed_rows, 2);
    }
}
