use crate::{bytes, E};
use bitcoin::{
    absolute,
    consensus::encode::{deserialize, serialize, serialize_hex},
    secp256k1::{schnorr::Signature, Message, Secp256k1, XOnlyPublicKey},
    sighash::{Prevouts, SighashCache, TapSighashType},
    transaction, Amount, OutPoint, ScriptBuf, Sequence, Transaction, TxIn, TxOut, Witness,
};
use serde_json::{json, Value};
use std::{collections::BTreeSet, str::FromStr};

/// Public address encoding uses the pinned Bitcoin library, never a Python codec.
pub fn script_address(v: &Value) -> Result<Value, E> {
    let script = ScriptBuf::from_bytes(bytes(v,"script")?);
    if !script.is_p2tr() { return Err("address script profile".into()); }
    let network = match v["network"].as_str() {
        Some("mainnet") => bitcoin::Network::Bitcoin,
        Some("testnet4") => bitcoin::Network::Testnet4,
        Some("regtest") => bitcoin::Network::Regtest,
        _ => return Err("address network".into()),
    };
    Ok(json!({"address":bitcoin::Address::from_script(&script,network)?.to_string()}))
}

/// Public destination parsing uses rust-bitcoin's checksum/network rules.
pub fn address_script(v: &Value) -> Result<Value, E> {
    use std::str::FromStr;
    let network = match v["network"].as_str() {
        Some("mainnet") => bitcoin::Network::Bitcoin,
        Some("testnet4") => bitcoin::Network::Testnet4,
        Some("regtest") => bitcoin::Network::Regtest,
        _ => return Err("address network".into()),
    };
    let address = bitcoin::Address::from_str(v["address"].as_str().ok_or("address")?)?.require_network(network)?;
    let script = address.script_pubkey();
    if !script.is_p2tr() && !script.is_p2wpkh() { return Err("withdrawal script profile".into()); }
    Ok(json!({"address":address.to_string(),"script":hex::encode(script.as_bytes())}))
}

#[cfg(test)]
mod destination_tests {
    use super::*;
    #[test]
    fn destinations_check_checksum_network_and_escrow_script_profile() {
        for (name, network) in [("mainnet", bitcoin::Network::Bitcoin), ("testnet4", bitcoin::Network::Testnet4), ("regtest", bitcoin::Network::Regtest)] {
            for raw in [format!("0014{}", "11".repeat(20)), format!("5120{}", "22".repeat(32))] {
                let script = ScriptBuf::from_bytes(hex::decode(&raw).unwrap());
                let address = bitcoin::Address::from_script(&script, network).unwrap().to_string();
                assert_eq!(address_script(&json!({"address":address,"network":name})).unwrap()["script"], raw);
                let wrong = if name == "mainnet" { "regtest" } else { "mainnet" };
                assert!(address_script(&json!({"address":address,"network":wrong})).is_err());
                let mut bad = address.clone(); bad.pop(); bad.push('q');
                if bad == address { bad.pop(); bad.push('p'); }
                assert!(address_script(&json!({"address":bad,"network":name})).is_err());
            }
            let legacy = ScriptBuf::from_bytes(hex::decode(format!("76a914{}88ac", "11".repeat(20))).unwrap());
            let address = bitcoin::Address::from_script(&legacy, network).unwrap().to_string();
            assert!(address_script(&json!({"address":address,"network":name})).is_err());
        }
    }
}

/// Bounded public watch scan; Bitcoin consensus/chain selection stays with Core.
pub fn watch_block(v: &Value) -> Result<Value, E> {
    let text = v["raw"].as_str().ok_or("block bytes")?;
    if text.len() > 8_000_000 { return Err("block envelope".into()); }
    let raw = hex::decode(text)?;
    let block: bitcoin::Block = deserialize(&raw)?;
    if serialize(&block) != raw || !block.check_merkle_root()
        || v["hash"].as_str() != Some(block.block_hash().to_string().as_str()) {
        return Err("block binding".into());
    }
    let scripts = v["scripts"].as_array().ok_or("watch scripts")?;
    let prior = v["outpoints"].as_array().ok_or("watch outpoints")?;
    if scripts.is_empty() || scripts.len()>256 || prior.len()>4096 { return Err("watch limit".into()); }
    let scripts = scripts.iter().map(|s| {
        let script = hex::decode(s.as_str().ok_or("watch script")?)?;
        if !ScriptBuf::from_bytes(script.clone()).is_p2tr() { return Err("watch P2TR".into()); }
        Ok(script)
    }).collect::<Result<BTreeSet<Vec<u8>>,E>>()?;
    let mut known = prior.iter().map(|s| {
        Ok(OutPoint::from_str(s.as_str().ok_or("watch outpoint")?)?)
    }).collect::<Result<BTreeSet<OutPoint>,E>>()?;
    let mut outputs=Vec::new(); let mut spends=Vec::new();
    for tx in &block.txdata {
        let txid=tx.compute_txid();
        for input in &tx.input {
            if known.contains(&input.previous_output) {
                spends.push(json!({"outpoint":input.previous_output.to_string(),"txid":txid.to_string()}));
            }
        }
        for (i,output) in tx.output.iter().enumerate() {
            if scripts.contains(output.script_pubkey.as_bytes()) {
                let outpoint=OutPoint{txid,vout:i.try_into()?};
                if outputs.len()>=4096 {
                    return Err("watch output budget".into());
                }
                known.insert(outpoint);
                outputs.push(json!({"outpoint":outpoint.to_string(),"script":hex::encode(output.script_pubkey.as_bytes()),
                    "satoshis":output.value.to_sat().to_string(),"coinbase":tx.is_coinbase()}));
            }
        }
    }
    Ok(json!({"hash":block.block_hash().to_string(),"parent":block.header.prev_blockhash.to_string(),
        "outputs":outputs,"spends":spends,"transactions":block.txdata.len().to_string()}))
}

/// External funding is independent of our deliberately smaller payment plan.
/// The existing pinned rust-bitcoin codec checks CompactSize, complete witness
/// structure and trailing data. No Bitcoin consensus implementation is added.
pub fn funding(v: &Value) -> Result<Value, E> {
    let raw_hex = v["raw"].as_str().ok_or("funding raw")?;
    if raw_hex.len() > 8_000_000 {
        return Err("funding raw envelope".into());
    }
    let raw = hex::decode(raw_hex)?;
    let mut tx: Transaction = deserialize(&raw)?;
    if serialize(&tx) != raw || tx.input.is_empty() || tx.output.is_empty() {
        return Err("noncanonical funding transaction".into());
    }
    let txid = tx.compute_txid().to_string();
    if v["txid"].as_str() != Some(txid.as_str()) {
        return Err("funding txid mismatch".into());
    }
    let wtxid = tx.compute_wtxid().to_string();
    let weight = tx.weight().to_wu();
    let coinbase = tx.is_coinbase();
    for input in &mut tx.input {
        input.witness = Witness::new();
    }
    let stripped = serialize(&tx);
    if stripped.len() > 100_000 {
        return Err("funding stripped envelope".into());
    }
    let inputs: Vec<_> = tx.input.iter().map(|input| json!({
        "outpoint": input.previous_output.to_string(),
        "script_sig": hex::encode(input.script_sig.as_bytes()),
        "sequence": input.sequence.to_consensus_u32().to_string(),
    })).collect();
    let outputs: Vec<_> = tx.output.iter().enumerate().map(|(index, output)| json!({
        "vout": index.to_string(), "amount": output.value.to_sat().to_string(),
        "script": hex::encode(output.script_pubkey.as_bytes()),
    })).collect();
    Ok(json!({"profile":"FUNDING_STRIPPED_100K_V1", "parser":"rust-bitcoin-0.32.7",
        "raw":raw_hex, "stripped":hex::encode(&stripped), "txid":txid, "wtxid":wtxid,
        "raw_bytes":raw.len().to_string(), "stripped_bytes":stripped.len().to_string(),
        "weight":weight.to_string(), "coinbase":coinbase, "inputs":inputs, "outputs":outputs}))
}
fn amount(v: &Value) -> Result<u64, E> {
    let n = v
        .as_str()
        .ok_or("amounts must be base unit strings")?
        .parse::<u64>()?;
    if n > 2_100_000_000_000_000 {
        return Err("money range".into());
    }
    Ok(n)
}
pub fn build(v: &Value) -> Result<Value, E> {
    build_mode(v, false)
}
pub fn build_v1(v: &Value) -> Result<Value, E> {
    build_mode(v, true)
}
fn build_mode(v: &Value, versioned: bool) -> Result<Value, E> {
    let inputs = v["inputs"].as_array().ok_or("inputs")?;
    let outputs = v["outputs"].as_array().ok_or("outputs")?;
    if inputs.is_empty() || inputs.len() > 16 || outputs.is_empty() || outputs.len() > 32 {
        return Err("batch limit".into());
    }
    let wallet = ScriptBuf::from_bytes(bytes(v, "wallet_script")?);
    if !wallet.is_p2tr() {
        return Err("P2TR inputs required".into());
    }
    let input_keys = if versioned {
        if v["input_key_version"].as_u64() != Some(1) {
            return Err("input key version".into());
        }
        let descriptors = v["input_keys"].as_array().ok_or("input keys")?;
        if descriptors.len() != inputs.len() { return Err("input key count".into()); }
        let keys = descriptors.iter().map(crate::bitcoin_input_key::InputKey::parse)
            .collect::<Result<Vec<_>, _>>()?;
        for key in &keys {
            if key.group()[1..] != wallet.as_bytes()[2..]
                || key.wallet_context() != keys[0].wallet_context() {
                return Err("mixed base key families".into());
            }
        }
        Some(keys)
    } else {
        if v.get("input_keys").is_some() || v.get("input_key_version").is_some() {
            return Err("versioned input keys require versioned builder".into());
        }
        None
    };
    let mut seen = BTreeSet::new();
    let mut prevouts = vec![];
    let mut vin = vec![];
    let mut total_in = 0u64;
    for (i, item) in inputs.iter().enumerate() {
        let outpoint = OutPoint::from_str(item["outpoint"].as_str().ok_or("outpoint")?)?;
        if !seen.insert(outpoint) {
            return Err("duplicate input".into());
        }
        let script = ScriptBuf::from_bytes(bytes(item, "script")?);
        let expected = input_keys.as_ref().map(|keys| keys[i].script()).unwrap_or(wallet.as_bytes());
        if script.as_bytes() != expected {
            return Err("input ownership".into());
        }
        let value = amount(&item["amount"])?;
        total_in = total_in.checked_add(value).ok_or("overflow")?;
        prevouts.push(TxOut {
            value: Amount::from_sat(value),
            script_pubkey: script,
        });
        vin.push(TxIn {
            previous_output: outpoint,
            script_sig: ScriptBuf::new(),
            sequence: Sequence::ENABLE_RBF_NO_LOCKTIME,
            witness: Witness::new(),
        });
    }
    let mut vout = vec![];
    let mut total_out = 0u64;
    for item in outputs {
        let script = ScriptBuf::from_bytes(bytes(item, "script")?);
        if !script.is_p2tr() && !script.is_p2wpkh() {
            return Err("recipient script policy".into());
        }
        let value = amount(&item["amount"])?;
        if value < if script.is_p2tr() { 330 } else { 294 } {
            return Err("dust".into());
        }
        total_out = total_out.checked_add(value).ok_or("overflow")?;
        vout.push(TxOut {
            value: Amount::from_sat(value),
            script_pubkey: script,
        });
    }
    let fee = total_in
        .checked_sub(total_out)
        .ok_or("insufficient input")?;
    if fee != amount(&v["fee"])? || fee > amount(&v["max_fee"])? {
        return Err("fee policy".into());
    }
    let tx = Transaction {
        version: transaction::Version::TWO,
        lock_time: absolute::LockTime::ZERO,
        input: vin,
        output: vout,
    };
    let mut cache = SighashCache::new(&tx);
    let mut hashes = vec![];
    for i in 0..inputs.len() {
        hashes.push(
            cache
                .taproot_key_spend_signature_hash(
                    i,
                    &Prevouts::All(&prevouts),
                    TapSighashType::Default,
                )?
                .to_string(),
        );
    }
    Ok(
        json!({"unsigned":serialize_hex(&tx),"txid":tx.compute_txid().to_string(),"sighashes":hashes,"fee":fee.to_string()}),
    )
}
pub fn attach(v: &Value) -> Result<Value, E> {
    attach_mode(v, false)
}
pub fn attach_v1(v: &Value) -> Result<Value, E> {
    attach_mode(v, true)
}
fn attach_mode(v: &Value, versioned: bool) -> Result<Value, E> {
    let mut tx: Transaction = deserialize(&bytes(v, "unsigned")?)?;
    let package = build_mode(&v["package"], versioned)?;
    if serialize_hex(&tx) != package["unsigned"].as_str().ok_or("unsigned")? {
        return Err("transaction mismatch".into());
    }
    let signatures = v["signatures"].as_array().ok_or("signatures")?;
    if signatures.len() != tx.input.len() {
        return Err("signature count".into());
    }
    let secp = Secp256k1::verification_only();
    for (i, s) in signatures.iter().enumerate() {
        let script = bytes(&v["package"]["inputs"][i], "script")?;
        let pk = XOnlyPublicKey::from_slice(&script[2..])?;
        let signature = hex::decode(s.as_str().ok_or("signature")?)?;
        let hash = hex::decode(package["sighashes"][i].as_str().ok_or("hash")?)?;
        secp.verify_schnorr(
            &Signature::from_slice(&signature)?,
            &Message::from_digest_slice(&hash)?,
            &pk,
        )?;
        tx.input[i].witness = Witness::from_slice(&[signature]);
    }
    Ok(
        json!({"signed":serialize_hex(&tx),"txid":tx.compute_txid().to_string(),"wtxid":tx.compute_wtxid().to_string()}),
    )
}

#[cfg(test)]
mod funding_tests {
    use super::*;

    fn transaction(inputs: usize, outputs: usize) -> Transaction {
        Transaction {
            version: transaction::Version::TWO,
            lock_time: absolute::LockTime::ZERO,
            input: (0..inputs).map(|index| TxIn {
                previous_output: OutPoint::from_str(&format!("{}:{}", "11".repeat(32), index)).unwrap(),
                script_sig: ScriptBuf::from_bytes(vec![0x51]),
                sequence: Sequence::MAX,
                witness: Witness::new(),
            }).collect(),
            output: (0..outputs).map(|_| TxOut {
                value: Amount::from_sat(1000),
                // Foreign outputs are accepted; this is not a payment template.
                script_pubkey: ScriptBuf::from_bytes(vec![0x51]),
            }).collect(),
        }
    }

    fn inspect(tx: &Transaction) -> Value {
        funding(&json!({"raw":serialize_hex(tx), "txid":tx.compute_txid().to_string()})).unwrap()
    }

    #[test]
    fn external_counts_are_bounded_by_bytes_not_payment_or_256_limits() {
        for outputs in [33, 256, 257] {
            let tx = transaction(257, outputs);
            let result = inspect(&tx);
            assert_eq!(result["inputs"].as_array().unwrap().len(), 257);
            assert_eq!(result["outputs"].as_array().unwrap().len(), outputs);
            assert_eq!(result["stripped"], serialize_hex(&tx));
        }
    }

    #[test]
    fn full_witness_can_exceed_stripped_envelope_and_txid_is_not_wtxid() {
        let mut tx = transaction(1, 1);
        tx.input[0].witness = Witness::from_slice(&[vec![1u8; 100_001]]);
        let result = inspect(&tx);
        assert!(result["raw_bytes"].as_str().unwrap().parse::<usize>().unwrap() > 100_000);
        assert!(result["stripped_bytes"].as_str().unwrap().parse::<usize>().unwrap() < 100_000);
        assert_ne!(result["txid"], result["wtxid"]);
        assert!(funding(&json!({"raw":serialize_hex(&tx), "txid":tx.compute_wtxid().to_string()})).is_err());
        tx.input[0].witness = Witness::new();
        assert_eq!(result["stripped"], serialize_hex(&tx));
    }

    #[test]
    fn exact_stripped_boundary_is_inclusive() {
        let mut tx = transaction(1, 1);
        // 4 version + 1 vin count + 42 input + 1 vout count + 8 value +
        // 5 CompactSize + script + 4 locktime = 65 + script.
        tx.output[0].script_pubkey = ScriptBuf::from_bytes(vec![0x51; 100_000 - 65]);
        assert_eq!(serialize(&tx).len(), 100_000);
        assert_eq!(inspect(&tx)["stripped_bytes"], "100000");
        tx.output[0].script_pubkey = ScriptBuf::from_bytes(vec![0x51; 100_001 - 65]);
        assert!(funding(&json!({"raw":serialize_hex(&tx), "txid":tx.compute_txid().to_string()})).is_err());
    }

    #[test]
    fn malformed_trailing_and_noncanonical_compactsize_are_rejected() {
        let tx = transaction(1, 1);
        let raw = serialize(&tx);
        for size in [0, 4, raw.len() - 1] {
            assert!(funding(&json!({"raw":hex::encode(&raw[..size]), "txid":tx.compute_txid().to_string()})).is_err());
        }
        let mut trailing = raw.clone(); trailing.push(0);
        assert!(funding(&json!({"raw":hex::encode(trailing), "txid":tx.compute_txid().to_string()})).is_err());
        let mut nonminimal = raw[..4].to_vec();
        nonminimal.extend([0xfd, 1, 0]); nonminimal.extend(&raw[5..]);
        assert!(funding(&json!({"raw":hex::encode(nonminimal), "txid":tx.compute_txid().to_string()})).is_err());
        assert!(funding(&json!({"raw":serialize_hex(&tx), "txid":"00".repeat(32)})).is_err());
    }
}
