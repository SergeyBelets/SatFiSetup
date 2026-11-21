//! Versioned per-input key selection. No caller-supplied tweak or output key.
//! Canonical DKG/intent provenance and nonce spending remain the caller's duty.
use crate::{
    bytes,
    deposit_derivation::{self as derivation, DepositIntent},
    E,
};
use frost_secp256k1_tr::{self as frost, keys::EvenY};
use serde_json::{json, Value};

pub(crate) struct InputKey {
    group: Vec<u8>,
    descriptor: Option<derivation::Descriptor>,
    script: Vec<u8>,
    wallet_context: [u8; 32],
}

impl InputKey {
    pub(crate) fn parse(v: &Value) -> Result<Self, E> {
        let fields = v.as_object().ok_or("input key schema")?;
        if v["version"].as_u64() != Some(1) {
            return Err("input key version".into());
        }
        let group = bytes(v, "base_group")?;
        if group.len() != 33 || group[0] != 2 {
            return Err("base key encoding".into());
        }
        bitcoin::secp256k1::XOnlyPublicKey::from_slice(&group[1..])?;
        let wallet_context: [u8; 32] = bytes(v, "wallet_context")?
            .try_into()
            .map_err(|_| "wallet context length")?;
        if wallet_context == [0; 32] {
            return Err("zero wallet context".into());
        }
        let (descriptor, script) = match v["mode"].as_str() {
            Some("BASE") if fields.len() == 4 => (None, [&[0x51, 0x20], &group[1..]].concat()),
            Some("DERIVED") if fields.len() == 6 => {
                let intent = DepositIntent::from_canonical(&bytes(v, "intent")?)?;
                if intent.wallet_context() != wallet_context {
                    return Err("wallet context mismatch".into());
                }
                let desc = derivation::descriptor(&group, &intent)?;
                if bytes(v, "binding_commitment")? != desc.binding_commitment {
                    return Err("input binding mismatch".into());
                }
                (Some(desc.clone()), desc.script_pubkey)
            }
            Some("WITHDRAWAL_FEE") if fields.len() == 6 => {
                let raw=bytes(v,"intent")?;let parent=crate::withdrawal_fee::parse(&raw)?;
                if parent.wallet_context()!=wallet_context {return Err("wallet context mismatch".into());}
                let desc=crate::withdrawal_fee::descriptor(&group,&raw)?;
                if bytes(v,"binding_commitment")?!=desc.binding_commitment {return Err("fee input binding mismatch".into());}
                (Some(desc.clone()),desc.script_pubkey)
            }
            _ => return Err("input key mode or schema".into()),
        };
        Ok(Self {
            group,
            descriptor,
            script,
            wallet_context,
        })
    }

    pub(crate) fn group(&self) -> &[u8] {
        &self.group
    }
    pub(crate) fn script(&self) -> &[u8] {
        &self.script
    }
    pub(crate) fn wallet_context(&self) -> &[u8; 32] {
        &self.wallet_context
    }

    pub(crate) fn public(
        &self,
        base: &frost::keys::PublicKeyPackage,
    ) -> Result<frost::keys::PublicKeyPackage, E> {
        if !base.has_even_y() || base.verifying_key().serialize()? != self.group {
            return Err("input public package mismatch".into());
        }
        base.serialize()?;
        Ok(match &self.descriptor {
            None => base.clone(),
            Some(desc) => derivation::derive_public_descriptor(base, desc.clone())?.1,
        })
    }

    pub(crate) fn key(
        &self,
        key: &frost::keys::KeyPackage,
        base: &frost::keys::PublicKeyPackage,
    ) -> Result<frost::keys::KeyPackage, E> {
        self.public(base)?;
        if !key.has_even_y()
            || key.verifying_key() != base.verifying_key()
            || base.verifying_shares().get(key.identifier()) != Some(key.verifying_share())
            || frost::keys::VerifyingShare::from(*key.signing_share()) != *key.verifying_share()
        {
            return Err("input key package mismatch".into());
        }
        key.serialize()?;
        Ok(match &self.descriptor {
            None => key.clone(),
            Some(desc) => derivation::derive_key_descriptor(key, base, desc.clone())?.1,
        })
    }
}

pub(crate) fn derive(v: &Value) -> Result<Value, E> {
    let fields = v.as_object().ok_or("derivation schema")?;
    if fields.len() != 3 {
        return Err("derivation schema".into());
    }
    let raw = bytes(v, "intent")?;
    let intent = DepositIntent::from_canonical(&raw)?;
    let desc = derivation::descriptor(&bytes(v, "base_group")?, &intent)?;
    Ok(json!({
        "profile":derivation::PROFILE,
        "intent_id":hex::encode(desc.intent_id),
        "base_group":hex::encode(&desc.base_group),
        "leaf_script":hex::encode(desc.leaf_script),
        "merkle_root":hex::encode(desc.merkle_root),
        "tweak":hex::encode(desc.tweak),
        "output_key":hex::encode(desc.output_key),
        "output_parity_odd":desc.output_parity_odd,
        "script_pubkey":hex::encode(desc.script_pubkey),
        "binding_commitment":hex::encode(desc.binding_commitment),
        "wallet_context":hex::encode(intent.wallet_context()),
        "input_key":{"version":1,"mode":"DERIVED","base_group":hex::encode(&desc.base_group),
            "intent":hex::encode(raw),"binding_commitment":hex::encode(desc.binding_commitment),
            "wallet_context":hex::encode(intent.wallet_context())},
        "ready":false
    }))
}
