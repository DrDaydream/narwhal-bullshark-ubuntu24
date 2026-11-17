// Copyright (c) 2021, Facebook, Inc. and its affiliates
// Copyright (c) 2022, Mysten Labs, Inc.
// SPDX-License-Identifier: Apache-2.0
use super::*;
use crypto::traits::KeyPair;
use test_utils::{committee, keys};
use tokio::sync::mpsc::channel;

#[tokio::test]
async fn propose_empty() {
    let kp = keys(None).pop().unwrap();
    let name = kp.public().clone();
    let signature_service = SignatureService::new(kp);

    let (_tx_parents, rx_parents) = channel(1);
    let (_tx_our_digests, rx_our_digests) = channel(1);
    let (tx_headers, mut rx_headers) = channel(1);

    // Spawn the proposer.
    Proposer::spawn(
        name,
        committee(None),
        signature_service,
        /* header_size */ 1_000,
        /* max_header_delay */ Duration::from_millis(20),
        /* rx_core */ rx_parents,
        /* rx_workers */ rx_our_digests,
        /* tx_core */ tx_headers,
        None,
    );

    // Ensure the proposer makes a correct empty header.
    let header = rx_headers.recv().await.unwrap();
    assert_eq!(header.round, 1);
    assert!(header.payload.is_empty());
    assert!(header.verify(&committee(None)).is_ok());
}

#[tokio::test]
async fn propose_payload() {
    let kp = keys(None).pop().unwrap();
    let name = kp.public().clone();
    let signature_service = SignatureService::new(kp);

    let (_tx_parents, rx_parents) = channel(1);
    let (tx_our_digests, rx_our_digests) = channel(1);
    let (tx_headers, mut rx_headers) = channel(1);

    // Spawn the proposer.
    Proposer::spawn(
        name.clone(),
        committee(None),
        signature_service,
        /* header_size */ 32,
        /* max_header_delay */
        Duration::from_millis(1_000_000), // Ensure it is not triggered.
        /* rx_core */ rx_parents,
        /* rx_workers */ rx_our_digests,
        /* tx_core */ tx_headers,
        None,
    );

    // Send enough digests for the header payload.
    let name_bytes: [u8; 32] = *name.0.as_bytes();
    let digest = BatchDigest(name_bytes);
    let worker_id = 0;
    tx_our_digests.send((digest, worker_id)).await.unwrap();

    // Ensure the proposer makes a correct header from the provided payload.
    let header = rx_headers.recv().await.unwrap();
    assert_eq!(header.round, 1);
    assert_eq!(header.payload.get(&digest), Some(&worker_id));
    assert!(header.verify(&committee(None)).is_ok());
}

#[tokio::test]
async fn alternating_slots_never_create_missing_leader_headers_and_resume_after_window() {
    use crate::adversary::{Adversary, Config};
    use tokio::time::timeout;

    let committee = committee(None);
    let key = keys(None)
        .into_iter()
        .min_by_key(|key| key.public().clone())
        .unwrap();
    let name = key.public().clone(); // r4 and r8 leader in a four-authority committee.
    let config = Config {
        mode: "network".into(),
        start_round: 1,
        rounds: 8,
        ..Config::default()
    };
    let adversary = Adversary::new(name.clone(), committee.clone(), config).unwrap();
    let (tx_parents, rx_parents) = channel(10);
    let (_tx_digests, rx_digests) = channel(10);
    let (tx_headers, mut rx_headers) = channel(10);
    Proposer::spawn(
        name,
        committee,
        SignatureService::new(key),
        1_000,
        Duration::from_millis(5),
        rx_parents,
        rx_digests,
        tx_headers,
        Some(adversary),
    );

    assert_eq!(
        timeout(Duration::from_secs(1), rx_headers.recv())
            .await
            .unwrap()
            .unwrap()
            .round,
        1
    );
    // Parent quorum delivery is the proposer input. No proposal for r4/r8 must
    // ever reach Core, where it could otherwise be stored and repaired later.
    for round in 2..=12 {
        tx_parents
            .send((vec![CertificateDigest::default()], round - 1))
            .await
            .unwrap();
        if round == 4 || round == 8 {
            assert!(timeout(Duration::from_millis(25), rx_headers.recv())
                .await
                .is_err());
        } else {
            let header = timeout(Duration::from_secs(1), rx_headers.recv())
                .await
                .unwrap()
                .unwrap();
            assert_eq!(header.round, round);
        }
    }
    // r12 is another even leader slot, but outside [1,9), so it was emitted.
}

#[tokio::test]
async fn silent_fixed_leader_proposes_normal_slots_two_and_six() {
    use crate::adversary::{Adversary, Config};
    use crypto::traits::EncodeDecodeBase64;
    use tokio::time::timeout;

    let committee = committee(None);
    let leader = committee.authorities.load().keys().nth(2).unwrap().clone();
    let key = keys(None)
        .into_iter()
        .find(|key| key.public() == &leader)
        .unwrap();
    let config = Config {
        mode: "silent".into(),
        byzantine_authorities: vec![leader.encode_base64()],
        ..Config::default()
    };
    let adversary = Adversary::new(leader.clone(), committee.clone(), config).unwrap();
    let (tx_parents, rx_parents) = channel(10);
    let (_tx_digests, rx_digests) = channel(10);
    let (tx_headers, mut rx_headers) = channel(10);
    Proposer::spawn(
        leader,
        committee,
        SignatureService::new(key),
        1_000,
        Duration::from_millis(5),
        rx_parents,
        rx_digests,
        tx_headers,
        Some(adversary),
    );
    assert!(timeout(Duration::from_millis(25), rx_headers.recv())
        .await
        .is_err());
    for round in [2, 6] {
        tx_parents
            .send((vec![CertificateDigest::default()], round - 1))
            .await
            .unwrap();
        assert_eq!(
            timeout(Duration::from_secs(1), rx_headers.recv())
                .await
                .unwrap()
                .unwrap()
                .round,
            round
        );
    }
}
