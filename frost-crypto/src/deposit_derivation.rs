//! Deterministic intent-key derivation using pinned Bitcoin/FROST APIs.
//! This primitive establishes neither canonical registration nor address readiness.
use ::bitcoin::{
    hashes::Hash,
    key::TweakedPublicKey,
    secp256k1::{Parity, Scalar, Secp256k1, XOnlyPublicKey},
    taproot::{LeafVersion, TapNodeHash, TapTweakHash},
    ScriptBuf,
};
use frost_secp256k1_tr::{
    self as frost,
    keys::{EvenY, Tweak},
};
use sha2::{Digest, Sha256};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
pub(crate) const PROFILE: &str = "BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1";
pub(crate) const INTENT_DOMAIN: &[u8] = b"SATOHSI_DEPOSIT_INTENT_V1\0";
pub(crate) const BINDING_DOMAIN: &[u8] = b"SATOHSI_DEPOSIT_BINDING_V1\0";

#[derive(Clone)]
pub(crate) struct DepositIntent {
    pub(crate) protocol_version: u16,
    pub(crate) derivation_version: u16,
    pub(crate) bitcoin_genesis: [u8; 32],
    pub(crate) evm_chain_id: [u8; 32],
    pub(crate) deposit_registry: [u8; 20],
    pub(crate) pool: [u8; 20],
    pub(crate) mp_token: [u8; 20],
    pub(crate) custody_graph_id: [u8; 32],
    pub(crate) dkg_registry: [u8; 20],
    pub(crate) wallet_id: [u8; 32],
    pub(crate) wallet_result: [u8; 32],
    pub(crate) key_epoch: u64,
    pub(crate) requester: [u8; 20],
    pub(crate) recipient: [u8; 20],
    pub(crate) request_nonce: [u8; 32],
    pub(crate) service_policy_hash: [u8; 32],
}

impl DepositIntent {
    /// Same custody/key epoch may span different pool/recipient intents.
    /// This public commitment is not proof of current on-chain provenance.
    pub(crate) fn wallet_context(&self) -> [u8; 32] {
        let mut bytes = b"SATOHSI_DEPOSIT_WALLET_CONTEXT_V1\0".to_vec();
        bytes.extend(self.bitcoin_genesis);
        bytes.extend(self.evm_chain_id);
        bytes.extend(self.deposit_registry);
        bytes.extend(self.custody_graph_id);
        bytes.extend(self.dkg_registry);
        bytes.extend(self.wallet_id);
        bytes.extend(self.wallet_result);
        bytes.extend(self.key_epoch.to_be_bytes());
        Sha256::digest(bytes).into()
    }

    pub(crate) fn from_canonical(raw: &[u8]) -> Result<Self> {
        if raw.len() != 382 || &raw[..26] != INTENT_DOMAIN {
            return Err("intent encoding".into());
        }
        let intent = Self {
            protocol_version: u16::from_be_bytes(raw[26..28].try_into()?),
            derivation_version: u16::from_be_bytes(raw[28..30].try_into()?),
            bitcoin_genesis: raw[30..62].try_into()?,
            evm_chain_id: raw[62..94].try_into()?,
            deposit_registry: raw[94..114].try_into()?,
            pool: raw[114..134].try_into()?,
            mp_token: raw[134..154].try_into()?,
            custody_graph_id: raw[154..186].try_into()?,
            dkg_registry: raw[186..206].try_into()?,
            wallet_id: raw[206..238].try_into()?,
            wallet_result: raw[238..270].try_into()?,
            key_epoch: u64::from_be_bytes(raw[270..278].try_into()?),
            requester: raw[278..298].try_into()?,
            recipient: raw[298..318].try_into()?,
            request_nonce: raw[318..350].try_into()?,
            service_policy_hash: raw[350..382].try_into()?,
        };
        if intent.canonical_bytes()? != raw {
            return Err("noncanonical intent".into());
        }
        Ok(intent)
    }

    pub(crate) fn canonical_bytes(&self) -> Result<Vec<u8>> {
        if self.protocol_version != 1 || self.derivation_version != 1 {
            return Err("qualification profile version".into());
        }
        // These are profile shape constraints, not proof of graph/recipient
        // eligibility. Only a canonical on-chain reader can establish those.
        for value in [
            &self.deposit_registry,
            &self.pool,
            &self.mp_token,
            &self.dkg_registry,
            &self.requester,
            &self.recipient,
        ] {
            if *value == [0; 20] {
                return Err("zero intent address".into());
            }
        }
        for value in [
            &self.bitcoin_genesis,
            &self.evm_chain_id,
            &self.custody_graph_id,
            &self.wallet_id,
            &self.wallet_result,
            &self.request_nonce,
            &self.service_policy_hash,
        ] {
            if *value == [0; 32] {
                return Err("zero intent identifier".into());
            }
        }
        let mut bytes = INTENT_DOMAIN.to_vec();
        bytes.extend(self.protocol_version.to_be_bytes());
        bytes.extend(self.derivation_version.to_be_bytes());
        bytes.extend(self.bitcoin_genesis);
        bytes.extend(self.evm_chain_id);
        bytes.extend(self.deposit_registry);
        bytes.extend(self.pool);
        bytes.extend(self.mp_token);
        bytes.extend(self.custody_graph_id);
        bytes.extend(self.dkg_registry);
        bytes.extend(self.wallet_id);
        bytes.extend(self.wallet_result);
        bytes.extend(self.key_epoch.to_be_bytes());
        bytes.extend(self.requester);
        bytes.extend(self.recipient);
        bytes.extend(self.request_nonce);
        bytes.extend(self.service_policy_hash);
        Ok(bytes)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Descriptor {
    pub(crate) intent_id: [u8; 32],
    pub(crate) base_group: Vec<u8>,
    pub(crate) leaf_script: Vec<u8>,
    pub(crate) merkle_root: [u8; 32],
    pub(crate) tweak: [u8; 32],
    pub(crate) output_key: [u8; 32],
    pub(crate) output_parity_odd: bool,
    pub(crate) script_pubkey: Vec<u8>,
    pub(crate) binding_commitment: [u8; 32],
}

pub(crate) fn checked_public_tweak(
    base: XOnlyPublicKey,
    tweak_bytes: [u8; 32],
) -> Result<(XOnlyPublicKey, Parity)> {
    // Do not use TapTweakHash::to_scalar (panics on >= n), or silently accept
    // FROST's scalar reduction. This existing library API returns an error.
    let scalar = Scalar::from_be_bytes(tweak_bytes)?;
    // Zero is valid BIP341 arithmetic; alias policy is a separate check below.
    Ok(base.add_tweak(&Secp256k1::verification_only(), &scalar)?)
}

pub(crate) fn reject_base_alias(base: XOnlyPublicKey, output: XOnlyPublicKey) -> Result<()> {
    if base == output {
        return Err("profile derived key aliases base".into());
    }
    Ok(())
}

pub(crate) fn descriptor(base_group: &[u8], intent: &DepositIntent) -> Result<Descriptor> {
    descriptor_raw(base_group, &intent.canonical_bytes()?, BINDING_DOMAIN)
}

pub(crate) fn descriptor_raw(base_group: &[u8], intent_bytes: &[u8], binding_domain: &[u8]) -> Result<Descriptor> {
    if base_group.len() != 33 || base_group[0] != 2 {
        return Err("baseline must already be compressed even-Y".into());
    }
    let internal = XOnlyPublicKey::from_slice(&base_group[1..])?;
    let intent_id: [u8; 32] = Sha256::digest(&intent_bytes).into();
    // Exactly one version0xc0 OP_RETURN PUSH32(intentId) leaf, no caller tree.
    // OP_RETURN is not present in the funding transaction: only its P2TR output.
    let leaf = ScriptBuf::new_op_return(intent_id);
    let root = TapNodeHash::from_script(&leaf, LeafVersion::TapScript);
    let tweak = TapTweakHash::from_key_and_tweak(internal, Some(root)).to_byte_array();
    let (output, parity) = checked_public_tweak(internal, tweak)?;
    reject_base_alias(internal, output)?;
    let script = ScriptBuf::new_p2tr_tweaked(TweakedPublicKey::dangerous_assume_tweaked(output));
    let mut binding = binding_domain.to_vec();
    binding.extend_from_slice(intent_bytes);
    binding.extend(base_group);
    binding.extend(leaf.as_bytes());
    binding.extend(root.to_byte_array());
    binding.extend(tweak);
    binding.extend(output.serialize());
    binding.push(u8::from(parity == Parity::Odd));
    binding.extend(script.as_bytes());
    Ok(Descriptor {
        intent_id,
        base_group: base_group.to_vec(),
        leaf_script: leaf.into_bytes(),
        merkle_root: root.to_byte_array(),
        tweak,
        output_key: output.serialize(),
        output_parity_odd: parity == Parity::Odd,
        script_pubkey: script.into_bytes(),
        binding_commitment: Sha256::digest(binding).into(),
    })
}

pub(crate) fn derive_key(
    key: &frost::keys::KeyPackage,
    base: &frost::keys::PublicKeyPackage,
    intent: &DepositIntent,
) -> Result<(Descriptor, frost::keys::KeyPackage)> {
    derive_key_descriptor(key, base, descriptor(&base.verifying_key().serialize()?, intent)?)
}

pub(crate) fn derive_key_descriptor(key: &frost::keys::KeyPackage, base: &frost::keys::PublicKeyPackage, desc: Descriptor)
    -> Result<(Descriptor, frost::keys::KeyPackage)> {
    if !key.has_even_y()
        || key.verifying_key() != base.verifying_key()
        || base.verifying_shares().get(key.identifier()) != Some(key.verifying_share())
        || frost::keys::VerifyingShare::from(*key.signing_share()) != *key.verifying_share()
    {
        return Err("base key/public package mismatch".into());
    }
    let (descriptor, public) = derive_public_descriptor(base, desc)?;
    let derived = key
        .clone()
        .tweak(Some(&descriptor.merkle_root))
        .into_even_y(None);
    derived.serialize()?;
    if derived.verifying_key() != public.verifying_key()
        || public.verifying_shares().get(derived.identifier()) != Some(derived.verifying_share())
        || frost::keys::VerifyingShare::from(*derived.signing_share()) != *derived.verifying_share()
    {
        return Err("derived key/public package mismatch".into());
    }
    Ok((descriptor, derived))
}

pub(crate) fn derive_public(
    base: &frost::keys::PublicKeyPackage,
    intent: &DepositIntent,
) -> Result<(Descriptor, frost::keys::PublicKeyPackage)> {
    derive_public_descriptor(base, descriptor(&base.verifying_key().serialize()?, intent)?)
}

pub(crate) fn derive_public_descriptor(base: &frost::keys::PublicKeyPackage, desc: Descriptor)
    -> Result<(Descriptor, frost::keys::PublicKeyPackage)> {
    if !base.has_even_y() {
        return Err("baseline must already be even-Y".into());
    }
    base.serialize()?;
    if desc.base_group != base.verifying_key().serialize()? { return Err("descriptor base mismatch".into()); }
    let tweaked = base.clone().tweak(Some(&desc.merkle_root));
    let point = tweaked.verifying_key().serialize()?;
    if point[1..] != desc.output_key || (point[0] == 3) != desc.output_parity_odd {
        return Err("FROST/libsecp derivation mismatch".into());
    }
    let public = tweaked.into_even_y(None);
    public.serialize()?;
    Ok((desc, public))
}
