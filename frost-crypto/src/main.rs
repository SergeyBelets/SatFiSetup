//! Participant-only subprocess. JSON over inherited pipes; never pass secrets in argv.
mod bitcoin;
mod smp1_psbt;
mod deposit_derivation;
mod bitcoin_input_key;
mod bitcoin_recovery;
mod withdrawal_fee;
#[cfg(test)]
mod derivation_qualification;
use frost::keys::{EvenY, Tweak};
use frost_secp256k1_tr as frost;
use hpke::{
    aead::ChaCha20Poly1305, kdf::HkdfSha256, kem::X25519HkdfSha256, Deserializable,
    Kem as KemTrait, OpModeR, OpModeS, Serializable,
};
use k256::{
    elliptic_curve::{sec1::ToEncodedPoint, PrimeField},
    ProjectivePoint, Scalar,
};
use rand::rngs::OsRng;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, IsTerminal, Read},
};
use zeroize::Zeroizing;
type E = Box<dyn std::error::Error>;
type Kem = X25519HkdfSha256;
fn bytes(v: &Value, key: &str) -> Result<Vec<u8>, E> {
    Ok(hex::decode(v[key].as_str().ok_or("missing bytes")?)?)
}
fn number(v: &Value, key: &str) -> Result<u16, E> {
    Ok(v[key].as_u64().ok_or("missing integer")?.try_into()?)
}
fn id(x: &str) -> Result<frost::Identifier, E> {
    Ok(frost::Identifier::try_from(x.parse::<u16>()?)?)
}
fn r1(v: &Value) -> Result<BTreeMap<frost::Identifier, frost::keys::dkg::round1::Package>, E> {
    v.as_object()
        .ok_or("missing round1")?
        .iter()
        .map(|(k, v)| {
            Ok((
                id(k)?,
                frost::keys::dkg::round1::Package::deserialize(&hex::decode(
                    v.as_str().ok_or("package")?,
                )?)?,
            ))
        })
        .collect()
}
fn scalar(v: &Value, key: &str) -> Result<Scalar, E> {
    let b: [u8; 32] = bytes(v, key)?.try_into().map_err(|_| "scalar length")?;
    Option::<Scalar>::from(Scalar::from_repr(b.into())).ok_or("scalar range".into())
}
fn execute(v: Value) -> Result<Value, E> {
    Ok(match v["op"].as_str().ok_or("operation")? {
        "bitcoin_build" => bitcoin::build(&v)?,
        "bitcoin_build_v1" => bitcoin::build_v1(&v)?,
        "bitcoin_derive_deposit_v1" => bitcoin_input_key::derive(&v)?,
        "bitcoin_derive_withdrawal_fee_v1" => withdrawal_fee::derive(&v)?,
        "bitcoin_funding" => bitcoin::funding(&v)?,
        "bitcoin_watch_block_v1" => bitcoin::watch_block(&v)?,
        "bitcoin_script_address_v1" => bitcoin::script_address(&v)?,
        "bitcoin_address_script_v1" => bitcoin::address_script(&v)?,
        "bitcoin_smp1_psbt_v1" => smp1_psbt::inspect(&v)?,
        "bitcoin_recovery_descriptor_v1" => bitcoin_recovery::descriptor(&v)?,
        "bitcoin_recovery_plan_v1" => bitcoin_recovery::plan(&v)?,
        "bitcoin_recovery_attach_v1" => bitcoin_recovery::attach(&v)?,
        "bitcoin_recovery_verify_v1" => bitcoin_recovery::verify(&v)?,
        "bitcoin_attach" => bitcoin::attach(&v)?,
        "bitcoin_attach_v1" => bitcoin::attach_v1(&v)?,
        "bitcoin_nonce_v1" => {
            let key = frost::keys::KeyPackage::deserialize(&bytes(&v, "key_package")?)?;
            let public = frost::keys::PublicKeyPackage::deserialize(&bytes(&v, "public_package")?)?;
            let key = bitcoin_input_key::InputKey::parse(&v["input_key"])?.key(&key, &public)?;
            let (n, c) = frost::round1::commit(key.signing_share(), &mut OsRng);
            json!({"nonces":hex::encode(n.serialize()?),"commitments":hex::encode(c.serialize()?)})
        }
        "transport_key" => {
            let (sk, pk) = Kem::gen_keypair(&mut OsRng);
            json!({"secret":hex::encode(sk.to_bytes()),"public":hex::encode(pk.to_bytes())})
        }
        "seal" => {
            let pk = <Kem as KemTrait>::PublicKey::from_bytes(&bytes(&v, "public")?)?;
            let (enc, mut ctx) = hpke::setup_sender::<ChaCha20Poly1305, HkdfSha256, Kem, _>(
                &OpModeS::Base,
                &pk,
                &bytes(&v, "info")?,
                &mut OsRng,
            )?;
            let ct = ctx.seal(&bytes(&v, "plaintext")?, &bytes(&v, "aad")?)?;
            json!({"enc":hex::encode(enc.to_bytes()),"ciphertext":hex::encode(ct)})
        }
        "open" => {
            let sk = <Kem as KemTrait>::PrivateKey::from_bytes(&bytes(&v, "secret")?)?;
            let enc = <Kem as KemTrait>::EncappedKey::from_bytes(&bytes(&v, "enc")?)?;
            let mut ctx = hpke::setup_receiver::<ChaCha20Poly1305, HkdfSha256, Kem>(
                &OpModeR::Base,
                &sk,
                &enc,
                &bytes(&v, "info")?,
            )?;
            json!({"plaintext":hex::encode(ctx.open(&bytes(&v,"ciphertext")?,&bytes(&v,"aad")?)?)})
        }
        "part1" => {
            let n = number(&v, "n")?;
            let t = number(&v, "t")?;
            if !(2..=16).contains(&n) || t < 2 || t > n {
                return Err("unsupported roster".into());
            }
            let (s, p) = frost::keys::dkg::part1(
                frost::Identifier::try_from(number(&v, "index")?)?,
                n,
                t,
                OsRng,
            )?;
            json!({"secret":hex::encode(s.serialize()?),"package":hex::encode(p.serialize()?),
                   "commitments":p.commitment().serialize()?.iter().map(hex::encode).collect::<Vec<_>>()})
        }
        "inspect_round1" => {
            let p = frost::keys::dkg::round1::Package::deserialize(&bytes(&v, "package")?)?;
            json!({"commitments":p.commitment().serialize()?.iter().map(hex::encode).collect::<Vec<_>>()})
        }
        "part2" => {
            let (s, p) = frost::keys::dkg::part2(
                frost::keys::dkg::round1::SecretPackage::deserialize(&bytes(&v, "secret")?)?,
                &r1(&v["round1"])?,
            )?;
            let mut packages = BTreeMap::new();
            for (k, p) in p {
                packages.insert(hex::encode(k.serialize()), hex::encode(p.serialize()?));
            }
            json!({"secret":hex::encode(s.serialize()?),"packages":packages})
        }
        "part3" => {
            let mut packages = BTreeMap::new();
            for (k, p) in v["round2"].as_object().ok_or("round2")? {
                packages.insert(
                    id(k)?,
                    frost::keys::dkg::round2::Package::deserialize(&hex::decode(
                        p.as_str().ok_or("package")?,
                    )?)?,
                );
            }
            let secret =
                frost::keys::dkg::round2::SecretPackage::deserialize(&bytes(&v, "secret")?)?;
            let round1 = r1(&v["round1"])?;
            let mut raw_group = ProjectivePoint::IDENTITY;
            for commitment in
                std::iter::once(secret.commitment()).chain(round1.values().map(|p| p.commitment()))
            {
                let point = k256::PublicKey::from_sec1_bytes(&commitment.serialize()?[0])?;
                raw_group += point.to_projective();
            }
            let raw_group_bytes = raw_group.to_affine().to_encoded_point(true);
            let mut internal_key = hex::encode(&raw_group_bytes.as_bytes()[1..]);
            use sha2::{Digest, Sha256};
            let tag = Sha256::digest(b"TapTweak");
            let mut tweak = hex::encode(Sha256::new()
                .chain_update(tag)
                .chain_update(tag)
                .chain_update(&raw_group_bytes.as_bytes()[1..])
                .finalize());
            let (mut key, mut public) = frost::keys::dkg::part3(&secret, &round1, &packages)?;
            let mut recovery = None;
            let mut script_tree = Value::Null;
            if let Some(policy) = v.get("recovery_policy") {
                // The library's ordinary no-tree output key is the internal
                // key of this separate descriptor. Its exact script-tree
                // tweak is computed from that key, never from raw DKG shares.
                internal_key = hex::encode(&public.verifying_key().serialize()?[1..]);
                let descriptor = bitcoin_recovery::Descriptor::new(policy, &internal_key)?;
                key = key.tweak(Some(descriptor.merkle_root));
                public = public.tweak(Some(descriptor.merkle_root));
                if public.verifying_key().serialize()?[1..] != descriptor.output_key()
                    || public.verifying_shares().get(key.identifier()) != Some(key.verifying_share())
                {
                    return Err("recovery DKG output mismatch".into());
                }
                script_tree = json!(hex::encode(descriptor.merkle_root));
                let descriptor = descriptor.public_json();
                tweak = descriptor["taproot_tweak"].as_str().ok_or("recovery tweak")?.to_string();
                recovery = Some(descriptor);
            }
            let original = public.verifying_key().serialize()?;
            let negated = !public.has_even_y();
            let key = key.into_even_y(None);
            let public = public.into_even_y(None);
            let mut shares = BTreeMap::new();
            for (i, p) in public.verifying_shares() {
                shares.insert(hex::encode(i.serialize()), hex::encode(p.serialize()?));
            }
            let mut output = json!({"key_package":hex::encode(key.serialize()?),"public_package":hex::encode(public.serialize()?),
                "signing_share":hex::encode(key.signing_share().serialize()),"public_share":hex::encode(key.verifying_share().serialize()?),
                "group":hex::encode(public.verifying_key().serialize()?),"original_group":hex::encode(original),
                "normalization_negated":negated,"public_shares":shares,"library_taproot_tweak_applied":true,"raw_dkg_group":hex::encode(raw_group_bytes.as_bytes()),
                "internal_key":internal_key,"taproot_tweak":tweak,"script_tree":script_tree});
            if let Some(descriptor) = recovery {
                output["recovery_descriptor"] = descriptor;
            }
            output
        }
        "nonce" => {
            let s = frost::keys::SigningShare::deserialize(&bytes(&v, "share")?)?;
            let (n, c) = frost::round1::commit(&s, &mut OsRng);
            json!({"nonces":hex::encode(n.serialize()?),"commitments":hex::encode(c.serialize()?)})
        }
        "inspect_bitcoin_packet" => {
            // This operation accepts public wire material only. The pinned FROST
            // version serializes two curve commitments to 71 bytes and z_i to 32.
            // A deserialization/serialization round trip also rejects alternate
            // encodings, invalid curve points, and out-of-range scalar values.
            let fields = v.as_object().ok_or("packet schema")?;
            if fields.len() != 3
                || !fields.contains_key("kind")
                || !fields.contains_key("value")
            {
                return Err("packet schema".into());
            }
            let raw = bytes(&v, "value")?;
            let canonical = match v["kind"].as_str().ok_or("packet kind")? {
                "commitment" if raw.len() == 71 => {
                    frost::round1::SigningCommitments::deserialize(&raw)?.serialize()?
                }
                "share" if raw.len() == 32 => {
                    frost::round2::SignatureShare::deserialize(&raw)?.serialize()
                }
                _ => return Err("packet kind or length".into()),
            };
            if raw != canonical {
                return Err("noncanonical packet".into());
            }
            json!({"value":hex::encode(canonical)})
        }
        "sign" | "bitcoin_sign_v1" => {
            let mut k = frost::keys::KeyPackage::deserialize(&bytes(&v, "key_package")?)?;
            if v["op"] == "bitcoin_sign_v1" {
                let public = frost::keys::PublicKeyPackage::deserialize(&bytes(&v, "public_package")?)?;
                k = bitcoin_input_key::InputKey::parse(&v["input_key"])?.key(&k, &public)?;
                if bytes(&v, "message")?.len() != 32 { return Err("Bitcoin sighash length".into()); }
            } else if v.get("input_key").is_some() { return Err("input key version required".into()); }
            let n = frost::round1::SigningNonces::deserialize(&bytes(&v, "nonces")?)?;
            let mut c = BTreeMap::new();
            for (i, p) in v["commitments"].as_object().ok_or("commitments")? {
                c.insert(
                    id(i)?,
                    frost::round1::SigningCommitments::deserialize(&hex::decode(
                        p.as_str().ok_or("commitment")?,
                    )?)?,
                );
            }
            let package = frost::SigningPackage::new(c, &bytes(&v, "message")?);
            json!({"share":hex::encode(frost::round2::sign(&package,&n,&k)?.serialize())})
        }
        "aggregate" | "bitcoin_aggregate_v1" => {
            let mut public = frost::keys::PublicKeyPackage::deserialize(&bytes(&v, "public_package")?)?;
            if v["op"] == "bitcoin_aggregate_v1" {
                public = bitcoin_input_key::InputKey::parse(&v["input_key"])?.public(&public)?;
                if bytes(&v, "message")?.len() != 32 { return Err("Bitcoin sighash length".into()); }
            } else if v.get("input_key").is_some() { return Err("input key version required".into()); }
            let mut c = BTreeMap::new();
            let mut s = BTreeMap::new();
            for (i, p) in v["commitments"].as_object().ok_or("commitments")? {
                c.insert(
                    id(i)?,
                    frost::round1::SigningCommitments::deserialize(&hex::decode(
                        p.as_str().ok_or("commitment")?,
                    )?)?,
                );
            }
            for (i, p) in v["shares"].as_object().ok_or("shares")? {
                s.insert(
                    id(i)?,
                    frost::round2::SignatureShare::deserialize(&hex::decode(
                        p.as_str().ok_or("share")?,
                    )?)?,
                );
            }
            let message = bytes(&v, "message")?;
            let package = frost::SigningPackage::new(c, &message);
            let sig = frost::aggregate(&package, &s, &public)?;
            public.verifying_key().verify(&message, &sig)?;
            json!({"signature":hex::encode(sig.serialize()?)})
        }
        "evm_share_v1" => {
            // Versioned adapter only: public transcript/rho/challenge are computed independently by participant.
            let mut nonce = scalar(&v, "hiding")? + scalar(&v, "rho")? * scalar(&v, "binding")?;
            if v["negate_nonce"] == true {
                nonce = -nonce;
            }
            let z =
                nonce + scalar(&v, "challenge")? * scalar(&v, "lagrange")? * scalar(&v, "share")?;
            json!({"share":hex::encode(z.to_bytes())})
        }
        "public_share" => {
            let s = scalar(&v, "share")?;
            if s == Scalar::ZERO {
                return Err("zero share".into());
            }
            json!({"public":hex::encode((ProjectivePoint::GENERATOR*s).to_affine().to_encoded_point(true).as_bytes())})
        }
        _ => return Err("unsupported operation".into()),
    })
}
fn main() {
    if std::env::args().nth(1).as_deref() != Some("--ipc")
        || io::stdin().is_terminal()
        || io::stdout().is_terminal()
    {
        eprintln!("LOCAL_PIPE_REQUIRED");
        std::process::exit(2);
    }
    let mut input = Zeroizing::new(String::new());
    let result = io::stdin()
        // Full witness funding can exceed the 100 kB stripped envelope. Keep
        // IPC bounded while allowing a complete consensus block-sized tx hex.
        .take(9 * 1024 * 1024)
        .read_to_string(&mut input)
        .map_err(|_| "IPC_READ_FAILED".to_string())
        .and_then(|_| serde_json::from_str(&input).map_err(|_| "INVALID_REQUEST".to_string()))
        .and_then(|v| execute(v).map_err(|_| "CRYPTO_VALIDATION_FAILED".to_string()));
    match result {
        Ok(v) => println!("{}", v),
        Err(code) => {
            eprintln!("{}", code);
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_evm_golden_vectors_and_independent_bip340() {
        let fixture: Value = serde_json::from_str(include_str!("../../tests/vectors/signing-v1.json")).unwrap();
        for vector in fixture["vectors"].as_array().unwrap() {
            for signer in vector["signers"].as_array().unwrap() {
                let mut request = signer["inputs"].clone();
                request["op"] = json!("evm_share_v1");
                request["negate_nonce"] = vector["negate_nonce"].clone();
                assert_eq!(execute(request).unwrap()["share"], signer["z"]);
            }
            let key = k256::schnorr::VerifyingKey::from_bytes(&hex::decode(vector["group"].as_str().unwrap()).unwrap()).unwrap();
            let raw = hex::decode(vector["signature"].as_str().unwrap()).unwrap();
            let signature = k256::schnorr::Signature::try_from(raw.as_slice()).unwrap();
            let message = hex::decode(vector["message"].as_str().unwrap()).unwrap();
            key.verify_raw(&message, &signature).unwrap();
        }
    }
    #[test]
    fn invalid_threshold_is_rejected() {
        assert!(execute(json!({"op":"part1","index":1,"n":3,"t":1})).is_err());
        assert!(execute(json!({"op":"part1","index":1,"n":17,"t":2})).is_err());
    }
    #[test]
    fn hpke_rejects_wrong_recipient_and_aad() {
        let key = execute(json!({"op":"transport_key"})).unwrap();
        let other = execute(json!({"op":"transport_key"})).unwrap();
        let envelope=execute(json!({"op":"seal","public":key["public"],"info":"aabb","aad":"ccdd","plaintext":"010203"})).unwrap();
        let request = json!({"op":"open","secret":key["secret"],"info":"aabb","aad":"ccdd","enc":envelope["enc"],"ciphertext":envelope["ciphertext"]});
        assert_eq!(execute(request.clone()).unwrap()["plaintext"], "010203");
        let mut wrong = request.clone();
        wrong["secret"] = other["secret"].clone();
        assert!(execute(wrong).is_err());
        let mut wrong = request;
        wrong["aad"] = json!("ccde");
        assert!(execute(wrong).is_err());
    }
    #[test]
    fn bitcoin_requires_exact_balance_and_unique_inputs() {
        let script = format!("5120{}", "11".repeat(32));
        let input =
            json!({"outpoint":format!("{}:0","22".repeat(32)),"amount":"10000","script":script});
        let package = json!({"op":"bitcoin_build","wallet_script":script,"inputs":[input],"outputs":[{"script":format!("0014{}","33".repeat(20)),"amount":"9000"}],"fee":"1000","max_fee":"2000"});
        assert!(execute(package.clone()).is_ok());
        let mut bad = package.clone();
        bad["fee"] = json!("999");
        assert!(execute(bad).is_err());
        let mut bad = package;
        bad["inputs"].as_array_mut().unwrap().push(input);
        assert!(execute(bad).is_err());
    }
    #[test]
    fn bitcoin_public_packets_are_canonical_and_never_accept_nonce_packages() {
        let nonce = execute(json!({"op":"nonce", "share":format!("{}01", "00".repeat(31))})).unwrap();
        let inspect = |kind: &str, value: &str| execute(json!({
            "op":"inspect_bitcoin_packet", "kind":kind, "value":value,
        }));
        let commitment = nonce["commitments"].as_str().unwrap();
        assert_eq!(commitment.len(), 142);
        assert_eq!(inspect("commitment", commitment).unwrap(), json!({"value":commitment}));
        let scalar = format!("{}01", "00".repeat(31));
        assert_eq!(inspect("share", &scalar).unwrap(), json!({"value":scalar}));

        for (kind, wire) in [("commitment", commitment), ("share", scalar.as_str())] {
            assert!(inspect(kind, &wire[..wire.len() - 2]).is_err());
            assert!(inspect(kind, &format!("{wire}00")).is_err());
            assert!(inspect(kind, "").is_err());
            assert!(inspect(kind, nonce["nonces"].as_str().unwrap()).is_err());
            assert!(execute(json!({"op":"inspect_bitcoin_packet", "kind":kind, "value":wire,
                                  "signing_share":scalar})).is_err());
        }
        assert!(inspect("share", &"ff".repeat(32)).is_err());
        // An ordinary encoding of the group order is not a reduced scalar.
        assert!(inspect("share", "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141").is_err());
        assert!(inspect("commitment", &"00".repeat(71)).is_err());
        // Preserve the version/ciphersuite prefix while corrupting a point.
        let invalid_point = format!("{}{}{}", &commitment[..10], "00".repeat(33), &commitment[76..]);
        assert!(inspect("commitment", &invalid_point).is_err());
        assert!(inspect("nonces", commitment).is_err());
    }
}
