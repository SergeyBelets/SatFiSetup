The `vendor/` files are unchanged excerpts from the exact commits recorded in
`ops/artifacts/zkspv/upstream-lock.json`. They are retained for reproducible review,
not wired into a deployed payment path.

* Citrea: Chainway Labs and original contributors. `COPYING`, per-directory
  `LICENSE`, package declarations and source copyright notices are preserved.
  In particular, workspace GPL-3.0-only dependencies are not relicensed by the
  separate bitcoin-da package declaration.
* Bitcoin Core: the Bitcoin Core developers; MIT, with `COPYING` retained. These
  files are comparison references, not a substitute implementation.
* RISC Zero Ethereum: RISC Zero, Inc. and contributors. Root Apache-2.0 license
  and source-specific GPL-3.0 verifier notices are preserved separately.

The generated arithmetic probe contains unchanged code from Citrea bitcoin-da
under its Apache-2.0 option. The probe imports crypto-bigint 0.5.5 under its
declared Apache-2.0 OR MIT license. No license assertion here grants rights in
other code or resolves deployment/distribution compatibility for the project.
