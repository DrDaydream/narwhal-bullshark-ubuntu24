//! Benchmark-only fault injection around proposal creation and transport.
//!
//! This module never changes signatures, quorum checks, or consensus rules.
//! Alternating unavailable leaders are temporary per-slot faults, including at
//! authorities outside the fixed Byzantine set; this is a stronger fault model.
mod policy {
    //! Deterministic scheduling policy, kept independent of the async transport.
    use std::{
        collections::{BTreeMap, BTreeSet},
        time::{Duration, Instant},
    };

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub enum Mode {
        None,
        Silent,
        Targeted,
        Network,
    }

    #[derive(Clone, Debug)]
    pub struct Settings {
        pub mode: Mode,
        pub seed: u64,
        pub start_round: u64,
        pub rounds: u64,
        pub delay_ms: u64,
        pub max_pending_messages: usize,
        pub max_pending_bytes: usize,
        pub bullshark_alternating: bool,
    }

    impl Default for Settings {
        fn default() -> Self {
            Self {
                mode: Mode::None,
                seed: 1,
                start_round: 1,
                rounds: 0,
                delay_ms: 200,
                max_pending_messages: 4096,
                max_pending_bytes: 67108864,
                bullshark_alternating: true,
            }
        }
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub enum Action {
        Deliver,
        Drop,
        Delay,
    }
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub enum Suppression {
        Silent,
        AlternatingLeader,
    }

    pub struct Policy<K> {
        pub settings: Settings,
        authorities: Vec<K>,
        byzantine: BTreeSet<K>,
    }

    impl<K: Ord> Policy<K> {
        pub fn new(
            settings: Settings,
            mut authorities: Vec<K>,
            byzantine: Vec<K>,
        ) -> Result<Self, String> {
            authorities.sort();
            if authorities.is_empty() {
                return Err("committee cannot be empty".into());
            }
            if authorities.windows(2).any(|x| x[0] == x[1]) {
                return Err("duplicate committee member".into());
            }
            if byzantine
                .iter()
                .any(|key| authorities.binary_search(key).is_err())
            {
                return Err("Byzantine authority is not in the committee".into());
            }
            if byzantine.len() > (authorities.len() - 1) / 3 {
                return Err("fixed Byzantine set exceeds floor((n-1)/3)".into());
            }
            if settings.start_round == 0 {
                return Err("start_round must be positive".into());
            }
            if settings.delay_ms == 0 || settings.delay_ms > 86_400_000 {
                return Err("delay_ms must be in 1..=86400000".into());
            }
            if matches!(settings.mode, Mode::Silent | Mode::Targeted)
                && byzantine.is_empty()
                && !settings.bullshark_alternating
            {
                return Err(
                    "silent/targeted requires fixed Byzantine authorities or alternating slots"
                        .into(),
                );
            }
            if settings.rounds > 0 && settings.start_round.checked_add(settings.rounds).is_none() {
                return Err("attack window overflows u64".into());
            }
            if settings.max_pending_messages == 0 || settings.max_pending_bytes == 0 {
                return Err("queue limits must be positive".into());
            }
            if Instant::now()
                .checked_add(Duration::from_millis(settings.delay_ms))
                .is_none()
            {
                return Err("delay_ms exceeds the monotonic clock range".into());
            }
            Ok(Self {
                settings,
                authorities,
                byzantine: byzantine.into_iter().collect(),
            })
        }
        pub fn leader(&self, round: u64) -> Option<&K> {
            if round < 2 || round % 2 != 0 {
                return None;
            }
            self.authorities
                .get((round % self.authorities.len() as u64) as usize)
        }
        pub fn active(&self, round: u64) -> bool {
            self.settings.mode != Mode::None
                && round >= self.settings.start_round
                && (self.settings.rounds == 0
                    || round - self.settings.start_round < self.settings.rounds)
        }
        pub fn suppression(&self, name: &K, round: u64) -> Option<Suppression> {
            if !self.active(round) {
                return None;
            }
            // The first global leader slot is round 2. Slot 2 (round 4), slot 4
            // (round 8), ... are temporarily unavailable, regardless of identity.
            if self.settings.bullshark_alternating
                && round >= 2
                && (round / 2) % 2 == 0
                && self.leader(round) == Some(name)
            {
                return Some(Suppression::AlternatingLeader);
            }
            if self.normal_leader_slot(name, round) {
                return None;
            }
            if self.settings.mode == Mode::Silent && self.byzantine.contains(name) {
                return Some(Suppression::Silent);
            }
            None
        }
        pub fn action(
            &self,
            sender: &K,
            recipient: &K,
            local_round: u64,
            evidence: Option<(u64, &K)>,
        ) -> Action {
            if !self.active(local_round) {
                return Action::Deliver;
            }
            if self.settings.mode == Mode::Silent {
                // The requested normal/down alternation takes precedence over
                // fixed-set silence for the normal leader's own signed evidence.
                if let Some((round, origin)) = evidence {
                    if sender == origin && self.normal_leader_slot(origin, round) {
                        return Action::Deliver;
                    }
                }
                return if self.byzantine.contains(sender) {
                    Action::Drop
                } else {
                    Action::Deliver
                };
            }
            if self.settings.mode == Mode::Targeted && !self.byzantine.contains(sender) {
                return Action::Deliver;
            }
            if self.settings.delay_ms == 0 {
                return Action::Deliver;
            }
            let (round, origin) = match evidence {
                Some(x) => x,
                None => return Action::Deliver,
            };
            if !self.active(round) {
                return Action::Deliver;
            }
            let leader_round = round - round % 2;
            let leader = match self.leader(leader_round) {
                Some(x) => x,
                None => return Action::Deliver,
            };
            let receiver = match self.authorities.binary_search(recipient) {
                Ok(x) => x,
                Err(_) => return Action::Deliver,
            };
            let leader_index = self.authorities.binary_search(leader).unwrap();
            let n = self.authorities.len();
            let rotation = self.settings.seed.wrapping_sub(1) % n as u64;
            let offset = (receiver + n - (leader_index + rotation as usize) % n) % n;
            let late_group = offset > n / 2;
            // Leader evidence and other origins use opposite recipient groups. The
            // schedule changes delivery order only; it never edits signed evidence.
            if late_group == (origin == leader) {
                Action::Delay
            } else {
                Action::Deliver
            }
        }

        fn normal_leader_slot(&self, name: &K, round: u64) -> bool {
            self.settings.bullshark_alternating
                && self.active(round)
                && (round / 2) % 2 == 1
                && self.leader(round) == Some(name)
        }
    }

    pub struct DelayQueue<T> {
        max_messages: usize,
        max_bytes: usize,
        pub pending_bytes: usize,
        next_id: u64,
        entries: BTreeMap<(Instant, u64), (usize, T)>,
    }

    impl<T> DelayQueue<T> {
        pub fn new(max_messages: usize, max_bytes: usize) -> Self {
            Self {
                max_messages,
                max_bytes,
                pending_bytes: 0,
                next_id: 0,
                entries: BTreeMap::new(),
            }
        }
        pub fn push(&mut self, deadline: Instant, bytes: usize, item: T) -> Result<(), T> {
            if self.entries.len() >= self.max_messages
                || bytes > self.max_bytes - self.pending_bytes
                || self.next_id == u64::MAX
            {
                return Err(item);
            }
            self.entries.insert((deadline, self.next_id), (bytes, item));
            self.next_id += 1;
            self.pending_bytes += bytes;
            Ok(())
        }
        pub fn next_deadline(&self) -> Option<Instant> {
            self.entries.keys().next().map(|x| x.0)
        }
        pub fn pop_due(&mut self, now: Instant) -> Option<T> {
            let key = *self.entries.keys().next()?;
            if key.0 > now {
                return None;
            }
            let (bytes, item) = self.entries.remove(&key).unwrap();
            self.pending_bytes -= bytes;
            Some(item)
        }
    }

    #[cfg(test)]
    mod tests {
        include!("adversary/policy_tests.rs");
    }
}

use config::SharedCommittee;
use crypto::traits::{EncodeDecodeBase64, VerifyingKey};
use futures::{future::BoxFuture, stream::FuturesUnordered, FutureExt, StreamExt};
use multiaddr::Multiaddr;
use network::{CancelHandler, PrimaryNetwork};
use policy::{Action, DelayQueue, Mode, Policy, Settings, Suppression};
use rand::{prelude::SliceRandom, rngs::SmallRng, SeedableRng};
use serde::Deserialize;
use std::{
    collections::{BTreeSet, HashMap},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, Weak,
    },
    time::{Duration, Instant},
};
use tokio::sync::Notify;
use tracing::info;
use types::{BincodeEncodedPayload, PrimaryMessage, Round};

#[cfg(test)]
mod tests;

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Config {
    pub mode: String,
    pub byzantine_authorities: Vec<String>,
    pub seed: u64,
    pub start_round: u64,
    pub rounds: u64,
    pub delay_ms: u64,
    pub max_pending_messages: usize,
    pub max_pending_bytes: usize,
    pub bullshark_alternating: bool,
}

impl Default for Config {
    fn default() -> Self {
        let settings = Settings::default();
        Self {
            mode: "none".into(),
            byzantine_authorities: vec![],
            seed: settings.seed,
            start_round: settings.start_round,
            rounds: settings.rounds,
            delay_ms: settings.delay_ms,
            max_pending_messages: settings.max_pending_messages,
            max_pending_bytes: settings.max_pending_bytes,
            bullshark_alternating: settings.bullshark_alternating,
        }
    }
}

/// Baseline sends retain GC cancellation. Admitted delayed sends are owned by
/// the scheduler until delivery completes, even when the caller advances rounds.
pub(crate) struct DeliveryHandle {
    _direct: Option<CancelHandler<()>>,
}

struct PendingSend {
    address: Multiaddr,
    message: BincodeEncodedPayload,
    reliable: bool,
    trace: MessageTrace,
}

#[derive(Clone, Debug)]
struct MessageTrace {
    mode: &'static str,
    kind: &'static str,
    local_round: Round,
    evidence_round: Option<Round>,
    leader_round: Option<Round>,
    target_leader: Option<String>,
    sender: String,
    recipient: String,
    evidence_origin: Option<String>,
}

impl MessageTrace {
    fn log(&self, event: &'static str, reason: &'static str, bytes: Option<usize>) {
        info!(target: "benchmark_adversary", event, reason, mode = self.mode,
            kind = self.kind, local_round = self.local_round,
            evidence_round = ?self.evidence_round, leader_round = ?self.leader_round,
            target_leader = ?self.target_leader, sender = %self.sender,
            recipient = %self.recipient, evidence_origin = ?self.evidence_origin,
            serialized_bytes = ?bytes, "Benchmark adversary delivery");
    }
}

struct Scheduler {
    state: Mutex<SchedulerState>,
    changed: Notify,
}

struct SchedulerState {
    queue: DelayQueue<PendingSend>,
    // Reservations span both delayed queue entries and in-flight transport.
    pending_messages: usize,
    pending_bytes: usize,
    max_messages: usize,
    max_bytes: usize,
}

pub(crate) struct Adversary<PublicKey: VerifyingKey> {
    name: PublicKey,
    policy: Policy<PublicKey>,
    recipients: HashMap<Multiaddr, PublicKey>,
    local_round: AtomicU64,
    scheduler: Arc<Scheduler>,
}

impl<PublicKey: VerifyingKey> Drop for Adversary<PublicKey> {
    fn drop(&mut self) {
        self.scheduler.changed.notify_one();
    }
}

impl<PublicKey: VerifyingKey> Adversary<PublicKey> {
    pub(crate) fn from_environment(
        name: PublicKey,
        committee: SharedCommittee<PublicKey>,
    ) -> Result<Option<Arc<Self>>, String> {
        let encoded = match std::env::var("BENCH_ADVERSARY_CONFIG") {
            Ok(value) => value,
            Err(std::env::VarError::NotPresent) => return Ok(None),
            Err(error) => return Err(format!("BENCH_ADVERSARY_CONFIG: {error}")),
        };
        let config: Config = serde_json::from_str(&encoded)
            .map_err(|error| format!("BENCH_ADVERSARY_CONFIG: {error}"))?;
        let adversary = Self::new(name, committee, config)?;
        if adversary.policy.settings.mode == Mode::None {
            Ok(None)
        } else {
            Ok(Some(adversary))
        }
    }

    pub(crate) fn new(
        name: PublicKey,
        committee: SharedCommittee<PublicKey>,
        config: Config,
    ) -> Result<Arc<Self>, String> {
        let mode = match config.mode.as_str() {
            "none" => Mode::None,
            "silent" => Mode::Silent,
            "targeted" => Mode::Targeted,
            "network" => Mode::Network,
            _ => {
                return Err(format!(
                    "invalid adversary mode {:?}; expected none/silent/targeted/network",
                    config.mode
                ))
            }
        };
        if committee.primary(&name).is_err() {
            return Err("local authority is not in the committee".into());
        }
        let mut seen = BTreeSet::new();
        let mut byzantine = Vec::new();
        for encoded in &config.byzantine_authorities {
            let key = PublicKey::decode_base64(encoded)
                .map_err(|error| format!("invalid full Byzantine public key: {error}"))?;
            if key.encode_base64() != *encoded {
                return Err("Byzantine public keys must use canonical full base64".into());
            }
            if !seen.insert(key.clone()) {
                return Err("duplicate Byzantine authority".into());
            }
            byzantine.push(key);
        }
        let settings = Settings {
            mode,
            seed: config.seed,
            start_round: config.start_round,
            rounds: config.rounds,
            delay_ms: config.delay_ms,
            max_pending_messages: config.max_pending_messages,
            max_pending_bytes: config.max_pending_bytes,
            bullshark_alternating: config.bullshark_alternating,
        };
        let authorities = committee.authorities.load();
        let policy = Policy::new(settings, authorities.keys().cloned().collect(), byzantine)?;
        let recipients = authorities
            .iter()
            .map(|(name, authority)| (authority.primary.primary_to_primary.clone(), name.clone()))
            .collect();
        let scheduler = Arc::new(Scheduler {
            state: Mutex::new(SchedulerState {
                queue: DelayQueue::new(config.max_pending_messages, config.max_pending_bytes),
                pending_messages: 0,
                pending_bytes: 0,
                max_messages: config.max_pending_messages,
                max_bytes: config.max_pending_bytes,
            }),
            changed: Notify::new(),
        });
        info!(target: "benchmark_adversary", authority = %name.encode_base64(), mode = %config.mode,
            seed = config.seed, start_round = config.start_round, rounds = config.rounds,
            delay_ms = config.delay_ms, max_pending_messages = config.max_pending_messages,
            max_pending_bytes = config.max_pending_bytes, byzantine_authorities = ?config.byzantine_authorities,
            bullshark_alternating = config.bullshark_alternating,
            fault_model = if config.bullshark_alternating && mode != Mode::None { "fixed_set_plus_temporary_per_slot_leader_faults" } else { "fixed_byzantine_set" },
            normal_slot_exception = config.bullshark_alternating && mode == Mode::Silent,
            "Benchmark adversary configured");
        if matches!(mode, Mode::Targeted | Mode::Network) {
            tokio::spawn(Self::run_scheduler(Arc::downgrade(&scheduler)));
        }
        Ok(Arc::new(Self {
            name,
            policy,
            recipients,
            local_round: AtomicU64::new(1),
            scheduler,
        }))
    }

    pub(crate) fn observe_round(&self, round: Round) {
        self.local_round.fetch_max(round, Ordering::Relaxed);
    }

    pub(crate) fn suppress_proposal(&self, round: Round) -> bool {
        if let Some(reason) = self.policy.suppression(&self.name, round) {
            info!(target: "benchmark_adversary", authority = %self.name.encode_base64(), round,
                reason = ?reason, temporary_slot_fault = reason == Suppression::AlternatingLeader,
                "Benchmark adversary suppressed proposal before signing");
            true
        } else {
            false
        }
    }

    fn action(&self, address: &Multiaddr, message: &PrimaryMessage<PublicKey>) -> Action {
        let recipient = match self.recipients.get(address) {
            Some(key) => key,
            None => return Action::Deliver,
        };
        let local_round = self.local_round.load(Ordering::Relaxed);
        let action = |round, origin| {
            self.policy
                .action(&self.name, recipient, local_round, Some((round, origin)))
        };
        match message {
            PrimaryMessage::Header(header) => action(header.round, &header.author),
            PrimaryMessage::Vote(vote) => action(vote.round, &vote.origin),
            PrimaryMessage::Certificate(certificate) => {
                action(certificate.round(), &certificate.header.author)
            }
            PrimaryMessage::CertificatesBatchResponse { certificates, .. } => {
                // A repair batch is delayed if any included signed evidence is
                // delayed for this actual sender/recipient pair. Honest relays
                // stay untouched in targeted mode, even for Byzantine origins.
                let mut result = Action::Deliver;
                let mut has_evidence = false;
                for certificate in certificates
                    .iter()
                    .filter_map(|(_, certificate)| certificate.as_ref())
                {
                    has_evidence = true;
                    match action(certificate.round(), &certificate.header.author) {
                        Action::Drop => return Action::Drop,
                        Action::Delay => result = Action::Delay,
                        Action::Deliver => (),
                    }
                }
                if has_evidence {
                    result
                } else {
                    self.policy.action(&self.name, recipient, local_round, None)
                }
            }
            _ => self.policy.action(&self.name, recipient, local_round, None),
        }
    }

    fn filter_silent_batch(
        &self,
        address: &Multiaddr,
        message: &PrimaryMessage<PublicKey>,
    ) -> Option<PrimaryMessage<PublicKey>> {
        if self.policy.settings.mode != Mode::Silent {
            return None;
        }
        let recipient = self.recipients.get(address)?;
        let local_round = self.local_round.load(Ordering::Relaxed);
        if self.policy.action(&self.name, recipient, local_round, None) != Action::Drop {
            return None;
        }
        if let PrimaryMessage::CertificatesBatchResponse { certificates, from } = message {
            // Repair may batch normal leader evidence with silent evidence.
            // Withhold only the latter so the strict normal-slot exception also
            // holds for mixed repair requests. Existing digests stay intact.
            Some(PrimaryMessage::CertificatesBatchResponse {
                certificates: certificates
                    .iter()
                    .map(|(digest, certificate)| {
                        let allowed = certificate
                            .as_ref()
                            .filter(|certificate| {
                                self.policy.action(
                                    &self.name,
                                    recipient,
                                    local_round,
                                    Some((certificate.round(), &certificate.header.author)),
                                ) != Action::Drop
                            })
                            .cloned();
                        (*digest, allowed)
                    })
                    .collect(),
                from: from.clone(),
            })
        } else {
            None
        }
    }

    fn trace(
        &self,
        address: &Multiaddr,
        message: &PrimaryMessage<PublicKey>,
        selected_action: Action,
    ) -> MessageTrace {
        let recipient = self.recipients.get(address);
        let local_round = self.local_round.load(Ordering::Relaxed);
        let (kind, evidence) = match message {
            PrimaryMessage::Header(header) => ("header", Some((header.round, &header.author))),
            PrimaryMessage::Vote(vote) => ("vote", Some((vote.round, &vote.origin))),
            PrimaryMessage::Certificate(certificate) => (
                "certificate",
                Some((certificate.round(), &certificate.header.author)),
            ),
            PrimaryMessage::CertificatesBatchResponse { certificates, .. } => {
                // Record the evidence that selected this batch for delay/drop.
                let selected = certificates
                    .iter()
                    .filter_map(|(_, certificate)| certificate.as_ref())
                    .find(|certificate| {
                        recipient.map_or(false, |recipient| {
                            self.policy.action(
                                &self.name,
                                recipient,
                                local_round,
                                Some((certificate.round(), &certificate.header.author)),
                            ) == selected_action
                        })
                    });
                (
                    "certificate_repair_batch",
                    selected.map(|certificate| (certificate.round(), &certificate.header.author)),
                )
            }
            PrimaryMessage::CertificatesRequest(..)
            | PrimaryMessage::CertificatesBatchRequest { .. } => ("certificate_request", None),
            PrimaryMessage::PayloadAvailabilityRequest { .. } => {
                ("payload_availability_request", None)
            }
            PrimaryMessage::PayloadAvailabilityResponse { .. } => {
                ("payload_availability_response", None)
            }
        };
        let leader_round = evidence.and_then(|(round, _)| {
            if round >= 2 {
                Some(round - round % 2)
            } else {
                None
            }
        });
        MessageTrace {
            mode: match self.policy.settings.mode {
                Mode::None => "none",
                Mode::Silent => "silent",
                Mode::Targeted => "targeted",
                Mode::Network => "network",
            },
            kind,
            local_round,
            evidence_round: evidence.map(|(round, _)| round),
            leader_round,
            target_leader: leader_round
                .and_then(|round| self.policy.leader(round))
                .map(EncodeDecodeBase64::encode_base64),
            sender: self.name.encode_base64(),
            recipient: recipient
                .map(EncodeDecodeBase64::encode_base64)
                .unwrap_or_else(|| address.to_string()),
            evidence_origin: evidence.map(|(_, origin)| origin.encode_base64()),
        }
    }

    fn enqueue(&self, pending: PendingSend) -> Result<(), PendingSend> {
        let deadline = Instant::now() + Duration::from_millis(self.policy.settings.delay_ms);
        let bytes = pending.message.payload.len();
        let trace = pending.trace.clone();
        let mut state = self
            .scheduler
            .state
            .lock()
            .expect("adversary queue mutex poisoned");
        let result = if state.pending_messages >= state.max_messages
            || bytes > state.max_bytes - state.pending_bytes
        {
            Err(pending)
        } else {
            let result = state.queue.push(deadline, bytes, pending);
            if result.is_ok() {
                state.pending_messages += 1;
                state.pending_bytes += bytes;
            }
            result
        };
        if result.is_ok() {
            trace.log("delay", "selected_recipient_group", Some(bytes));
            self.scheduler.changed.notify_one();
        } else {
            trace.log(
                "bypass",
                "pending_budget_exhausted_deliver_immediately",
                Some(bytes),
            );
        }
        result
    }

    async fn run_scheduler(weak: Weak<Scheduler>) {
        let mut network = PrimaryNetwork::default();
        let mut in_flight = FuturesUnordered::<BoxFuture<'static, usize>>::new();
        loop {
            let scheduler = match weak.upgrade() {
                Some(scheduler) => scheduler,
                None => return,
            };
            loop {
                let pending = scheduler
                    .state
                    .lock()
                    .expect("adversary queue mutex poisoned")
                    .queue
                    .pop_due(Instant::now());
                let pending = match pending {
                    Some(pending) => pending,
                    None => break,
                };
                let bytes = pending.message.payload.len();
                pending
                    .trace
                    .log("release", "finite_delay_elapsed", Some(bytes));
                let handle = if pending.reliable {
                    network
                        .send_serialized(pending.address, pending.message)
                        .await
                } else {
                    CancelHandler::new(
                        network
                            .unreliable_send_serialized(pending.address, pending.message)
                            .await,
                    )
                };
                in_flight.push(
                    async move {
                        handle.await;
                        bytes
                    }
                    .boxed(),
                );
            }
            let deadline = scheduler
                .state
                .lock()
                .expect("adversary queue mutex poisoned")
                .queue
                .next_deadline();
            // One bounded scheduler task, never a sleeping task per message.
            // The idle wake also lets the task finish when all owners disappear.
            let deadline = deadline.unwrap_or_else(|| Instant::now() + Duration::from_secs(1));
            tokio::select! {
                _ = tokio::time::sleep_until(deadline.into()) => (),
                _ = scheduler.changed.notified() => (),
                Some(bytes) = in_flight.next(), if !in_flight.is_empty() => {
                    let mut state = scheduler.state.lock().expect("adversary queue mutex poisoned");
                    state.pending_messages -= 1;
                    state.pending_bytes -= bytes;
                }
            }
        }
    }
}

pub(crate) struct ScheduledNetwork<PublicKey: VerifyingKey> {
    network: PrimaryNetwork,
    adversary: Option<Arc<Adversary<PublicKey>>>,
}

impl<PublicKey: VerifyingKey> From<PrimaryNetwork> for ScheduledNetwork<PublicKey> {
    fn from(network: PrimaryNetwork) -> Self {
        Self::new(network, None)
    }
}

impl<PublicKey: VerifyingKey> Default for ScheduledNetwork<PublicKey> {
    fn default() -> Self {
        Self::new(PrimaryNetwork::default(), None)
    }
}

impl<PublicKey: VerifyingKey> ScheduledNetwork<PublicKey> {
    pub(crate) fn new(
        network: PrimaryNetwork,
        adversary: Option<Arc<Adversary<PublicKey>>>,
    ) -> Self {
        Self { network, adversary }
    }

    pub(crate) async fn send(
        &mut self,
        address: Multiaddr,
        message: &PrimaryMessage<PublicKey>,
    ) -> DeliveryHandle {
        let filtered = self
            .adversary
            .as_ref()
            .and_then(|adversary| adversary.filter_silent_batch(&address, message));
        let message = filtered.as_ref().unwrap_or(message);
        let action = self
            .adversary
            .as_ref()
            .map_or(Action::Deliver, |adversary| {
                adversary.action(&address, message)
            });
        match action {
            Action::Deliver => DeliveryHandle {
                _direct: Some(self.network.send(address, message).await),
            },
            Action::Drop => {
                self.adversary
                    .as_ref()
                    .unwrap()
                    .trace(&address, message, action)
                    .log("drop", "fixed_byzantine_silence", None);
                DeliveryHandle { _direct: None }
            }
            Action::Delay => {
                let trace = self
                    .adversary
                    .as_ref()
                    .unwrap()
                    .trace(&address, message, action);
                let message =
                    BincodeEncodedPayload::try_from(message).expect("Failed to serialize payload");
                let pending = PendingSend {
                    address,
                    message,
                    reliable: true,
                    trace,
                };
                if let Err(pending) = self.adversary.as_ref().unwrap().enqueue(pending) {
                    return DeliveryHandle {
                        _direct: Some(
                            self.network
                                .send_serialized(pending.address, pending.message)
                                .await,
                        ),
                    };
                }
                DeliveryHandle { _direct: None }
            }
        }
    }

    pub(crate) async fn broadcast(
        &mut self,
        addresses: Vec<Multiaddr>,
        message: &PrimaryMessage<PublicKey>,
    ) -> Vec<DeliveryHandle> {
        if self.adversary.is_none() {
            return self
                .network
                .broadcast(addresses, message)
                .await
                .into_iter()
                .map(|handler| DeliveryHandle {
                    _direct: Some(handler),
                })
                .collect();
        }
        let mut handles = Vec::with_capacity(addresses.len());
        for address in addresses {
            handles.push(self.send(address, message).await);
        }
        handles
    }

    pub(crate) async fn unreliable_send(
        &mut self,
        address: Multiaddr,
        message: &PrimaryMessage<PublicKey>,
    ) {
        let filtered = self
            .adversary
            .as_ref()
            .and_then(|adversary| adversary.filter_silent_batch(&address, message));
        let message = filtered.as_ref().unwrap_or(message);
        let action = self
            .adversary
            .as_ref()
            .map_or(Action::Deliver, |adversary| {
                adversary.action(&address, message)
            });
        match action {
            Action::Deliver => {
                self.network.unreliable_send(address, message).await;
            }
            Action::Drop => {
                self.adversary
                    .as_ref()
                    .unwrap()
                    .trace(&address, message, action)
                    .log("drop", "fixed_byzantine_silence", None);
            }
            Action::Delay => {
                let trace = self
                    .adversary
                    .as_ref()
                    .unwrap()
                    .trace(&address, message, action);
                let message =
                    BincodeEncodedPayload::try_from(message).expect("Failed to serialize payload");
                let pending = PendingSend {
                    address,
                    message,
                    reliable: false,
                    trace,
                };
                if let Err(pending) = self.adversary.as_ref().unwrap().enqueue(pending) {
                    self.network
                        .unreliable_send_serialized(pending.address, pending.message)
                        .await;
                }
            }
        }
    }

    pub(crate) async fn unreliable_broadcast(
        &mut self,
        addresses: Vec<Multiaddr>,
        message: &PrimaryMessage<PublicKey>,
    ) {
        if self.adversary.is_none() {
            self.network.unreliable_broadcast(addresses, message).await;
            return;
        }
        for address in addresses {
            self.unreliable_send(address, message).await;
        }
    }

    pub(crate) async fn lucky_broadcast(
        &mut self,
        mut addresses: Vec<Multiaddr>,
        message: &PrimaryMessage<PublicKey>,
        nodes: usize,
    ) {
        if self.adversary.as_ref().map_or(true, |adversary| {
            addresses
                .iter()
                .all(|address| adversary.action(address, message) == Action::Deliver)
        }) {
            self.network
                .lucky_broadcast(addresses, message, nodes)
                .await;
            return;
        }
        let seed = self.adversary.as_ref().unwrap().policy.settings.seed;
        addresses.shuffle(&mut SmallRng::seed_from_u64(seed));
        addresses.truncate(nodes);
        self.unreliable_broadcast(addresses, message).await;
    }
}
