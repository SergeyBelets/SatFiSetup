// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/federation/FederationFactory.sol";
import "../contracts/src/federation/ConfederationFactory.sol";
import "../contracts/src/federation/BitcoinBatchRegistry.sol";

contract RegistryBindingCoordinatorFixture {
    address public constant defaultVerifier = address(0x123);
    address public constant dkgRegistry = address(0x456);
}

contract RegistryBindingAuthorizationFixture {
    address public immutable coordinator;
    constructor(address value) { coordinator = value; }
}

contract CanonicalRegistryBindingTest is Test {
    function testFactoriesRejectDifferentRegistryBeforeAdoption() public {
        address coordinator = address(new RegistryBindingCoordinatorFixture());
        FederationFactory fed = new FederationFactory(address(0x789), address(1), coordinator);
        vm.expectRevert("DKG registry mismatch");
        fed.adoptFederationDKG(bytes32(uint256(1)), "wrong-registry");
        ConfederationFactory conf = new ConfederationFactory(address(fed), address(0x789), address(1), coordinator);
        string[] memory ids = new string[](0);
        IConfederationFactory.CustodianMember[] memory members = new IConfederationFactory.CustodianMember[](0);
        vm.expectRevert("DKG registry mismatch");
        conf.adoptConfederationDKG(bytes32(uint256(1)), ids, members, 2, 1, "wrong-registry");
    }

    function testBitcoinWalletRejectsDifferentRegistry() public {
        address coordinator = address(new RegistryBindingCoordinatorFixture());
        address auth = address(new RegistryBindingAuthorizationFixture(coordinator));
        vm.expectRevert("DKG registry mismatch");
        new BitcoinBatchRegistry(auth, address(0x789), bytes32(uint256(1)));
    }
}
