// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
}

interface IDemoStockToken {
    function mint(address to, uint256 amount) external;
    function burn(address from, uint256 amount) external;
}

/// @title DemoExchange - testnet-only fixed-price exchange for AutoWallet's demo stock tokens.
/// @notice Swaps tUSDG (6 decimals) for DemoStockTokens (18 decimals) at a price the
///         owner posts from real Chainlink feeds on Robinhood Chain mainnet. Buys mint
///         shares; sells burn them and pay tUSDG from this contract's reserve.
///         Prices older than maxPriceAge are rejected so trades can't use stale quotes.
contract DemoExchange {
    IERC20 public immutable usdg;
    address public owner;
    uint256 public maxPriceAge = 1 hours;

    /// @dev price = tUSDG base units (1e-6 USD) per 1 whole share
    mapping(address => uint256) public price;
    mapping(address => uint256) public priceUpdatedAt;
    mapping(address => bool) public listed;

    event PriceUpdated(address indexed token, uint256 price);
    event Trade(address indexed trader, address indexed token, bool isBuy, uint256 usdgAmount, uint256 shares, uint256 price);

    constructor(address usdg_) {
        usdg = IERC20(usdg_);
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "only owner");
        _;
    }

    function list(address token) external onlyOwner {
        listed[token] = true;
    }

    function setPrices(address[] calldata tokens, uint256[] calldata prices) external onlyOwner {
        require(tokens.length == prices.length, "length");
        for (uint256 i = 0; i < tokens.length; i++) {
            require(listed[tokens[i]] && prices[i] > 0, "bad price");
            price[tokens[i]] = prices[i];
            priceUpdatedAt[tokens[i]] = block.timestamp;
            emit PriceUpdated(tokens[i], prices[i]);
        }
    }

    function setMaxPriceAge(uint256 seconds_) external onlyOwner {
        maxPriceAge = seconds_;
    }

    function _freshPrice(address token) internal view returns (uint256 p) {
        require(listed[token], "not listed");
        p = price[token];
        require(p > 0 && block.timestamp - priceUpdatedAt[token] <= maxPriceAge, "stale price");
    }

    /// @notice Spend `usdgIn` tUSDG for shares of `token`. Requires prior tUSDG approval.
    function buy(address token, uint256 usdgIn, uint256 minSharesOut) external returns (uint256 shares) {
        uint256 p = _freshPrice(token);
        shares = (usdgIn * 1e18) / p;
        require(shares > 0 && shares >= minSharesOut, "slippage");
        require(usdg.transferFrom(msg.sender, address(this), usdgIn), "usdg transfer");
        IDemoStockToken(token).mint(msg.sender, shares);
        emit Trade(msg.sender, token, true, usdgIn, shares, p);
    }

    /// @notice Sell `sharesIn` of `token` for tUSDG from the reserve.
    function sell(address token, uint256 sharesIn, uint256 minUsdgOut) external returns (uint256 usdgOut) {
        uint256 p = _freshPrice(token);
        usdgOut = (sharesIn * p) / 1e18;
        require(usdgOut > 0 && usdgOut >= minUsdgOut, "slippage");
        IDemoStockToken(token).burn(msg.sender, sharesIn);
        require(usdg.transfer(msg.sender, usdgOut), "usdg transfer");
        emit Trade(msg.sender, token, false, usdgOut, sharesIn, p);
    }
}
