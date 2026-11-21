//! Bounded public PSBT v0 inspection via the pinned rust-bitcoin library.
//! Wallet keys/signing remain in Core or the user's external signer.
use crate::{bytes, E};
use bitcoin::{consensus::encode::{deserialize, serialize}, psbt::Psbt,
    sighash::{EcdsaSighashType, TapSighashType}, Transaction, Witness, ScriptBuf};
use serde_json::{json, Value};
use std::{collections::BTreeSet, io::Cursor};

const MAX_MONEY: u64 = 2_100_000_000_000_000;

fn witness(w: &Witness, taproot: bool) -> Result<(), E> {
    let elements: Vec<_> = w.iter().collect();
    let valid = if taproot {
        elements.len() == 1 && (elements[0].len() == 64 || elements[0].len() == 65 && elements[0][64] == 1)
    } else {
        elements.len() == 2 && elements[0].last() == Some(&1) && elements[1].len() == 33
    };
    if !valid { return Err("SMP1 witness/sighash profile".into()); }
    Ok(())
}

pub fn inspect(v: &Value) -> Result<Value, E> {
    let text = v["psbt"].as_str().ok_or("PSBT bytes")?;
    if text.len() > 2_097_152 { return Err("SMP1 PSBT envelope".into()); }
    let raw = hex::decode(text)?;
    let mut cursor = Cursor::new(&raw);
    let mut p = Psbt::deserialize_from_reader(&mut cursor)?;
    if cursor.position() != raw.len() as u64 || p.version != 0 || !p.proprietary.is_empty() || !p.unknown.is_empty() {
        return Err("SMP1 PSBT version/metadata".into());
    }
    let tx = &p.unsigned_tx;
    if tx.version.0 != 2 || tx.lock_time.to_consensus_u32() != 0 || tx.is_coinbase()
        || tx.input.is_empty() || tx.input.len() > 256 || tx.output.is_empty() || tx.output.len() > 32
        || serialize(tx).len() > 100_000 {
        return Err("SMP1 transaction profile".into());
    }
    let signed = v["allow_signed"].as_bool().unwrap_or(false);
    let mut total = 0u64;
    let mut seen = BTreeSet::new();
    let mut inputs = Vec::new();
    let mut has_signatures = false;
    for (input, map) in tx.input.iter().zip(&p.inputs) {
        let prev = map.witness_utxo.as_ref().ok_or("SMP1 witness UTXO required")?;
        let taproot = prev.script_pubkey.is_p2tr();
        if (!taproot && !prev.script_pubkey.is_p2wpkh()) || !seen.insert(input.previous_output)
            || input.previous_output.is_null() || !input.script_sig.is_empty() || !input.witness.is_empty()
            || input.sequence.to_consensus_u32() < 0xfffffffe
            || map.redeem_script.is_some() || map.witness_script.is_some() || !map.tap_scripts.is_empty()
            || !map.tap_script_sigs.is_empty() || !map.proprietary.is_empty() || !map.unknown.is_empty()
            || map.sighash_type.is_some_and(|s| if taproot { ![0,1].contains(&s.to_u32()) } else { s.to_u32() != 1 })
            || map.partial_sigs.values().any(|s| s.sighash_type != EcdsaSighashType::All)
            || taproot && !map.partial_sigs.is_empty() || !taproot && map.tap_key_sig.is_some()
            || map.tap_key_sig.is_some_and(|s| ![TapSighashType::Default,TapSighashType::All].contains(&s.sighash_type))
            || map.final_script_sig.as_ref().is_some_and(|s| !s.is_empty()) {
            return Err("SMP1 input/sighash profile".into());
        }
        if !signed && (!map.partial_sigs.is_empty() || map.tap_key_sig.is_some()
            || map.final_script_witness.is_some() || map.final_script_sig.is_some()) {
            return Err("SMP1 already signed".into());
        }
        if let Some(w) = &map.final_script_witness { witness(w,taproot)?; }
        has_signatures |= !map.partial_sigs.is_empty() || map.tap_key_sig.is_some() || map.final_script_witness.is_some();
        if let Some(parent) = &map.non_witness_utxo {
            if parent.compute_txid() != input.previous_output.txid
                || parent.output.get(input.previous_output.vout as usize) != Some(prev) {
                return Err("SMP1 parent mismatch".into());
            }
        }
        if prev.value.to_sat() == 0 { return Err("SMP1 zero input".into()); }
        total = total.checked_add(prev.value.to_sat()).filter(|n| *n <= MAX_MONEY).ok_or("SMP1 money range")?;
        inputs.push(json!({"outpoint":input.previous_output.to_string(), "amount":prev.value.to_sat().to_string(),
            "script":hex::encode(prev.script_pubkey.as_bytes())}));
    }
    if v.get("replace_marker").is_some() {
        if signed { return Err("SMP1 signed mutation".into()); }
        let change = &v["replace_marker"];
        let index: usize = change["vout"].as_str().ok_or("SMP1 marker index")?.parse()?;
        let script = bytes(change,"script")?;
        let out = p.unsigned_tx.output.get_mut(index).ok_or("SMP1 marker index")?;
        if out.value.to_sat() != 0 || !out.script_pubkey.as_bytes().starts_with(&hex::decode("6a38534d5031")?)
            || script.len() != 58 || !script.starts_with(&hex::decode("6a38534d5031")?) {
            return Err("SMP1 marker profile".into());
        }
        out.script_pubkey = ScriptBuf::from_bytes(script);
    }
    let tx = &p.unsigned_tx;
    let mut output_sum = 0u64;
    let mut outputs = Vec::new();
    for (index, (out, map)) in tx.output.iter().zip(&p.outputs).enumerate() {
        if !map.proprietary.is_empty() || !map.unknown.is_empty() { return Err("SMP1 output metadata".into()); }
        output_sum = output_sum.checked_add(out.value.to_sat()).filter(|n| *n <= MAX_MONEY).ok_or("SMP1 money range")?;
        outputs.push(json!({"vout":index.to_string(),"amount":out.value.to_sat().to_string(),"script":hex::encode(out.script_pubkey.as_bytes())}));
    }
    let fee = total.checked_sub(output_sum).ok_or("SMP1 negative fee")?;
    let mut result = json!({"profile":"SMP1_PSBT_V0_NATIVE_SEGWIT_V1", "psbt":hex::encode(p.serialize()),
        "unsigned":hex::encode(serialize(tx)),"txid":tx.compute_txid().to_string(),"inputs":inputs,"outputs":outputs,
        "fee_sats":fee.to_string(),"has_signatures":has_signatures});
    if v.get("raw").is_some() {
        let raw = bytes(v,"raw")?;
        if raw.len() > 400_000 { return Err("SMP1 raw envelope".into()); }
        let mut final_tx: Transaction = deserialize(&raw)?;
        if serialize(&final_tx) != raw || final_tx.input.len() != p.inputs.len() { return Err("SMP1 final transaction".into()); }
        for (input, map) in final_tx.input.iter_mut().zip(&p.inputs) {
            witness(&input.witness,map.witness_utxo.as_ref().unwrap().script_pubkey.is_p2tr())?;
            input.witness = Witness::new();
        }
        if serialize(&final_tx) != serialize(tx) { return Err("SMP1 transaction changed".into()); }
        let final_tx: Transaction = deserialize(&raw)?;
        if final_tx.weight().to_wu() > 400_000 { return Err("SMP1 standard weight".into()); }
        result["raw"] = json!(hex::encode(raw));
        result["vsize"] = json!(final_tx.vsize().to_string());
        result["wtxid"] = json!(final_tx.compute_wtxid().to_string());
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use bitcoin::{absolute,transaction,TxIn,TxOut,OutPoint,Sequence,Amount};
    use std::str::FromStr;
    fn fixture() -> Psbt {
        let tx=Transaction { version:transaction::Version::TWO,lock_time:absolute::LockTime::ZERO,
            input:vec![TxIn { previous_output:OutPoint::from_str(&format!("{}:0","11".repeat(32))).unwrap(),
                sequence:Sequence::MAX,script_sig:ScriptBuf::new(),witness:Witness::new() }],
            output:vec![TxOut { value:Amount::from_sat(99000),script_pubkey:ScriptBuf::from_bytes(hex::decode(format!("5120{}","22".repeat(32))).unwrap()) }] };
        let mut p=Psbt::from_unsigned_tx(tx).unwrap();
        p.inputs[0].witness_utxo=Some(TxOut { value:Amount::from_sat(100000),
            script_pubkey:ScriptBuf::from_bytes(hex::decode(format!("5120{}","33".repeat(32))).unwrap()) });
        p
    }
    fn request(p:&Psbt)->Value { json!({"psbt":hex::encode(p.serialize())}) }

    #[test]
    fn psbt_v0_money_and_unsigned_hash_come_from_library() {
        let p=fixture();let got=inspect(&request(&p)).unwrap();
        assert_eq!(got["fee_sats"],"1000");
        assert_eq!(got["txid"],p.unsigned_tx.compute_txid().to_string());
        assert_eq!(got["unsigned"],hex::encode(serialize(&p.unsigned_tx)));
    }
    #[test]
    fn unsigned_template_rejects_signatures_rbf_and_wrong_sighash() {
        let mut p=fixture();p.unsigned_tx.input[0].sequence=Sequence::ENABLE_RBF_NO_LOCKTIME;
        assert!(inspect(&request(&p)).is_err());
        let mut p=fixture();p.inputs[0].sighash_type=Some(bitcoin::psbt::PsbtSighashType::from_u32(0x81));
        assert!(inspect(&request(&p)).is_err());
        let mut p=fixture();p.inputs[0].final_script_witness=Some(Witness::from_slice(&[vec![0x44;64]]));
        assert!(inspect(&request(&p)).is_err());
    }
    #[test]
    fn contradictory_parent_and_negative_fee_are_rejected() {
        let mut p=fixture();p.inputs[0].non_witness_utxo=Some(p.unsigned_tx.clone());
        assert!(inspect(&request(&p)).is_err());
        let mut p=fixture();p.unsigned_tx.output[0].value=Amount::from_sat(100001);
        assert!(inspect(&request(&p)).is_err());
    }
    #[test]
    fn finalized_witness_does_not_authorize_new_outputs() {
        let p=fixture();let mut tx=p.unsigned_tx.clone();
        tx.input[0].witness=Witness::from_slice(&[vec![0x44;64]]);
        let mut v=request(&p);v["raw"]=json!(hex::encode(serialize(&tx)));
        assert!(inspect(&v).is_ok());
        tx.output[0].value=Amount::from_sat(98999);v["raw"]=json!(hex::encode(serialize(&tx)));
        assert!(inspect(&v).is_err());
    }
    #[test]
    fn legacy_and_taproot_script_path_are_separate_unsupported_profiles() {
        let mut p=fixture();p.inputs[0].witness_utxo.as_mut().unwrap().script_pubkey=
            ScriptBuf::from_bytes(hex::decode(format!("a914{}87","11".repeat(20))).unwrap());
        assert!(inspect(&request(&p)).is_err());
        let p=fixture();let mut tx=p.unsigned_tx.clone();
        tx.input[0].witness=Witness::from_slice(&[vec![0x44;64],vec![0x51],vec![0xc0;33]]);
        let mut v=request(&p);v["raw"]=json!(hex::encode(serialize(&tx)));
        assert!(inspect(&v).is_err());
    }
}
