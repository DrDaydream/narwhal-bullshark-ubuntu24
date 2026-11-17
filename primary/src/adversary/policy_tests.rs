use super::*;

fn policy(mode: Mode) -> Policy<u8> {
    Policy::new(
        Settings {
            mode,
            ..Settings::default()
        },
        vec![70, 20, 50, 10, 60, 40, 30],
        vec![10],
    )
    .unwrap()
}

#[test]
fn alternating_uses_actual_global_leader_slots_and_sorted_keys() {
    let policy = policy(Mode::Targeted);
    // Sorted leader indices round % 7: r2 -> 30, r4 -> 50, r6 -> 70, r8 -> 20.
    assert_eq!(policy.leader(2), Some(&30));
    assert_eq!(policy.leader(4), Some(&50));
    assert_eq!(policy.leader(6), Some(&70));
    assert_eq!(policy.leader(8), Some(&20));
    assert_eq!(policy.suppression(&30, 2), None);
    assert_eq!(
        policy.suppression(&50, 4),
        Some(Suppression::AlternatingLeader)
    );
    assert_eq!(policy.suppression(&70, 6), None);
    assert_eq!(
        policy.suppression(&20, 8),
        Some(Suppression::AlternatingLeader)
    );
    assert_eq!(policy.suppression(&10, 4), None);
    assert_eq!(policy.suppression(&50, 5), None);
}

#[test]
fn window_filters_global_slots_without_restarting_parity() {
    let mut policy = policy(Mode::Network);
    policy.settings.start_round = 5;
    policy.settings.rounds = 8;
    assert_eq!(policy.suppression(&50, 4), None);
    assert_eq!(policy.suppression(&70, 6), None);
    assert_eq!(
        policy.suppression(&20, 8),
        Some(Suppression::AlternatingLeader)
    );
    assert_eq!(
        policy.suppression(&60, 12),
        Some(Suppression::AlternatingLeader)
    );
    assert_eq!(policy.suppression(&30, 16), None);
    assert!(!policy.active(4));
    assert!(policy.active(5));
    assert!(policy.active(12));
    assert!(!policy.active(13));
}

#[test]
fn none_and_opted_out_alternation_do_not_suppress_leaders() {
    let baseline = policy(Mode::None);
    assert_eq!(baseline.suppression(&50, 4), None);
    assert_eq!(
        baseline.action(&10, &70, 4, Some((4, &50))),
        Action::Deliver
    );
    let mut fixed = policy(Mode::Targeted);
    fixed.settings.bullshark_alternating = false;
    assert_eq!(fixed.suppression(&50, 4), None);
}

#[test]
fn silent_fixed_leader_remains_available_in_normal_alternating_slot() {
    let policy = policy(Mode::Silent);
    // In a seven-authority committee, fixed member 10 leads r14: global slot 7.
    assert_eq!(policy.suppression(&10, 14), None);
    assert_eq!(
        policy.action(&10, &30, 14, Some((14, &10))),
        Action::Deliver
    );
    assert_eq!(
        policy.action(&10, &30, 15, Some((14, &10))),
        Action::Deliver
    );
    assert_eq!(policy.action(&10, &30, 14, Some((14, &30))), Action::Drop);
    assert_eq!(
        policy.suppression(&10, 28),
        Some(Suppression::AlternatingLeader)
    );
    let mut fixed = policy;
    fixed.settings.bullshark_alternating = false;
    assert_eq!(fixed.suppression(&10, 14), Some(Suppression::Silent));
}

#[test]
fn silent_withholds_all_fixed_sender_traffic_only_inside_window() {
    let mut policy = policy(Mode::Silent);
    policy.settings.start_round = 3;
    policy.settings.rounds = 2;
    assert_eq!(policy.action(&10, &20, 2, None), Action::Deliver);
    assert_eq!(policy.action(&10, &20, 3, None), Action::Drop);
    assert_eq!(policy.action(&10, &20, 4, Some((1, &20))), Action::Drop);
    assert_eq!(policy.action(&10, &20, 5, None), Action::Deliver);
    assert_eq!(policy.action(&20, &10, 3, None), Action::Deliver);
    assert_eq!(policy.suppression(&10, 3), Some(Suppression::Silent));
}

#[test]
fn targeted_preserves_honest_relays_but_schedules_fixed_sender_evidence() {
    let policy = policy(Mode::Targeted);
    // Leader 30's early recipient group at r2 is 30,40,50,60; 70 is delayed.
    assert_eq!(policy.action(&10, &70, 2, Some((2, &30))), Action::Delay);
    assert_eq!(policy.action(&10, &40, 2, Some((2, &30))), Action::Deliver);
    assert_eq!(policy.action(&20, &70, 2, Some((2, &30))), Action::Deliver);
    assert_eq!(policy.action(&20, &70, 2, Some((2, &10))), Action::Deliver);
    assert_eq!(policy.action(&10, &70, 2, None), Action::Deliver);
}

#[test]
fn network_schedules_honest_evidence_and_releases_after_window() {
    let mut policy = policy(Mode::Network);
    policy.settings.rounds = 4;
    assert_eq!(policy.action(&20, &70, 2, Some((2, &30))), Action::Delay);
    assert_eq!(policy.action(&20, &70, 5, Some((5, &30))), Action::Deliver);
    policy.settings.delay_ms = 0;
    assert_eq!(policy.action(&20, &70, 2, Some((2, &30))), Action::Deliver);
}

#[test]
fn invalid_members_and_overflowing_window_are_rejected() {
    assert!(Policy::new(Settings::default(), vec![1, 2], vec![3]).is_err());
    assert!(Policy::<u8>::new(Settings::default(), vec![], vec![]).is_err());
    assert!(Policy::new(Settings::default(), vec![1, 2, 3, 4], vec![1, 2]).is_err());
    assert!(Policy::new(Settings::default(), vec![1, 2, 3, 4], vec![1, 1]).is_err());
    assert!(Policy::new(
        Settings {
            start_round: u64::MAX,
            rounds: 2,
            ..Settings::default()
        },
        vec![1],
        vec![]
    )
    .is_err());
    assert!(Policy::new(
        Settings {
            max_pending_messages: 0,
            ..Settings::default()
        },
        vec![1],
        vec![]
    )
    .is_err());
    assert!(Policy::new(
        Settings {
            max_pending_bytes: 0,
            ..Settings::default()
        },
        vec![1],
        vec![]
    )
    .is_err());
}

#[test]
fn seed_reproducibly_rotates_recipient_groups() {
    let mut policy = policy(Mode::Network);
    assert_eq!(policy.action(&20, &30, 2, Some((2, &30))), Action::Deliver);
    policy.settings.seed = 2;
    assert_eq!(policy.action(&20, &30, 2, Some((2, &30))), Action::Delay);
    assert_eq!(policy.action(&20, &30, 3, Some((3, &30))), Action::Delay);
    policy.settings.seed = 1;
    assert_eq!(policy.action(&20, &30, 2, Some((2, &30))), Action::Deliver);
}

#[test]
fn delay_queue_bounds_count_and_serialized_bytes_and_recovers_capacity() {
    let now = Instant::now();
    let mut queue = DelayQueue::new(2, 10);
    assert!(queue
        .push(now + Duration::from_millis(20), 6, "first")
        .is_ok());
    assert_eq!(queue.push(now, 5, "too many bytes"), Err("too many bytes"));
    assert!(queue
        .push(now + Duration::from_millis(10), 4, "second")
        .is_ok());
    assert_eq!(
        queue.push(now, 0, "too many messages"),
        Err("too many messages")
    );
    assert_eq!(queue.pop_due(now), None);
    assert_eq!(
        queue.pop_due(now + Duration::from_millis(10)),
        Some("second")
    );
    assert!(queue.push(now, 4, "replacement").is_ok());
    assert_eq!(queue.pop_due(now), Some("replacement"));
    assert_eq!(
        queue.pop_due(now + Duration::from_millis(20)),
        Some("first")
    );
    assert_eq!(queue.next_deadline(), None);
    assert_eq!(queue.pending_bytes, 0);
}

#[test]
fn delay_queue_preserves_fifo_for_equal_deadlines() {
    let now = Instant::now();
    let mut queue = DelayQueue::new(3, 10);
    queue.push(now, 1, 1).unwrap();
    queue.push(now, 1, 2).unwrap();
    assert_eq!(queue.pop_due(now), Some(1));
    assert_eq!(queue.pop_due(now), Some(2));
}
