// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {SimpleMultiPoolDeployer} from "../contracts/src/federation/SimpleMultiPoolDeployer.sol";
import {MultiPoolDeployer} from "../contracts/src/federation/MultiPoolDeployer.sol";
import {DKGCoordinator} from "../contracts/src/federation/DKGCoordinator.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {MultiPoolShareTokenDeployer} from "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";
import {ConfederationTokenDeployer} from "../contracts/src/federation/ConfederationTokenDeployer.sol";
import {SimpleMultiPool} from "../contracts/src/demo/SimpleMultiPool.sol";
import {ConfederationMultiPool} from "../contracts/src/federation/ConfederationMultiPool.sol";
import {MultiPoolShareToken} from "../contracts/src/tokens/MultiPoolShareToken.sol";
import {ConfederationToken} from "../contracts/src/tokens/ConfederationToken.sol";
import "./ValueFlowTestFixtures.sol";

contract PoolInitcodeLimitsTest is Test {
    uint256 constant MAX_INITCODE = 49_152;
    uint256 constant MAX_RUNTIME = 24_576;

    function _text(uint256 length, bytes1 value) internal pure returns (string memory) {
        bytes memory text = new bytes(length);
        for (uint256 i; i < length; i++) text[i] = value;
        return string(text);
    }

    function _participants() internal returns (address[] memory participants) {
        // Use the actual V1 DKG maximum, including this complete roster in the
        // authorization deployment performed by the simple-pool deployer.
        uint256 count = new DKGCoordinator().MAX_PARTICIPANTS();
        participants = new address[](count);
        for (uint256 i; i < count; i++) participants[i] = address(uint160(0x1000 + i));
    }

    function _assertBoundary(bytes memory payload) internal pure {
        require(payload.length <= MAX_INITCODE, "oversized supported payload");
        require(payload.length + 32 > MAX_INITCODE, "not the largest padded payload");
    }

    function test_SimpleLargestPoolIdDeploysThroughRealDeployerWithMaximumRoster() public {
        ValueFlowCoordinator coordinator = new ValueFlowCoordinator(address(this));
        FROSTAuthorizationDeployer frostDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer tokenDeployer = new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer deployer = new SimpleMultiPoolDeployer(address(frostDeployer), address(tokenDeployer));
        frostDeployer.setAuthorizedDeployer(address(deployer), true);
        tokenDeployer.setAuthorizedDeployer(address(deployer), true);
        address[] memory participants = _participants();
        uint256 maxIdLength = (MAX_INITCODE - type(SimpleMultiPool).creationCode.length - 128) / 32 * 32;
        string memory id = _text(maxIdLength, 0x69);
        address expected = deployer.predictNextMultiPoolAddress();
        address predictedAuth = vm.computeCreateAddress(address(frostDeployer), vm.getNonce(address(frostDeployer)));
        address predictedToken = vm.computeCreateAddress(address(tokenDeployer), vm.getNonce(address(tokenDeployer)));
        bytes memory payload = abi.encodePacked(type(SimpleMultiPool).creationCode, abi.encode(id, predictedAuth, predictedToken));
        _assertBoundary(payload);
        emit log_named_uint("Simple pool maximum ID bytes", maxIdLength);
        emit log_named_uint("Simple pool complete initcode bytes", payload.length);
        uint256 initialNonce = deployer.nextMultiPoolNonce();
        assertEq(initialNonce, vm.getNonce(address(deployer)));
        (address pool, address auth, address share) = deployer.deploySimpleMultiPool(
            id, participants, participants.length, "Max ID Share", "MIS", address(coordinator), abi.encodePacked(bytes32(uint256(1))));
        assertEq(pool, expected);
        assertEq(auth, predictedAuth);
        assertEq(share, predictedToken);
        assertEq(deployer.nextMultiPoolNonce(), initialNonce + 1);
        assertEq(bytes(SimpleMultiPool(pool).multiPoolId()).length, maxIdLength);
        assertEq(FROSTAuthorization(auth).getParticipants().length, participants.length);
        MultiPoolShareToken token = MultiPoolShareToken(share);
        assertEq(token.multiPool(), pool);
        assertTrue(token.hasRole(token.MINTER_ROLE(), pool));
        assertTrue(token.hasRole(token.BURNER_ROLE(), pool));
        assertFalse(token.hasRole(token.DEFAULT_ADMIN_ROLE(), address(deployer)));
        assertLe(pool.code.length, MAX_RUNTIME);
        assertLe(address(deployer).code.length, MAX_RUNTIME);
        assertLe(auth.code.length, MAX_RUNTIME);
        // The next successful create must still use the correctly advanced nonce.
        expected = deployer.predictNextMultiPoolAddress();
        (address next,, address nextToken) = deployer.deploySimpleMultiPool(
            "next", participants, participants.length, "Next", "N", address(coordinator), abi.encodePacked(bytes32(uint256(1))));
        assertEq(next, expected);
        assertEq(MultiPoolShareToken(nextToken).multiPool(), expected);
        assertEq(deployer.nextMultiPoolNonce(), initialNonce + 2);
    }

    function test_ConfederationLargestPoolIdDeploysThroughRealDeployer() public {
        ValueFlowCoordinator coordinator = new ValueFlowCoordinator(address(this));
        address[] memory participants = _participants();
        FROSTAuthorization authorization = new FROSTAuthorization(
            address(coordinator), abi.encodePacked(bytes32(uint256(1))), participants, participants.length);
        CustodianValidator validator = new CustodianValidator(participants, participants.length, address(authorization));
        ConfederationTokenDeployer tokenDeployer = new ConfederationTokenDeployer();
        MultiPoolDeployer deployer = new MultiPoolDeployer(address(tokenDeployer));
        tokenDeployer.setAuthorizedDeployer(address(deployer), true);
        uint256 maxIdLength = (MAX_INITCODE - type(ConfederationMultiPool).creationCode.length - 160) / 32 * 32;
        string memory id = _text(maxIdLength, 0x63);
        address expected = deployer.predictNextMultiPoolAddress();
        address predictedToken = vm.computeCreateAddress(address(tokenDeployer), vm.getNonce(address(tokenDeployer)));
        bytes memory payload = abi.encodePacked(type(ConfederationMultiPool).creationCode,
            abi.encode(id, address(authorization), address(validator), predictedToken));
        _assertBoundary(payload);
        emit log_named_uint("Confederation pool maximum ID bytes", maxIdLength);
        emit log_named_uint("Confederation pool complete initcode bytes", payload.length);
        (address pool, address share) = deployer.deployMultiPool(id, address(authorization), address(validator), "Max ID Share", "MIS");
        assertEq(pool, expected);
        assertEq(share, predictedToken);
        assertEq(deployer.nextMultiPoolNonce(), 2);
        assertEq(bytes(ConfederationMultiPool(pool).confederationId()).length, maxIdLength);
        ConfederationToken token = ConfederationToken(share);
        assertEq(token.confederationPool(), pool);
        assertTrue(token.hasRole(token.MINTER_ROLE(), pool));
        assertTrue(token.hasRole(token.BURNER_ROLE(), pool));
        assertFalse(token.hasRole(token.DEFAULT_ADMIN_ROLE(), address(deployer)));
        assertLe(pool.code.length, MAX_RUNTIME);
        assertLe(address(deployer).code.length, MAX_RUNTIME);
        expected = deployer.predictNextMultiPoolAddress();
        (address next, address nextToken) = deployer.deployMultiPool("next", address(authorization), address(validator), "Next", "N");
        assertEq(next, expected);
        assertEq(ConfederationToken(nextToken).confederationPool(), expected);
        assertEq(deployer.nextMultiPoolNonce(), 3);
    }

    function test_ShareTokensLargestJointNameSymbolPayloadsDeploy() public {
        uint256 budget = (MAX_INITCODE - type(MultiPoolShareToken).creationCode.length - 160) / 32 * 32;
        uint256 nameLength = budget / 64 * 32;
        string memory name = _text(nameLength, 0x4e);
        string memory symbol = _text(budget - nameLength, 0x53);
        bytes memory payload = abi.encodePacked(type(MultiPoolShareToken).creationCode, abi.encode(name, symbol, address(this)));
        _assertBoundary(payload);
        MultiPoolShareToken simple = new MultiPoolShareToken(name, symbol, address(this));
        assertEq(bytes(simple.name()).length + bytes(simple.symbol()).length, budget);
        assertLe(address(simple).code.length, MAX_RUNTIME);
        emit log_named_uint("Simple share joint dynamic padded bytes", budget);
        emit log_named_uint("Simple share complete initcode bytes", payload.length);

        budget = (MAX_INITCODE - type(ConfederationToken).creationCode.length - 160) / 32 * 32;
        nameLength = budget / 64 * 32;
        name = _text(nameLength, 0x4e);
        symbol = _text(budget - nameLength, 0x53);
        payload = abi.encodePacked(type(ConfederationToken).creationCode, abi.encode(name, symbol, address(this)));
        _assertBoundary(payload);
        ConfederationToken confederation = new ConfederationToken(name, symbol, address(this));
        assertEq(bytes(confederation.name()).length + bytes(confederation.symbol()).length, budget);
        assertLe(address(confederation).code.length, MAX_RUNTIME);
        emit log_named_uint("Confederation share joint dynamic padded bytes", budget);
        emit log_named_uint("Confederation share complete initcode bytes", payload.length);
    }
}
