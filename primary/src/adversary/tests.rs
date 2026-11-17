use super::*;
use crypto::{ed25519::Ed25519PublicKey, traits::KeyPair, Hash as _, SignatureService};
use test_utils::{committee, keys, PrimaryToPrimaryMockServer};
use tokio::time::{timeout, Duration};
use types::Header;

fn configured(
    committee: SharedCommittee<Ed25519PublicKey>,
    mode: &str,
    delay: u64,
    byzantine: Vec<String>,
) -> (Arc<Adversary<Ed25519PublicKey>>, Ed25519PublicKey) {
    let sorted: Vec<_> = committee.authorities.load().keys().cloned().collect();
    let sender = sorted[0].clone();
    let recipient = sorted[1].clone();
    let config = Config {
        mode: mode.into(),
        delay_ms: delay,
        byzantine_authorities: byzantine,
        ..Config::default()
    };
    (
        Adversary::new(sender, committee, config).unwrap(),
        recipient,
    )
}

async fn leader_header() -> Header<Ed25519PublicKey> {
    let committee = committee(None);
    let leader = committee.authorities.load().keys().nth(2).unwrap().clone();
    let key = keys(None)
        .into_iter()
        .find(|key| key.public() == &leader)
        .unwrap();
    Header::new(
        leader,
        2,
        committee.epoch(),
        Default::default(),
        Default::default(),
        &mut SignatureService::new(key),
    )
    .await
}

#[tokio::test]
async fn delayed_send_releases_without_subsequent_input() {
    let committee = committee(None);
    let (adversary, recipient) = configured(committee.clone(), "network", 60, vec![]);
    adversary.observe_round(2);
    let address = committee.primary(&recipient).unwrap().primary_to_primary;
    let mut receiver = PrimaryToPrimaryMockServer::spawn(address.clone());
    let mut network = ScheduledNetwork::new(PrimaryNetwork::default(), Some(adversary));
    let message = PrimaryMessage::Header(leader_header().await);
    let _handler = network.send(address, &message).await;
    assert!(timeout(Duration::from_millis(20), receiver.recv())
        .await
        .is_err());
    let payload = timeout(Duration::from_secs(2), receiver.recv())
        .await
        .unwrap()
        .unwrap();
    let received: PrimaryMessage<Ed25519PublicKey> =
        bincode::deserialize(&payload.payload).unwrap();
    match received {
        PrimaryMessage::Header(header) => assert_eq!(header.round, 2),
        _ => panic!("wrong message"),
    }
}

#[tokio::test]
async fn caller_gc_cannot_discard_admitted_delayed_message() {
    let committee = committee(None);
    let (adversary, recipient) = configured(committee.clone(), "network", 40, vec![]);
    adversary.observe_round(2);
    let address = committee.primary(&recipient).unwrap().primary_to_primary;
    let mut receiver = PrimaryToPrimaryMockServer::spawn(address.clone());
    let mut network = ScheduledNetwork::new(PrimaryNetwork::default(), Some(adversary));
    let handler = network
        .send(address, &PrimaryMessage::Header(leader_header().await))
        .await;
    drop(handler);
    assert!(timeout(Duration::from_secs(2), receiver.recv())
        .await
        .unwrap()
        .is_some());
}

#[tokio::test]
async fn honest_repair_is_unchanged_by_targeted_but_delayed_by_network() {
    let committee = committee(None);
    let sorted: Vec<_> = committee.authorities.load().keys().cloned().collect();
    let byzantine = vec![sorted[3].encode_base64()];
    let (targeted, recipient) = configured(committee.clone(), "targeted", 100, byzantine.clone());
    let (network, _) = configured(committee.clone(), "network", 100, byzantine);
    targeted.observe_round(2);
    network.observe_round(2);
    let certificate = types::Certificate {
        header: leader_header().await,
        ..Default::default()
    };
    let repair = PrimaryMessage::CertificatesBatchResponse {
        certificates: vec![(certificate.digest(), Some(certificate))],
        from: sorted[0].clone(),
    };
    let address = committee.primary(&recipient).unwrap().primary_to_primary;
    assert_eq!(targeted.action(&address, &repair), Action::Deliver);
    assert_eq!(network.action(&address, &repair), Action::Delay);
    let trace = network.trace(&address, &repair, Action::Delay);
    assert_eq!(trace.kind, "certificate_repair_batch");
    assert_eq!(trace.evidence_round, Some(2));
    assert_eq!(trace.leader_round, Some(2));
    assert_eq!(trace.target_leader, Some(sorted[2].encode_base64()));
    assert_eq!(trace.evidence_origin, Some(sorted[2].encode_base64()));
    assert_eq!(trace.sender, sorted[0].encode_base64());
    assert_eq!(trace.recipient, sorted[1].encode_base64());
}

#[tokio::test]
async fn normal_silent_leader_repair_keeps_its_evidence_but_withholds_other_evidence() {
    let committee = committee(None);
    let leader = committee.authorities.load().keys().nth(2).unwrap().clone();
    let recipient = committee.authorities.load().keys().next().unwrap().clone();
    let adversary = Adversary::new(
        leader.clone(),
        committee.clone(),
        Config {
            mode: "silent".into(),
            byzantine_authorities: vec![leader.encode_base64()],
            ..Config::default()
        },
    )
    .unwrap();
    adversary.observe_round(3);
    let normal = test_utils::certificate(&leader_header().await);
    let unrelated = test_utils::certificate(&test_utils::header());
    let request = PrimaryMessage::CertificatesBatchResponse {
        certificates: vec![
            (normal.digest(), Some(normal.clone())),
            (unrelated.digest(), Some(unrelated)),
        ],
        from: leader,
    };
    let address = committee.primary(&recipient).unwrap().primary_to_primary;
    let filtered = adversary.filter_silent_batch(&address, &request).unwrap();
    match &filtered {
        PrimaryMessage::CertificatesBatchResponse { certificates, .. } => {
            assert_eq!(certificates.len(), 2);
            assert_eq!(
                certificates[0].1.as_ref().unwrap().digest(),
                normal.digest()
            );
            assert!(certificates[1].1.is_none());
        }
        _ => panic!("expected batch repair"),
    }
    assert_eq!(adversary.action(&address, &filtered), Action::Deliver);
}

#[tokio::test]
async fn pending_budget_covers_transport_until_acknowledgement() {
    let committee = committee(None);
    let sorted: Vec<_> = committee.authorities.load().keys().cloned().collect();
    let adversary = Adversary::new(
        sorted[0].clone(),
        committee.clone(),
        Config {
            mode: "network".into(),
            delay_ms: 10,
            max_pending_messages: 1,
            ..Config::default()
        },
    )
    .unwrap();
    adversary.observe_round(2);
    let address = committee.primary(&sorted[1]).unwrap().primary_to_primary;
    let message = PrimaryMessage::Header(leader_header().await);
    let serialized = BincodeEncodedPayload::try_from(&message).unwrap();
    let bytes = serialized.payload.len();
    let mut network = ScheduledNetwork::new(PrimaryNetwork::default(), Some(adversary.clone()));
    drop(network.send(address.clone(), &message).await);
    // No listener: the reliable transport is in flight and retries. Its bytes
    // must remain charged after the delay queue has released the entry.
    tokio::time::sleep(Duration::from_millis(40)).await;
    {
        let state = adversary.scheduler.state.lock().unwrap();
        assert_eq!(state.pending_messages, 1);
        assert_eq!(state.pending_bytes, bytes);
        assert!(state.queue.next_deadline().is_none());
    }
    assert!(adversary
        .enqueue(PendingSend {
            address: address.clone(),
            message: serialized,
            reliable: true,
            trace: adversary.trace(&address, &message, Action::Delay),
        })
        .is_err());
    let mut receiver = PrimaryToPrimaryMockServer::spawn(address);
    assert!(timeout(Duration::from_secs(5), receiver.recv())
        .await
        .unwrap()
        .is_some());
    timeout(Duration::from_secs(2), async {
        loop {
            if adversary.scheduler.state.lock().unwrap().pending_messages == 0 {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(adversary.scheduler.state.lock().unwrap().pending_bytes, 0);
}

#[test]
fn malformed_configuration_is_rejected_and_defaults_are_explicit() {
    let defaults: Config = serde_json::from_str("{}").unwrap();
    assert_eq!(defaults.mode, "none");
    assert!(defaults.bullshark_alternating);
    assert!(serde_json::from_str::<Config>(r#"{"rounds":-1}"#).is_err());
    assert!(serde_json::from_str::<Config>(r#"{"max_pending_byte":100}"#).is_err());
    let committee = committee(None);
    let name = keys(None).pop().unwrap().public().clone();
    assert!(Adversary::new(
        name.clone(),
        committee.clone(),
        Config {
            mode: "invalid".into(),
            ..Config::default()
        }
    )
    .is_err());
    assert!(Adversary::new(
        name,
        committee,
        Config {
            mode: "network".into(),
            byzantine_authorities: vec!["short-key".into()],
            ..Config::default()
        }
    )
    .is_err());
}
