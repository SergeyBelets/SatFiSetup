// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "../contracts/src/federation/FundedFeeConverter.sol";

contract FundedConverterFixtureToken is ERC20 {
    constructor() ERC20("Quote fixture", "Q") {}
    function mint(address recipient, uint256 amount) external { _mint(recipient, amount); }
}

contract FundedConverterTest is Test {
    function testRealFundingMinDeadlineAndTreasuryInventory() public {
        _exerciseFundedQuote(31337);
    }

    function testEthereumMainnetFundingSlippageDeadlineAndTreasuryInventory() public {
        _exerciseFundedQuote(1);
    }

    function testSepoliaFundingSlippageDeadlineAndTreasuryInventory() public {
        _exerciseFundedQuote(11155111);
    }

    function testUnsupportedChainRejected() public {
        FundedConverterFixtureToken input = new FundedConverterFixtureToken();
        FundedConverterFixtureToken reward = new FundedConverterFixtureToken();
        vm.chainId(2);
        vm.expectRevert("unsupported chain");
        new FundedFeeConverter(address(input), address(reward), 2, 1);
    }

    function _exerciseFundedQuote(uint256 chain) private {
        vm.chainId(chain);
        FundedConverterFixtureToken input = new FundedConverterFixtureToken();
        FundedConverterFixtureToken reward = new FundedConverterFixtureToken();
        FundedFeeConverter c = new FundedFeeConverter(address(input), address(reward), 2, 1);
        input.mint(address(this), 100);
        input.approve(address(c), 100);
        vm.expectRevert();
        c.convert(10, 20, block.timestamp);
        assertEq(input.balanceOf(address(this)), 100);
        reward.mint(address(c), 200);
        vm.expectRevert("minimum output");
        c.convert(10, 21, block.timestamp);
        vm.warp(100);
        vm.expectRevert("expired or empty");
        c.convert(10, 20, 99);
        assertEq(c.convert(10, 20, 100), 20);
        assertEq(reward.balanceOf(address(this)), 20);
        assertEq(input.balanceOf(address(c)), 10);
        vm.prank(address(0x123));
        vm.expectRevert("treasury inventory");
        c.withdrawLiquidity(address(input), 10);
        c.withdrawLiquidity(address(input), 10);
        assertEq(input.balanceOf(address(this)), 100);
        assertEq(input.balanceOf(address(c)), 0);
    }
}
