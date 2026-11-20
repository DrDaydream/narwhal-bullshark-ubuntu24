//! Observational batch readiness; it does not authorize or release commits.
use crypto::traits::VerifyingKey;
use std::collections::HashSet;
use types::{BatchDigest, Certificate};

#[derive(Default)]
pub(crate) struct RuleReadyLog {
    seen: HashSet<BatchDigest>,
}

impl RuleReadyLog {
    pub(crate) fn record<K: VerifyingKey>(&mut self, sequence: &[Certificate<K>], ready_at: u128) {
        for certificate in sequence {
            for digest in certificate.header.payload.keys() {
                if self.seen.insert(digest.clone()) {
                    tracing::info!("Benchmark batch rule-ready {:?} at_us {}", digest, ready_at);
                }
            }
        }
    }
}
