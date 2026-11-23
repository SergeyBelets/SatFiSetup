//! Deterministic, opt-in CLTV recovery for a single custody wallet epoch.
use crate::{bytes, E};
use bitcoin::{
    absolute,
    consensus::encode::{deserialize, serialize, serialize_hex},
    hashes::Hash,
    key::TweakedPublicKey,
    opcodes::all::{OP_CHECKSIG, OP_CHECKSIGADD, OP_CLTV, OP_NUMEQUAL, OP_VERIFY},
    psbt::Psbt,
    script::Builder,
    secp256k1::{schnorr::Signature, Message, Scalar, Secp256k1, XOnlyPublicKey},
    sighash::{Prevouts, SighashCache, TapSighashType},
    taproot::{ControlBlock, LeafVersion, TapLeafHash, TapNodeHash, TapTweakHash},
    transaction, Amount, OutPoint, ScriptBuf, Sequence, Transaction, TxIn, TxOut, Witness,
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    str::FromStr,
};

const MAX_MONEY: u64 = 2_100_000_000_000_000;
const MIN_P2TR: u64 = 330;
const MAX_INPUTS: usize = 16;
const MAX_COLD_KEYS: usize = 16;

struct Policy {
    lock: absolute::LockTime,
    keys: Vec<XOnlyPublicKey>,
    threshold: usize,
}

fn decimal(v: &Value, name: &str, max: u64) -> Result<u64, E> {
    let raw = v[name].as_str().ok_or("recovery integer")?;
    let value: u64 = raw.parse()?;
    if value == 0 || value > max || raw != value.to_string() {
        return Err("recovery integer range or encoding".into());
    }
    Ok(value)
}

fn fixed_key(raw: &str) -> Result<XOnlyPublicKey, E> {
    if raw.len() != 64
        || !raw
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("recovery key encoding".into());
    }
    Ok(XOnlyPublicKey::from_str(raw)?)
}

impl Policy {
    fn parse(v: &Value) -> Result<Self, E> {
        let fields = v.as_object().ok_or("recovery policy")?;
        if fields.len() != 3 {
            return Err("recovery policy fields".into());
        }
        let lock = absolute::LockTime::from_height(decimal(v, "height", 499_999_999)? as u32)?;
        let roster = v["keys"].as_array().ok_or("recovery roster")?;
        if !(2..=MAX_COLD_KEYS).contains(&roster.len()) {
            return Err("recovery roster limit".into());
        }
        let threshold = decimal(v, "threshold", roster.len() as u64)? as usize;
        if threshold < 2 {
            return Err("recovery threshold".into());
        }
        let keys = roster
            .iter()
            .map(|item| fixed_key(item.as_str().ok_or("recovery roster key")?))
            .collect::<Result<Vec<_>, E>>()?;
        if !keys
            .windows(2)
            .all(|pair| pair[0].serialize() < pair[1].serialize())
        {
            return Err("recovery roster must be sorted and unique".into());
        }
        Ok(Self {
            lock,
            keys,
            threshold,
        })
    }

    fn leaf(&self) -> ScriptBuf {
        let mut builder = Builder::new()
            .push_lock_time(self.lock)
            .push_opcode(OP_CLTV)
            .push_opcode(OP_VERIFY);
        for (index, key) in self.keys.iter().enumerate() {
            builder = builder.push_x_only_key(key).push_opcode(if index == 0 {
                OP_CHECKSIG
            } else {
                OP_CHECKSIGADD
            });
        }
        builder
            .push_int(self.threshold as i64)
            .push_opcode(OP_NUMEQUAL)
            .into_script()
    }
}

pub(crate) struct Descriptor {
    policy: Policy,
    internal: XOnlyPublicKey,
    output: XOnlyPublicKey,
    leaf: ScriptBuf,
    control: ControlBlock,
    pub(crate) merkle_root: [u8; 32],
    tweak: [u8; 32],
}

impl Descriptor {
    pub(crate) fn new(v: &Value, internal_key: &str) -> Result<Self, E> {
        let policy = Policy::parse(v)?;
        let internal = fixed_key(internal_key)?;
        let leaf = policy.leaf();
        let merkle_root = TapNodeHash::from_script(&leaf, LeafVersion::TapScript);
        let tweak = TapTweakHash::from_key_and_tweak(internal, Some(merkle_root)).to_byte_array();
        let (output, parity) = internal.add_tweak(
            &Secp256k1::verification_only(),
            &Scalar::from_be_bytes(tweak)?,
        )?;
        if policy.keys.contains(&internal) || policy.keys.contains(&output) {
            return Err("recovery roster must be distinct from the active wallet".into());
        }
        let control =
            ControlBlock::decode(&[&[0xc0 | parity.to_u8()], &internal.serialize()[..]].concat())?;
        Ok(Self {
            policy,
            internal,
            output,
            leaf,
            control,
            merkle_root: merkle_root.to_byte_array(),
            tweak,
        })
    }

    pub(crate) fn output_key(&self) -> [u8; 32] {
        self.output.serialize()
    }

    fn script(&self) -> ScriptBuf {
        ScriptBuf::new_p2tr_tweaked(TweakedPublicKey::dangerous_assume_tweaked(self.output))
    }

    pub(crate) fn public_json(&self) -> Value {
        let keys = self
            .policy
            .keys
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(",");
        json!({
            "internal_key": self.internal.to_string(),
            "output_key": self.output.to_string(),
            "output_parity_odd": self.control.output_key_parity.to_u8() == 1,
            "leaf_script": hex::encode(self.leaf.as_bytes()),
            "merkle_root": hex::encode(self.merkle_root),
            "control_block": hex::encode(self.control.serialize()),
            "taproot_tweak": hex::encode(self.tweak),
            "script_pubkey": hex::encode(self.script().as_bytes()),
            "descriptor": format!(
                "tr({},and_v(v:after({}),multi_a({},{})))",
                self.internal, self.policy.lock.to_consensus_u32(), self.policy.threshold, keys
            )
        })
    }
}

pub(crate) fn descriptor(v: &Value) -> Result<Value, E> {
    Ok(Descriptor::new(
        &v["policy"],
        v["internal_key"].as_str().ok_or("recovery internal key")?,
    )?
    .public_json())
}

struct Recovery {
    wallet: Descriptor,
    tx: Transaction,
    prevouts: Vec<TxOut>,
    principal: u64,
    fee: u64,
}

fn previous(item: &Value) -> Result<(OutPoint, TxOut), E> {
    if item.as_object().is_none_or(|fields| fields.len() != 3) {
        return Err("recovery prevout schema".into());
    }
    let outpoint = item["outpoint"].as_str().ok_or("recovery outpoint")?;
    let id = OutPoint::from_str(outpoint)?;
    if id.is_null() || id.to_string() != outpoint {
        return Err("recovery outpoint encoding".into());
    }
    let script_pubkey = ScriptBuf::from_bytes(bytes(item, "script")?);
    if !script_pubkey.is_p2tr() {
        return Err("recovery prevout script".into());
    }
    XOnlyPublicKey::from_slice(&script_pubkey.as_bytes()[2..])?;
    let value = Amount::from_sat(decimal(item, "amount", MAX_MONEY)?);
    Ok((
        id,
        TxOut {
            value,
            script_pubkey,
        },
    ))
}

impl Recovery {
    fn new(v: &Value) -> Result<Self, E> {
        let wallet = Descriptor::new(
            &v["policy"],
            v["internal_key"].as_str().ok_or("recovery internal key")?,
        )?;
        let rows = v["inputs"].as_array().ok_or("recovery inputs")?;
        if rows.is_empty() || rows.len() > MAX_INPUTS {
            return Err("recovery input count".into());
        }
        let mut pairs = rows.iter().map(previous).collect::<Result<Vec<_>, E>>()?;
        let old_script = wallet.script();
        if pairs
            .iter()
            .any(|(_, previous)| previous.script_pubkey != old_script)
        {
            return Err("recovery input wallet mismatch".into());
        }
        let principal = pairs
            .iter()
            .map(|(_, previous)| previous.value.to_sat())
            .sum::<u64>();
        let sponsor = previous(&v["sponsor"])?;
        let sponsor_amount = sponsor.1.value.to_sat();
        let successor = ScriptBuf::from_bytes(bytes(v, "successor")?);
        if !successor.is_p2tr()
            || successor == old_script
            || successor == sponsor.1.script_pubkey
            || old_script == sponsor.1.script_pubkey
            || principal < MIN_P2TR
            || principal + sponsor_amount > MAX_MONEY
        {
            return Err("recovery successor or sponsor mismatch".into());
        }
        XOnlyPublicKey::from_slice(&successor.as_bytes()[2..])?;
        let fee = decimal(v, "fee", sponsor_amount)?;
        let extra = sponsor_amount - fee;
        let mut outputs = vec![TxOut {
            value: Amount::from_sat(principal),
            script_pubkey: successor,
        }];
        if extra != 0 {
            if extra < MIN_P2TR {
                return Err("recovery sponsor change below minimum".into());
            }
            outputs.push(TxOut {
                value: Amount::from_sat(extra),
                script_pubkey: sponsor.1.script_pubkey.clone(),
            });
        }
        pairs.push(sponsor);
        if pairs
            .iter()
            .map(|(id, _)| id)
            .collect::<BTreeSet<_>>()
            .len()
            != pairs.len()
        {
            return Err("duplicate recovery input".into());
        }
        let (input, prevouts) = pairs
            .into_iter()
            .map(|(previous_output, prevout)| {
                (
                    TxIn {
                        previous_output,
                        script_sig: ScriptBuf::new(),
                        sequence: Sequence::ENABLE_RBF_NO_LOCKTIME,
                        witness: Witness::new(),
                    },
                    prevout,
                )
            })
            .unzip();
        let tx = Transaction {
            version: transaction::Version::TWO,
            lock_time: wallet.policy.lock,
            input,
            output: outputs,
        };
        Ok(Self {
            wallet,
            tx,
            prevouts,
            principal,
            fee,
        })
    }

    fn sighashes(&self) -> Result<(Vec<[u8; 32]>, [u8; 32]), E> {
        let mut cache = SighashCache::new(&self.tx);
        let previous = Prevouts::All(&self.prevouts);
        let leaf = TapLeafHash::from_script(&self.wallet.leaf, LeafVersion::TapScript);
        let sponsor_index = self.prevouts.len() - 1;
        let hashes = (0..sponsor_index)
            .map(|index| {
                cache
                    .taproot_script_spend_signature_hash(
                        index,
                        &previous,
                        leaf,
                        TapSighashType::Default,
                    )
                    .map(|hash| hash.to_byte_array())
            })
            .collect::<Result<Vec<_>, _>>()?;
        let sponsor = cache.taproot_key_spend_signature_hash(
            sponsor_index,
            &previous,
            TapSighashType::Default,
        )?;
        Ok((hashes, sponsor.to_byte_array()))
    }

    fn verify(&self, tx: &Transaction) -> Result<(), E> {
        let mut stripped = tx.clone();
        for input in &mut stripped.input {
            input.witness = Witness::new();
        }
        if stripped != self.tx {
            return Err("recovery transaction differs from reviewed plan".into());
        }
        let secp = Secp256k1::verification_only();
        let (cold_hashes, sponsor_hash) = self.sighashes()?;
        let wallet = &self.wallet;
        let control = wallet.control.serialize();
        for (input, hash) in tx.input.iter().zip(cold_hashes) {
            let stack: Vec<_> = input.witness.iter().collect();
            let count = wallet.policy.keys.len();
            if stack.len() != count + 2
                || stack[count] != wallet.leaf.as_bytes()
                || stack[count + 1] != control
            {
                return Err("recovery witness format".into());
            }
            let mut signed = 0;
            for (item, key) in stack[..count].iter().rev().zip(&wallet.policy.keys) {
                if !item.is_empty() {
                    secp.verify_schnorr(
                        &Signature::from_slice(item)?,
                        &Message::from_digest(hash),
                        key,
                    )?;
                    signed += 1;
                }
            }
            if signed != wallet.policy.threshold {
                return Err("recovery quorum witness mismatch".into());
            }
        }
        let sponsor_index = self.prevouts.len() - 1;
        let witness = &tx.input[sponsor_index].witness;
        let sponsor_key = XOnlyPublicKey::from_slice(
            &self.prevouts[sponsor_index].script_pubkey.as_bytes()[2..],
        )?;
        if witness.len() != 1 {
            return Err("recovery sponsor witness format".into());
        }
        secp.verify_schnorr(
            &Signature::from_slice(witness.last().expect("one witness item"))?,
            &Message::from_digest(sponsor_hash),
            &sponsor_key,
        )?;
        Ok(())
    }
}

pub(crate) fn plan(v: &Value) -> Result<Value, E> {
    let recovery = Recovery::new(v)?;
    let mut psbt = Psbt::from_unsigned_tx(recovery.tx.clone())?;
    for (index, (input, prevout)) in psbt.inputs.iter_mut().zip(&recovery.prevouts).enumerate() {
        input.witness_utxo = Some(prevout.clone());
        input.sighash_type = Some(TapSighashType::Default.into());
        if index + 1 < recovery.prevouts.len() {
            input.tap_internal_key = Some(recovery.wallet.internal);
            input.tap_merkle_root = Some(TapNodeHash::from_byte_array(recovery.wallet.merkle_root));
            input.tap_scripts.insert(
                recovery.wallet.control.clone(),
                (recovery.wallet.leaf.clone(), LeafVersion::TapScript),
            );
        }
    }
    let (cold, sponsor) = recovery.sighashes()?;
    Ok(json!({
        "unsigned": serialize_hex(&recovery.tx),
        "txid": recovery.tx.compute_txid().to_string(),
        "psbt": hex::encode(psbt.serialize()),
        "principal_sats": recovery.principal.to_string(),
        "fee_sats": recovery.fee.to_string(),
        "cold_sighashes": cold.into_iter().map(hex::encode).collect::<Vec<_>>(),
        "sponsor_sighash": hex::encode(sponsor)
    }))
}

pub(crate) fn verify(v: &Value) -> Result<Value, E> {
    let recovery = Recovery::new(v)?;
    let raw = v["signed"].as_str().ok_or("recovery signed transaction")?;
    if raw.len() > 800_000 {
        return Err("recovery signed transaction size".into());
    }
    let raw = hex::decode(raw)?;
    let tx: Transaction = deserialize(&raw)?;
    if serialize(&tx) != raw {
        return Err("noncanonical recovery transaction".into());
    }
    recovery.verify(&tx)?;
    Ok(json!({"txid":tx.compute_txid().to_string(), "wtxid":tx.compute_wtxid().to_string()}))
}

/// Only accepts unfinalized offline PSBT signatures for the exact reviewed transaction.
/// No secret keys cross the SDK boundary.
pub(crate) fn attach(v: &Value) -> Result<Value, E> {
    let recovery = Recovery::new(v)?;
    let partials = v["partials"].as_array().ok_or("recovery partial PSBTs")?;
    let count = recovery.wallet.policy.keys.len();
    if partials.is_empty() || partials.len() > count + 1 {
        return Err("recovery partial count".into());
    }
    let sponsor_index = recovery.prevouts.len() - 1;
    let leaf_hash = TapLeafHash::from_script(&recovery.wallet.leaf, LeafVersion::TapScript);
    let mut signatures = vec![BTreeMap::new(); sponsor_index];
    let mut sponsor_signature = None;
    for item in partials {
        let text = item.as_str().ok_or("recovery partial PSBT encoding")?;
        if text.len() > 400_000 {
            return Err("recovery partial PSBT size".into());
        }
        let psbt = Psbt::deserialize(&hex::decode(text)?)?;
        if psbt.unsigned_tx != recovery.tx || psbt.inputs.len() != recovery.prevouts.len() {
            return Err("recovery partial PSBT transaction mismatch".into());
        }
        for (index, (input, prevout)) in psbt.inputs.iter().zip(&recovery.prevouts).enumerate() {
            if input.witness_utxo.as_ref() != Some(prevout)
                || input.final_script_sig.is_some()
                || input.final_script_witness.is_some()
                || !input.partial_sigs.is_empty()
            {
                return Err("recovery partial PSBT input mismatch".into());
            }
            if index == sponsor_index {
                if !input.tap_script_sigs.is_empty() {
                    return Err("recovery sponsor script signature".into());
                }
                if let Some(sig) = input.tap_key_sig {
                    if sig.sighash_type != TapSighashType::Default
                        || sponsor_signature.is_some_and(|previous| previous != sig.signature)
                    {
                        return Err("recovery sponsor signature mismatch".into());
                    }
                    sponsor_signature = Some(sig.signature);
                }
            } else {
                if input.tap_key_sig.is_some() {
                    return Err("recovery partial must use the cold leaf".into());
                }
                for ((key, leaf), sig) in &input.tap_script_sigs {
                    if *leaf != leaf_hash
                        || sig.sighash_type != TapSighashType::Default
                        || !recovery.wallet.policy.keys.contains(key)
                        || signatures[index]
                            .insert(*key, sig.signature)
                            .is_some_and(|previous| previous != sig.signature)
                    {
                        return Err("recovery cold signature mismatch".into());
                    }
                }
            }
        }
    }

    let secp = Secp256k1::verification_only();
    let (hashes, _) = recovery.sighashes()?;
    let mut signed = recovery.tx.clone();
    for ((input, sigs), hash) in signed.input.iter_mut().zip(signatures).zip(hashes) {
        if sigs.len() < recovery.wallet.policy.threshold {
            return Err("recovery partial quorum missing".into());
        }
        let message = Message::from_digest(hash);
        for (key, sig) in &sigs {
            secp.verify_schnorr(sig, &message, key)?;
        }
        let chosen = sigs
            .into_iter()
            .take(recovery.wallet.policy.threshold)
            .collect::<BTreeMap<_, _>>();
        for key in recovery.wallet.policy.keys.iter().rev() {
            match chosen.get(key) {
                Some(sig) => input.witness.push(sig.as_ref()),
                None => input.witness.push([]),
            };
        }
        input.witness.push(recovery.wallet.leaf.as_bytes());
        input.witness.push(recovery.wallet.control.serialize());
    }
    signed.input[sponsor_index].witness.push(
        sponsor_signature
            .ok_or("recovery sponsor signature missing")?
            .as_ref(),
    );
    recovery.verify(&signed)?;
    Ok(json!({
        "signed": serialize_hex(&signed),
        "txid": signed.compute_txid().to_string(),
        "wtxid": signed.compute_wtxid().to_string()
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use bitcoin::secp256k1::{Keypair, SecretKey};

    fn key(index: u8) -> Keypair {
        let secret = SecretKey::from_slice(&[index; 32]).unwrap();
        Keypair::from_secret_key(&Secp256k1::new(), &secret)
    }

    fn x(key: &Keypair) -> String {
        key.x_only_public_key().0.to_string()
    }

    fn policy() -> Value {
        let roster = BTreeSet::from([x(&key(1)), x(&key(2)), x(&key(3))]);
        json!({"height":"500","threshold":"2","keys":roster})
    }

    fn fixture() -> Value {
        let internal = x(&key(10));
        let policy = policy();
        let wallet = Descriptor::new(&policy, &internal).unwrap();
        json!({
            "policy":policy,
            "internal_key":internal,
            "inputs":[
                {"outpoint":format!("{}:2","12".repeat(32)),"script":hex::encode(wallet.script()),"amount":"100000"},
                {"outpoint":format!("{}:1","ab".repeat(32)),"script":hex::encode(wallet.script()),"amount":"200000"}
            ],
            "sponsor":{"outpoint":format!("{}:1","cd".repeat(32)),"script":format!("5120{}",x(&key(12))),"amount":"10000"},
            "successor":format!("5120{}",x(&key(13))),
            "fee":"2500"
        })
    }

    #[test]
    fn recovery_policy_rejects_shared_duplicate_noncanonical_or_out_of_range_keys() {
        let valid = policy();
        let internal = x(&key(10));
        assert!(Descriptor::new(&valid, &internal).is_ok());
        for (field, bad) in [
            ("height", json!("500000000")),
            ("height", json!("0500")),
            ("threshold", json!("1")),
            ("keys", json!([x(&key(1)), x(&key(1))])),
            ("keys", json!([x(&key(1)), "ff".repeat(32)])),
        ] {
            let mut invalid = valid.clone();
            invalid[field] = bad;
            assert!(Descriptor::new(&invalid, &internal).is_err());
        }
        assert!(Descriptor::new(&valid, &x(&key(1))).is_err());
    }

    #[test]
    fn recovery_plan_preserves_principal_requires_separate_sponsor_and_keeps_exact_order() {
        let valid = fixture();
        let recovery = Recovery::new(&valid).unwrap();
        assert_eq!(recovery.tx.lock_time.to_consensus_u32(), 500);
        assert!(recovery
            .tx
            .input
            .iter()
            .all(|input| input.sequence != Sequence::MAX));
        assert_eq!(
            recovery.tx.input[0].previous_output.to_string(),
            valid["inputs"][0]["outpoint"]
        );
        assert_eq!(
            recovery.tx.input[1].previous_output.to_string(),
            valid["inputs"][1]["outpoint"]
        );
        assert_eq!(
            recovery.tx.input[2].previous_output.to_string(),
            valid["sponsor"]["outpoint"]
        );
        assert_eq!(recovery.tx.output[0].value.to_sat(), 300_000);
        assert_eq!(recovery.tx.output[1].value.to_sat(), 7_500);
        assert_eq!(
            recovery.tx.output[1].script_pubkey,
            recovery.prevouts[2].script_pubkey
        );

        let mut wrong = valid.clone();
        wrong["inputs"][1] = wrong["inputs"][0].clone();
        assert!(Recovery::new(&wrong).is_err());
        wrong = valid.clone();
        wrong["sponsor"] = wrong["inputs"][0].clone();
        assert!(Recovery::new(&wrong).is_err());
        wrong = valid.clone();
        wrong["successor"] = wrong["inputs"][0]["script"].clone();
        assert!(Recovery::new(&wrong).is_err());
        wrong["successor"] = valid["successor"].clone();
        wrong["fee"] = json!("10001");
        assert!(Recovery::new(&wrong).is_err());
    }
}
