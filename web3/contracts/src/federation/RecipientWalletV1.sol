// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC1271} from "@openzeppelin/contracts/interfaces/IERC1271.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Address} from "@openzeppelin/contracts/utils/Address.sol";

/// @notice Concrete supported recipient: direct, non-upgradeable, immutable EOA owner.
/// @dev No receive callback is required by MP. No module, delegatecall or owner replacement.
contract RecipientWalletV1 is IERC1271 {
    address public immutable owner;
    constructor(address owner_) {
        require(owner_ != address(0) && owner_.code.length == 0,"wallet owner profile");
        owner=owner_;
    }
    receive() external payable {}
    function execute(address target,uint256 value,bytes calldata data) external returns(bytes memory) {
        require(msg.sender == owner,"wallet owner only");
        return Address.functionCallWithValue(target,data,value);
    }
    function isValidSignature(bytes32 hash,bytes memory signature) external view returns(bytes4) {
        (address recovered,ECDSA.RecoverError error,)=ECDSA.tryRecover(hash,signature);
        return error == ECDSA.RecoverError.NoError && recovered == owner ? IERC1271.isValidSignature.selector : bytes4(0xffffffff);
    }
}
