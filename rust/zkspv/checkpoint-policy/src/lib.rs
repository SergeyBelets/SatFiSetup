// SPDX-License-Identifier: MIT OR Apache-2.0
//! Checkpoint component; not an activated guest or authenticated state contract.
//! The supplied pre-anchor cumulative work remains a trusted deployment input.
pub use satoshifi_fork_policy as fork;
mod compact;
mod checkpoint;
pub use checkpoint::*;
