//! Synthetic qualification tests for the shared derivation implementation.
use crate::deposit_derivation::*;
use bitcoin::{
    hashes::Hash,
    secp256k1::{Parity, Secp256k1, XOnlyPublicKey},
    taproot::TapTweakHash,
};
use frost_secp256k1_tr::{
    self as frost,
    keys::{EvenY, Tweak},
};

#[cfg(test)]
mod tests {
    use super::*;
    use ::bitcoin::secp256k1::{constants::CURVE_ORDER, schnorr::Signature, Message, PublicKey};
    use rand::{rngs::StdRng, SeedableRng};
    use serde_json::{json, Value};
    use std::collections::{BTreeMap, BTreeSet};

    fn intent() -> DepositIntent {
        let mut chain_id = [0; 32];
        chain_id[30..].copy_from_slice(&31337u16.to_be_bytes());
        DepositIntent {
            protocol_version: 1,
            derivation_version: 1,
            bitcoin_genesis: [1; 32],
            evm_chain_id: chain_id,
            deposit_registry: [2; 20],
            pool: [3; 20],
            mp_token: [4; 20],
            custody_graph_id: [5; 32],
            dkg_registry: [6; 20],
            wallet_id: [7; 32],
            wallet_result: [8; 32],
            key_epoch: 9,
            requester: [10; 20],
            recipient: [11; 20],
            request_nonce: [12; 32],
            service_policy_hash: [13; 32],
        }
    }

    fn baseline(
        seed: u8,
    ) -> (
        BTreeMap<frost::Identifier, frost::keys::KeyPackage>,
        frost::keys::PublicKeyPackage,
        Vec<u8>,
    ) {
        let mut rng = StdRng::from_seed([seed; 32]);
        let mut secrets = BTreeMap::new();
        let mut round1 = BTreeMap::new();
        for index in 1u16..=3 {
            let id = frost::Identifier::try_from(index).unwrap();
            let (secret, package) = frost::keys::dkg::part1(id, 3, 2, &mut rng).unwrap();
            secrets.insert(id, secret);
            round1.insert(id, package);
        }
        let raw_points: Vec<_> = round1
            .values()
            .map(|p| PublicKey::from_slice(&p.commitment().serialize().unwrap()[0]).unwrap())
            .collect();
        let raw_group = PublicKey::combine_keys(&raw_points.iter().collect::<Vec<_>>()).unwrap();
        let mut second = BTreeMap::new();
        for (id, secret) in secrets {
            let peers = round1
                .iter()
                .filter(|(other, _)| **other != id)
                .map(|(id, p)| (*id, p.clone()))
                .collect();
            second.insert(id, frost::keys::dkg::part2(secret, &peers).unwrap());
        }
        let mut keys = BTreeMap::new();
        let mut public = None;
        for (id, (secret, _)) in &second {
            let peers = round1
                .iter()
                .filter(|(other, _)| **other != *id)
                .map(|(id, p)| (*id, p.clone()))
                .collect();
            let incoming = second
                .iter()
                .filter(|(other, _)| **other != *id)
                .map(|(sender, (_, packages))| (*sender, packages[id].clone()))
                .collect();
            // Exactly the same existing part3/post_dkg then EvenY sequence.
            let (key, group) = frost::keys::dkg::part3(secret, &peers, &incoming).unwrap();
            let group = group.into_even_y(None);
            if let Some(previous) = &public {
                assert_eq!(previous, &group);
            }
            public = Some(group);
            keys.insert(*id, key.into_even_y(None));
        }
        (keys, public.unwrap(), raw_group.serialize().to_vec())
    }

    fn public_vector() -> Value {
        let (_, public, raw) = baseline(42);
        let intent = intent();
        let (d, derived) = derive_public(&public, &intent).unwrap();
        json!({"profile":PROFILE,"test_keys_only":true,"rng":"rand0.8.6 StdRng seed[42;32]",
            "dkg":"3 participants, threshold2; library part3/post_dkg then EvenY",
            "intent_bytes":hex::encode(intent.canonical_bytes().unwrap()),"intent_id":hex::encode(d.intent_id),
            "raw_dkg_group":hex::encode(raw),"base_group":hex::encode(d.base_group),
            "leaf_version":"c0","leaf_script":hex::encode(d.leaf_script),"merkle_root":hex::encode(d.merkle_root),
            "tweak":hex::encode(d.tweak),"output_key":hex::encode(d.output_key),
            "output_parity_odd":d.output_parity_odd,"derived_group_even_y":hex::encode(derived.verifying_key().serialize().unwrap()),
            "script_pubkey":hex::encode(d.script_pubkey),"binding_commitment":hex::encode(d.binding_commitment)})
    }

    #[test]
    #[ignore = "public fixture generator, not a qualification test"]
    fn emit_public_vector() {
        println!("DERIVATION_PUBLIC_VECTOR={}", public_vector());
    }

    #[test]
    fn frozen_public_vector_matches() {
        let frozen: Value = serde_json::from_str(include_str!(
            "../../rust/zkspv/derivation-qualification/public-vector.json"
        ))
        .unwrap();
        assert_eq!(public_vector(), frozen);
    }

    #[test]
    fn baseline_is_no_tree_tweaked_once_and_existing_keys_remain_unchanged() {
        let (keys, public, raw) = baseline(42);
        let (internal, _) = PublicKey::from_slice(&raw).unwrap().x_only_public_key();
        let no_tree = TapTweakHash::from_key_and_tweak(internal, None).to_byte_array();
        let (expected, _) = checked_public_tweak(internal, no_tree).unwrap();
        let group = public.verifying_key().serialize().unwrap();
        assert_eq!(&group[1..], &expected.serialize());
        assert_eq!(group[0], 2);
        let saved_public = public.serialize().unwrap();
        for key in keys.values() {
            let saved = key.serialize().unwrap();
            let (_, child) = derive_key(key, &public, &intent()).unwrap();
            assert_eq!(key.serialize().unwrap(), saved);
            assert_ne!(child.verifying_key(), key.verifying_key());
            assert_eq!(child.min_signers(), key.min_signers());
        }
        assert_eq!(public.serialize().unwrap(), saved_public);
    }

    #[test]
    fn full_intent_context_and_single_fixed_unspendable_leaf_are_bound() {
        let (_, public, _) = baseline(42);
        let original = intent();
        let (descriptor, _) = derive_public(&public, &original).unwrap();
        assert_eq!(&descriptor.leaf_script[..2], &[0x6a, 0x20]);
        assert_eq!(&descriptor.leaf_script[2..], &descriptor.intent_id);
        assert_eq!(descriptor.leaf_script.len(), 34);
        assert_eq!(&descriptor.script_pubkey[..2], &[0x51, 0x20]);
        assert_eq!(descriptor.script_pubkey.len(), 34);
        let mut variants = Vec::new();
        macro_rules! change {
            ($field:ident) => {{
                let mut v = original.clone();
                v.$field[0] ^= 0x80;
                variants.push(v);
            }};
        }
        change!(bitcoin_genesis);
        change!(evm_chain_id);
        change!(deposit_registry);
        change!(pool);
        change!(mp_token);
        change!(custody_graph_id);
        change!(dkg_registry);
        change!(wallet_id);
        change!(wallet_result);
        change!(requester);
        change!(recipient);
        change!(request_nonce);
        change!(service_policy_hash);
        let mut epoch = original.clone();
        epoch.key_epoch += 1;
        variants.push(epoch);
        for variant in variants {
            let (changed, _) = derive_public(&public, &variant).unwrap();
            assert_ne!(changed.intent_id, descriptor.intent_id);
            assert_ne!(changed.output_key, descriptor.output_key);
            assert_ne!(changed.binding_commitment, descriptor.binding_commitment);
        }
        let mut bad = original.clone();
        bad.recipient = [0; 20];
        assert!(derive_public(&public, &bad).is_err());
        bad = original.clone();
        bad.request_nonce = [0; 32];
        assert!(derive_public(&public, &bad).is_err());
        bad = original.clone();
        bad.protocol_version = 2;
        assert!(derive_public(&public, &bad).is_err());
        bad = original;
        bad.derivation_version = 2;
        assert!(derive_public(&public, &bad).is_err());
    }

    #[test]
    fn invalid_scalar_and_infinity_fail_but_zero_is_separate_alias_policy() {
        let generator = PublicKey::from_slice(
            &hex::decode("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
                .unwrap(),
        )
        .unwrap()
        .x_only_public_key()
        .0;
        assert!(checked_public_tweak(generator, CURVE_ORDER).is_err());
        assert!(checked_public_tweak(generator, [0xff; 32]).is_err());
        let mut minus_one = CURVE_ORDER;
        minus_one[31] -= 1;
        assert!(checked_public_tweak(generator, minus_one).is_err()); // G + (n-1)G is infinity.
        let (same, parity) = checked_public_tweak(generator, [0; 32]).unwrap();
        assert_eq!(same, generator);
        assert_eq!(parity, Parity::Even);
        assert!(reject_base_alias(generator, same).is_err());
        let mut minus_two = CURVE_ORDER;
        minus_two[31] -= 2;
        let (opposite, parity) = checked_public_tweak(generator, minus_two).unwrap();
        assert_eq!(opposite, generator); // -(G) also has the same x-only key.
        assert_eq!(parity, Parity::Odd);
        assert!(reject_base_alias(generator, opposite).is_err());
        assert!(XOnlyPublicKey::from_slice(&[0xff; 32]).is_err());
    }

    #[test]
    fn wrong_key_package_and_non_normalized_baseline_are_rejected() {
        let (keys, public, _) = baseline(42);
        let (_, foreign, _) = baseline(43);
        assert!(derive_key(keys.values().next().unwrap(), &foreign, &intent()).is_err());
        let first = &keys[&frost::Identifier::try_from(1u16).unwrap()];
        let second = &keys[&frost::Identifier::try_from(2u16).unwrap()];
        let mut missing_shares = public.verifying_shares().clone();
        missing_shares.remove(first.identifier());
        let missing = frost::keys::PublicKeyPackage::new(missing_shares, *public.verifying_key());
        assert!(derive_key(first, &missing, &intent()).is_err());
        let mut wrong_shares = public.verifying_shares().clone();
        wrong_shares.insert(*first.identifier(), *second.verifying_share());
        let wrong = frost::keys::PublicKeyPackage::new(wrong_shares, *public.verifying_key());
        assert!(derive_key(first, &wrong, &intent()).is_err());
        let wrong_secret = frost::keys::KeyPackage::new(
            *first.identifier(),
            *second.signing_share(),
            *first.verifying_share(),
            *first.verifying_key(),
            *first.min_signers(),
        );
        assert!(derive_key(&wrong_secret, &public, &intent()).is_err());
        let mut found_odd = false;
        for nonce in 1..=32 {
            let mut context = intent();
            context.request_nonce[0] = nonce;
            let (descriptor, _) = derive_public(&public, &context).unwrap();
            if descriptor.output_parity_odd {
                let unnormalized = public.clone().tweak(Some(&descriptor.merkle_root));
                assert!(derive_public(&unnormalized, &context).is_err());
                found_odd = true;
                break;
            }
        }
        assert!(found_odd);
    }

    #[test]
    fn derived_threshold_signatures_verify_per_key_and_wrong_context_fails() {
        let (keys, public, _) = baseline(42);
        let mut rng = StdRng::from_seed([99; 32]);
        let mut commitments_seen = BTreeSet::new();
        let mut parity_seen = BTreeSet::new();
        let mut signer_sets_seen = BTreeSet::new();
        for index in 1u8..=8 {
            let mut context = intent();
            context.request_nonce[0] = index;
            let (descriptor, derived_public) = derive_public(&public, &context).unwrap();
            parity_seen.insert(descriptor.output_parity_odd);
            let derived_keys: BTreeMap<_, _> = keys
                .iter()
                .map(|(id, key)| (*id, derive_key(key, &public, &context).unwrap().1))
                .collect();
            let message = [index; 32];
            let mut commitments = BTreeMap::new();
            let mut nonces = BTreeMap::new();
            let excluded = usize::from(index - 1) % 3;
            for (_, (id, key)) in derived_keys
                .iter()
                .enumerate()
                .filter(|(i, _)| *i != excluded)
            {
                let (nonce, commitment) = frost::round1::commit(key.signing_share(), &mut rng);
                assert!(commitments_seen.insert(commitment.serialize().unwrap()));
                nonces.insert(*id, nonce);
                commitments.insert(*id, commitment);
            }
            signer_sets_seen.insert(commitments.keys().copied().collect::<Vec<_>>());
            let package = frost::SigningPackage::new(commitments, &message);
            let shares: BTreeMap<_, _> = nonces
                .iter()
                .map(|(id, nonce)| {
                    (
                        *id,
                        frost::round2::sign(&package, nonce, &derived_keys[id]).unwrap(),
                    )
                })
                .collect();
            let signature = frost::aggregate(&package, &shares, &derived_public).unwrap();
            derived_public
                .verifying_key()
                .verify(&message, &signature)
                .unwrap();
            assert!(public.verifying_key().verify(&message, &signature).is_err());
            let mut wrong = context.clone();
            wrong.recipient[0] ^= 1;
            let (_, wrong_public) = derive_public(&public, &wrong).unwrap();
            assert!(wrong_public
                .verifying_key()
                .verify(&message, &signature)
                .is_err());
            Secp256k1::verification_only()
                .verify_schnorr(
                    &Signature::from_slice(&signature.serialize().unwrap()).unwrap(),
                    &Message::from_digest_slice(&message).unwrap(),
                    &XOnlyPublicKey::from_slice(&descriptor.output_key).unwrap(),
                )
                .unwrap();
        }
        assert_eq!(parity_seen.len(), 2);
        assert_eq!(signer_sets_seen.len(), 3);
    }

    #[test]
    fn versioned_ipc_mixed_inputs_sign_real_bitcoin_sighashes() {
        use crate::execute;
        let (keys, public, _) = baseline(42);
        let base_group = hex::encode(public.verifying_key().serialize().unwrap());
        let base_script = format!("5120{}", &base_group[2..]);
        let mut descriptors = vec![
            json!({"version":1,"mode":"BASE","base_group":base_group,"wallet_context":hex::encode(intent().wallet_context())}),
        ];
        let mut scripts = vec![base_script.clone()];
        let mut parities = BTreeSet::new();
        for index in 1..=32 {
            let mut context = intent();
            context.request_nonce[0] = index;
            let d = execute(
                json!({"op":"bitcoin_derive_deposit_v1","base_group":base_group,
                "intent":hex::encode(context.canonical_bytes().unwrap())}),
            )
            .unwrap();
            if parities.insert(d["output_parity_odd"].as_bool().unwrap()) {
                assert_eq!(d["ready"], false);
                scripts.push(d["script_pubkey"].as_str().unwrap().to_owned());
                descriptors.push(d["input_key"].clone());
            }
            if parities.len() == 2 {
                break;
            }
        }
        assert_eq!(descriptors.len(), 3); // BASE and DERIVED of both parities.
        let inputs: Vec<_> = scripts.iter().enumerate().map(|(i, script)| json!({
            "outpoint":format!("{}:{}", "11".repeat(32), i), "amount":"10000", "script":script,
        })).collect();
        let plan = json!({"op":"bitcoin_build_v1","input_key_version":1,"input_keys":descriptors,
            "inputs":inputs,"outputs":[{"amount":"29000","script":base_script}],
            "wallet_script":base_script,"fee":"1000","max_fee":"2000"});
        let built = execute(plan.clone()).unwrap();
        let saved_public = public.serialize().unwrap();
        let mut signatures = vec![];
        let mut commitments_seen = BTreeSet::new();
        for i in 0..3 {
            let mut nonces = BTreeMap::new();
            let mut commitments = serde_json::Map::new();
            let mut shares = serde_json::Map::new();
            for index in 1u16..=2 {
                let id = frost::Identifier::try_from(index).unwrap();
                let saved_key = keys[&id].serialize().unwrap();
                let n = execute(json!({"op":"bitcoin_nonce_v1","input_key":descriptors[i],
                    "key_package":hex::encode(&saved_key),"public_package":hex::encode(&saved_public)})).unwrap();
                assert!(commitments_seen.insert(n["commitments"].as_str().unwrap().to_owned()));
                commitments.insert(index.to_string(), n["commitments"].clone());
                nonces.insert(index, n["nonces"].clone());
                assert_eq!(keys[&id].serialize().unwrap(), saved_key);
            }
            for index in 1u16..=2 {
                let id = frost::Identifier::try_from(index).unwrap();
                let share = execute(json!({"op":"bitcoin_sign_v1","input_key":descriptors[i],
                    "key_package":hex::encode(keys[&id].serialize().unwrap()),
                    "public_package":hex::encode(&saved_public),"nonces":nonces[&index],
                    "commitments":commitments,"message":built["sighashes"][i]}))
                .unwrap();
                shares.insert(index.to_string(), share["share"].clone());
            }
            let request = json!({"op":"bitcoin_aggregate_v1","input_key":descriptors[i],
                "public_package":hex::encode(&saved_public),"commitments":commitments,
                "shares":shares,"message":built["sighashes"][i]});
            let signature = execute(request.clone()).unwrap();
            signatures.push(signature["signature"].clone());
            let mut wrong = request;
            wrong["input_key"] = descriptors[(i + 1) % 3].clone();
            assert!(execute(wrong).is_err());
        }
        assert_eq!(public.serialize().unwrap(), saved_public);
        let attach = json!({"op":"bitcoin_attach_v1","package":plan,
            "unsigned":built["unsigned"],"signatures":signatures});
        // attach independently verifies every actual BIP341 sighash/signature
        // with libsecp256k1 against that input's exact script, not a common key.
        let signed = execute(attach.clone()).unwrap();
        assert_eq!(signed["txid"], built["txid"]);
        assert_ne!(signed["wtxid"], signed["txid"]);
        let mut wrong = attach;
        wrong["signatures"].as_array_mut().unwrap().swap(0, 1);
        assert!(execute(wrong).is_err());
        let mut wrong = plan.clone();
        wrong["inputs"][1]["script"] = json!(base_script);
        assert!(execute(wrong).is_err());
        let mut wrong = plan.clone();
        wrong["op"] = json!("bitcoin_build");
        assert!(execute(wrong).is_err());
        let mut wrong = plan.clone();
        wrong["input_keys"].as_array_mut().unwrap().swap(0, 1);
        assert!(execute(wrong).is_err());
        // The same curve point alone cannot authorize a different wallet epoch.
        for field in ["epoch", "wallet_result", "registry"] {
            let mut context = intent();
            match field {
                "epoch" => context.key_epoch += 1,
                "wallet_result" => context.wallet_result[0] ^= 1,
                _ => context.deposit_registry[0] ^= 1,
            }
            let changed = execute(
                json!({"op":"bitcoin_derive_deposit_v1","base_group":base_group,
                "intent":hex::encode(context.canonical_bytes().unwrap())}),
            )
            .unwrap();
            let mut wrong = plan.clone();
            wrong["input_keys"][1] = changed["input_key"].clone();
            wrong["inputs"][1]["script"] = changed["script_pubkey"].clone();
            assert!(execute(wrong).is_err());
        }
        let mut wrong = plan;
        wrong["input_keys"][1]["binding_commitment"] = json!("00".repeat(32));
        assert!(execute(wrong).is_err());
    }

    #[test]
    fn versioned_ipc_rejects_ambiguous_descriptors_and_intent_encodings() {
        use crate::{bitcoin_input_key::InputKey, execute};
        let (_, public, _) = baseline(42);
        let group = hex::encode(public.verifying_key().serialize().unwrap());
        let canonical = intent().canonical_bytes().unwrap();
        assert_eq!(
            DepositIntent::from_canonical(&canonical)
                .unwrap()
                .canonical_bytes()
                .unwrap(),
            canonical
        );
        for size in [0, 26, 381, 383] {
            assert!(DepositIntent::from_canonical(&vec![1; size]).is_err());
        }
        for offset in [0, 26, 28] {
            let mut bad = canonical.clone();
            bad[offset] ^= 1;
            assert!(DepositIntent::from_canonical(&bad).is_err());
        }
        let mut bad = canonical.clone();
        bad[298..318].fill(0);
        assert!(DepositIntent::from_canonical(&bad).is_err());
        let base = json!({"version":1,"mode":"BASE","base_group":group,"wallet_context":hex::encode(intent().wallet_context())});
        assert!(InputKey::parse(&base).is_ok());
        for extra in ["intent", "tweak", "output_key", "binding_commitment"] {
            let mut bad = base.clone();
            bad[extra] = json!("00");
            assert!(InputKey::parse(&bad).is_err());
        }
        let mut bad = base;
        bad["version"] = json!("1");
        assert!(InputKey::parse(&bad).is_err());
        let mut request = json!({"op":"bitcoin_derive_deposit_v1","base_group":group,
            "intent":hex::encode(canonical)});
        request["tweak"] = json!("00".repeat(32));
        assert!(execute(request).is_err());
    }

    #[test]
    fn versioned_nonce_ipc_rejects_wrong_base_packages_and_uncanonical_modes() {
        use crate::execute;
        let (keys, public, _) = baseline(42);
        let (_, foreign, _) = baseline(43);
        let group = hex::encode(public.verifying_key().serialize().unwrap());
        let key = keys.values().next().unwrap();
        let input_key = json!({"version":1,"mode":"BASE","base_group":group,
            "wallet_context":hex::encode(intent().wallet_context())});
        let request = json!({"op":"bitcoin_nonce_v1","input_key":input_key,
            "key_package":hex::encode(key.serialize().unwrap()),
            "public_package":hex::encode(foreign.serialize().unwrap())});
        assert!(execute(request.clone()).is_err());
        let mut request = request;
        request["public_package"] = json!(hex::encode(public.serialize().unwrap()));
        request["input_key"]["mode"] = json!("DERIVED"); // No implicit fallback to BASE.
        assert!(execute(request).is_err());
    }
}
