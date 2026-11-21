// SPDX-License-Identifier: MIT
//! Application transition kernel, not a ZK proof or an authenticated state service.
//! The eventual guest must statically bind the verifier adapter. The EVM must
//! verify these exact journal bytes against its fixed real program/image ID.
pub use satoshifi_checkpoint_policy as checkpoint;
pub use satoshifi_fork_policy as fork;
pub use satoshifi_header_time_policy as time;
mod transition;
pub use transition::*;
