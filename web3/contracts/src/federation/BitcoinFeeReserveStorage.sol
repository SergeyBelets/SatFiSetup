// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../core/BitcoinTxParser.sol";
import "./BitcoinFeeFundingVerifier.sol";
abstract contract BitcoinFeeReserveStorage {
    uint256 public nonce;
    mapping(bytes32 => bytes32) public receipts;
    uint256 public freeSats;
    uint256 public lockedSats;
    uint256 public promisedSats; // Unbound caps only; bound caps are collateralized by locked UTXOs.
    enum Health { UNKNOWN, HEALTHY, OBSERVATION_UNAVAILABLE, RECOVERY_REQUIRED }
    struct Control { uint256 sequence; uint256 cg; uint256 dg; uint64 deadline; }
    struct Report {
        Control control; uint8 state; uint64 height; bytes32 tip; uint64 validUntil;
        uint256 walletSats; uint256 backing; uint256 pending; uint256 free; bytes32 evidence;
    }
    Report internal report;
    struct Credit { bytes32 txid; uint32 vout; uint256 free; bytes32 root; bytes32 blockHash; }
    Credit[] internal credits;
    mapping(bytes32 => uint256) internal creditIndex;
    struct Allocation { uint256 cap; uint256 maxFee; bool treasury; bool active; bool bound; bytes32 root; }
    mapping(bytes32 => Allocation) public allocations;
    bytes32[] public allocationIds;
    struct Plan {
        bytes32 allocation; bytes32 root; bytes32 previous; bytes32 planHash; bytes32 txid;
        bytes32 inputHash; bytes32 requestHash; uint256 inputFree; uint256 fee; uint256 debit;
        uint256 burned; uint256 cg; uint256 dg; bool reserved; bool settled;
    }
    mapping(bytes32 => Plan) internal plans;
    mapping(bytes32 => bytes32) public transactionPlans;
    mapping(bytes32 => bytes32) public winners;
    mapping(bytes32 => bytes32[]) public familyPlans;
    struct Grant { Control control; bytes32 id; uint256 cap; uint256 maxFee; bool treasury; }
    struct Donation { Control control; BitcoinFeeFundingVerifier.Funding funding; }
    event Reconciled(uint8 indexed state, bytes32 indexed evidence, uint256 cg, uint256 dg);
    event Funded(bytes32 indexed outpoint, uint256 free, bytes32 blockHash);
    event Allocated(bytes32 indexed id, uint256 cap, uint256 maxFee);
    event PreparedPlan(bytes32 indexed batch, bytes32 indexed allocation, bytes32 txid, uint256 fee, uint256 debit);
    event Debited(bytes32 indexed allocation, bytes32 indexed winner, uint256 actualDebit);
    mapping(bytes32 => bytes32[]) internal rootCredits;
    mapping(bytes32 => bytes32[]) internal planCreditKeys;
    mapping(bytes32=>bool) public userLocked;
    mapping(bytes32=>uint256) public userInputFree;
    mapping(bytes32=>bytes32) public userGrant;
    mapping(bytes32=>bytes32) internal userInputHash;

    function _unlock(bytes32 root) internal {
        bytes32[] storage keys=rootCredits[root];
        for(uint256 i;i<keys.length;++i) {
            uint256 at=creditIndex[keys[i]];
            if(at != 0) { Credit storage c=credits[at-1]; require(c.root == root,"fee lock"); lockedSats-=c.free; c.root=0; }
        }
    }
    function _putCredit(bytes32 txid,uint32 vout,uint256 amount,bytes32 blockHash) internal {
        if(amount == 0)return;
        bytes32 key=keccak256(abi.encode(txid,vout)); require(creditIndex[key] == 0,"fee duplicate credit");
        credits.push(Credit(txid,vout,amount,0,blockHash)); creditIndex[key]=credits.length; freeSats+=amount;
        emit Funded(key,amount,blockHash);
    }
    function _deleteCredit(bytes32 key) internal {
        uint256 at=creditIndex[key]; if(at == 0)return;
        Credit memory c=credits[at-1]; freeSats-=c.free;
        Credit memory last=credits[credits.length-1]; credits[at-1]=last;
        creditIndex[keccak256(abi.encode(last.txid,last.vout))]=at; credits.pop(); delete creditIndex[key];
    }
}
