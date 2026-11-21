// SPDX-License-Identifier: MIT
//! Separate V2 statement for bounded resume. This crate does not authenticate a
//! proof. A guest must statically supply the pinned consensus adapter; the EVM
//! must authenticate the exact output bytes with a fixed real program/image ID.
pub use satoshifi_resume_policy as bounded;
mod transition;
mod input;
pub use transition::*;
pub use input::*;
