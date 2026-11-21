//! Separate purpose, using the same pinned BIP341/FROST arithmetic and nonce lane.
use crate::{bytes, deposit_derivation::{self as d,DepositIntent}, E};
use serde_json::{json,Value};
pub(crate) const DOMAIN: &[u8]=b"SATOHSI_WITHDRAWAL_FEE_V1\0";
pub(crate) const PROFILE: &str="BASE_POST_DKG_EVEN_Y__WITHDRAWAL_FEE_TAPLEAF_V1";
pub(crate) fn parse(raw: &[u8])->Result<DepositIntent,E> {
    if raw.len()!=DOMAIN.len()+478 || &raw[..DOMAIN.len()]!=DOMAIN {return Err("withdrawal fee intent".into());}
    let parent=DepositIntent::from_canonical(&raw[DOMAIN.len()..DOMAIN.len()+382])?;
    for chunk in raw[DOMAIN.len()+382..].chunks_exact(32) {if chunk==[0;32] {return Err("zero fee binding".into());}}
    Ok(parent)
}
pub(crate) fn descriptor(group: &[u8],raw: &[u8])->Result<d::Descriptor,E> {
    parse(raw)?;d::descriptor_raw(group,raw,b"SATOHSI_WITHDRAWAL_FEE_BINDING_V1\0")
}
pub(crate) fn derive(v: &Value)->Result<Value,E> {
    if v.as_object().ok_or("fee derivation schema")?.len()!=3 {return Err("fee derivation schema".into());}
    let raw=bytes(v,"intent")?;let parent=parse(&raw)?;let desc=descriptor(&bytes(v,"base_group")?,&raw)?;
    Ok(json!({"profile":PROFILE,"intent_id":hex::encode(desc.intent_id),"base_group":hex::encode(&desc.base_group),
        "leaf_script":hex::encode(desc.leaf_script),"merkle_root":hex::encode(desc.merkle_root),"tweak":hex::encode(desc.tweak),
        "output_key":hex::encode(desc.output_key),"output_parity_odd":desc.output_parity_odd,"script_pubkey":hex::encode(desc.script_pubkey),
        "binding_commitment":hex::encode(desc.binding_commitment),"wallet_context":hex::encode(parent.wallet_context()),
        "input_key":{"version":1,"mode":"WITHDRAWAL_FEE","base_group":hex::encode(desc.base_group),"intent":hex::encode(raw),
            "binding_commitment":hex::encode(desc.binding_commitment),"wallet_context":hex::encode(parent.wallet_context())},"ready":false}))
}
