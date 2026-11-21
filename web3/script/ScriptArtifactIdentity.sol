// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Script.sol";

/// @dev Local-script preflight against this checkout's compiled artifacts.
/// Runtime identity is separate from the callers' constructor/getter checks.
abstract contract ScriptArtifactIdentity is Script {
    // Foundry parseJson orders object fields alphabetically: length, then start.
    struct ImmutableReference {
        uint256 length;
        uint256 start;
    }

    function _requireArtifact(address target, string memory artifactName) internal view {
        string memory mismatch = string.concat("runtime mismatch: ", artifactName);
        bytes memory actual = target.code;
        require(actual.length > 0, mismatch);
        string memory artifact = string.concat(artifactName, ".sol:", artifactName);
        bytes memory expected = vm.getDeployedCode(artifact);
        require(actual.length == expected.length, mismatch);

        // Resolve Foundry's configured artifact directory, rather than assuming /out.
        string memory path = vm.getArtifactPathByCode(vm.getCode(artifact));
        string memory json = vm.readFile(path);
        string memory immutablePath = ".deployedBytecode.immutableReferences";
        if (vm.keyExistsJson(json, immutablePath)) {
            string[] memory ids = vm.parseJsonKeys(json, immutablePath);
            for (uint256 i; i < ids.length; ++i) {
                ImmutableReference[] memory references = abi.decode(
                    vm.parseJson(json, string.concat(immutablePath, ".", ids[i])), (ImmutableReference[])
                );
                bytes32 firstWord;
                for (uint256 j; j < references.length; ++j) {
                    uint256 start = references[j].start;
                    require(references[j].length == 32 && start <= actual.length - 32, "invalid artifact immutable reference");
                    bytes32 word;
                    assembly ("memory-safe") { word := mload(add(add(actual, 32), start)) }
                    if (j == 0) firstWord = word;
                    else require(word == firstWord, string.concat("inconsistent artifact immutable: ", artifactName));
                    // Only compiler-declared immutable words are excluded. All
                    // instructions, constants and metadata remain exact matches.
                    assembly ("memory-safe") {
                        mstore(add(add(actual, 32), start), 0)
                        mstore(add(add(expected, 32), start), 0)
                    }
                }
            }
        }
        require(keccak256(actual) == keccak256(expected), mismatch);
    }
}
