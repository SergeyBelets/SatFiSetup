// SPDX-License-Identifier: MIT
//! Same user ELF + default v1compat kernel packaging as risc0-build 3.0.3.
use risc0_binfmt::{compute_image_id, ProgramBinary};
use std::{fs, io::Write};
fn main() {
    let args: Vec<_> = std::env::args_os().collect();
    assert_eq!(args.len(), 3, "usage: satoshifi-guest-image user.elf output.r0bf");
    let user = fs::read(&args[1]).unwrap();
    let program = ProgramBinary::new(&user, risc0_zkos_v1compat::V1COMPAT_ELF).encode();
    let image_id = compute_image_id(&program).expect("invalid program");
    let mut file = fs::OpenOptions::new().write(true).create_new(true).open(&args[2]).unwrap();
    file.write_all(&program).unwrap();
    println!("{image_id}");
}
