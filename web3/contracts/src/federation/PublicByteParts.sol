// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
/// @notice Bounded PUBLIC bytes assembly, with one storage word read per word.
library PublicByteParts {
    function join(bytes[] storage parts,uint256 size) internal view returns(bytes memory raw) {
        // Reserve a full padding word for unaligned part boundaries. Only the
        // exact committed size is exposed; appenders separately bound count/size.
        raw=new bytes(size+32);assembly("memory-safe") { mstore(raw,size) }
        uint256 at;
        for(uint256 i;i<parts.length;++i) {
            bytes storage part=parts[i];uint256 n;
            assembly("memory-safe") {
                let word:=sload(part.slot)
                let destination:=add(add(raw,32),at)
                switch and(word,1)
                case 0 { n:=shr(1,and(word,255)) mstore(destination,and(word,not(255))) }
                default {
                    n:=shr(1,word) mstore(0,part.slot) let base:=keccak256(0,32)
                    for { let offset:=0 } lt(offset,n) { offset:=add(offset,32) } {
                        mstore(add(destination,offset),sload(add(base,shr(5,offset))))
                    }
                }
            }
            at+=n;
        }
        require(at == size,"public parts size");
    }
}
