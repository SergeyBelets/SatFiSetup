// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/**
 * @title Secp256k1
 * @notice Elliptic curve operations on secp256k1 (Bitcoin/Ethereum curve)
 * @dev Implements point addition, scalar multiplication, and field operations
 */
library Secp256k1 {
    // ======== CONSTANTS ========

    /// @notice Prime field modulus (2^256 - 2^32 - 977)
    uint256 constant P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F;

    /// @notice Curve order (number of points on the curve)
    uint256 constant N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    /// @notice Generator point G (uncompressed)
    uint256 constant GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798;
    uint256 constant GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8;

    /// @notice Curve parameter a (for y^2 = x^3 + ax + b)
    uint256 constant A = 0;

    /// @notice Curve parameter b
    uint256 constant B = 7;

    // ======== STRUCTS ========

    struct Point {
        uint256 x;
        uint256 y;
    }

    // ======== ERRORS ========

    error InvalidPoint();
    error PointAtInfinity();
    error InvalidScalar();

    // ======== FIELD OPERATIONS ========

    /**
     * @notice Modular addition
     * @param a First operand
     * @param b Second operand
     * @return result (a + b) mod P
     */
    function addMod(uint256 a, uint256 b) internal pure returns (uint256) {
        return addmod(a, b, P);
    }

    /**
     * @notice Modular subtraction
     * @param a First operand
     * @param b Second operand
     * @return result (a - b) mod P
     */
    function subMod(uint256 a, uint256 b) internal pure returns (uint256) {
        return addmod(a, P - b, P);
    }

    /**
     * @notice Modular multiplication
     * @param a First operand
     * @param b Second operand
     * @return result (a * b) mod P
     */
    function mulMod(uint256 a, uint256 b) internal pure returns (uint256) {
        return mulmod(a, b, P);
    }

    /**
     * @notice Modular inversion using Fermat's little theorem
     * @param a Value to invert
     * @return result a^(-1) mod P
     * @dev Uses: a^(-1) = a^(P-2) mod P
     */
    function invMod(uint256 a) internal view returns (uint256) {
        return expMod(a, P - 2, P);
    }

    /**
     * @notice Modular exponentiation
     * @param base Base value
     * @param exponent Exponent
     * @param modulus Modulus
     * @return result base^exponent mod modulus
     */
    function expMod(uint256 base, uint256 exponent, uint256 modulus) internal view returns (uint256 result) {
        assembly {
            let ptr := mload(0x40)
            mstore(ptr, 0x20)             // Length of base
            mstore(add(ptr, 0x20), 0x20)  // Length of exponent
            mstore(add(ptr, 0x40), 0x20)  // Length of modulus
            mstore(add(ptr, 0x60), base)
            mstore(add(ptr, 0x80), exponent)
            mstore(add(ptr, 0xa0), modulus)

            // Call precompiled contract 0x05 (modexp)
            if iszero(staticcall(gas(), 0x05, ptr, 0xc0, ptr, 0x20)) {
                revert(0, 0)
            }

            result := mload(ptr)
        }
    }

    // ======== POINT OPERATIONS ========

    /**
     * @notice Check if point is on the curve
     * @param point Point to check
     * @return valid True if point is on curve: y^2 = x^3 + 7 (mod P)
     */
    function isOnCurve(Point memory point) internal pure returns (bool) {
        if (point.x >= P || point.y >= P) return false;
        if (point.x == 0 && point.y == 0) return true; // Point at infinity

        uint256 left = mulmod(point.y, point.y, P);
        uint256 right = addmod(
            mulmod(mulmod(point.x, point.x, P), point.x, P),
            B,
            P
        );

        return left == right;
    }

    /**
     * @notice Get generator point G
     * @return g Generator point
     */
    function generator() internal pure returns (Point memory) {
        return Point(GX, GY);
    }

    /**
     * @notice Check if point is infinity (identity element)
     * @param point Point to check
     * @return isInfinity True if point is at infinity
     */
    function isInfinity(Point memory point) internal pure returns (bool) {
        return point.x == 0 && point.y == 0;
    }

    /**
     * @notice Negate a point (x, y) -> (x, -y)
     * @param point Point to negate
     * @return neg Negated point
     */
    function negate(Point memory point) internal pure returns (Point memory) {
        if (isInfinity(point)) return point;
        return Point(point.x, P - point.y);
    }

    /**
     * @notice Point addition using EVM precompile
     * @param p1 First point
     * @param p2 Second point
     * @return result p1 + p2
     * @dev Uses precompiled contract at 0x06 (bn256Add adapted for secp256k1)
     */
    function addPoints(Point memory p1, Point memory p2) internal view returns (Point memory result) {
        // Handle infinity cases
        if (isInfinity(p1)) return p2;
        if (isInfinity(p2)) return p1;

        // If points are equal, use doubling
        if (p1.x == p2.x && p1.y == p2.y) {
            return double(p1);
        }

        // If x coordinates equal but y different, result is infinity
        if (p1.x == p2.x) {
            return Point(0, 0);
        }

        // Compute lambda = (y2 - y1) / (x2 - x1)
        uint256 numerator = subMod(p2.y, p1.y);
        uint256 denominator = subMod(p2.x, p1.x);
        uint256 lambda = mulMod(numerator, invMod(denominator));

        // x3 = lambda^2 - x1 - x2
        uint256 x3 = subMod(subMod(mulMod(lambda, lambda), p1.x), p2.x);

        // y3 = lambda * (x1 - x3) - y1
        uint256 y3 = subMod(mulMod(lambda, subMod(p1.x, x3)), p1.y);

        result = Point(x3, y3);

        if (!isOnCurve(result)) revert InvalidPoint();
    }

    /**
     * @notice Point doubling: 2 * P
     * @param point Point to double
     * @return result 2 * point
     */
    function double(Point memory point) internal view returns (Point memory result) {
        if (isInfinity(point)) return point;

        // Compute lambda = (3 * x^2 + a) / (2 * y)
        // For secp256k1, a = 0, so lambda = (3 * x^2) / (2 * y)
        uint256 numerator = mulMod(3, mulMod(point.x, point.x));
        uint256 denominator = mulMod(2, point.y);
        uint256 lambda = mulMod(numerator, invMod(denominator));

        // x3 = lambda^2 - 2*x
        uint256 x3 = subMod(mulMod(lambda, lambda), mulMod(2, point.x));

        // y3 = lambda * (x - x3) - y
        uint256 y3 = subMod(mulMod(lambda, subMod(point.x, x3)), point.y);

        result = Point(x3, y3);

        if (!isOnCurve(result)) revert InvalidPoint();
    }

    /**
     * @notice Scalar multiplication: k * P
     * @param scalar Scalar value
     * @param point Point to multiply
     * @return result scalar * point
     * @dev Uses double-and-add algorithm
     */
    function scalarMul(uint256 scalar, Point memory point) internal view returns (Point memory result) {
        if (scalar == 0 || isInfinity(point)) {
            return Point(0, 0);
        }

        if (scalar >= N) {
            scalar = scalar % N;
        }

        result = Point(0, 0); // Start with infinity
        Point memory temp = point;

        while (scalar > 0) {
            if (scalar & 1 == 1) {
                result = addPoints(result, temp);
            }
            temp = double(temp);
            scalar >>= 1;
        }
    }

    /**
     * @notice Scalar multiplication by generator: k * G
     * @param scalar Scalar value
     * @return result scalar * G
     */
    function scalarBaseMul(uint256 scalar) internal view returns (Point memory) {
        return scalarMul(scalar, generator());
    }

    /**
     * @notice Multi-scalar multiplication: k1*P1 + k2*P2
     * @param k1 First scalar
     * @param p1 First point
     * @param k2 Second scalar
     * @param p2 Second point
     * @return result k1*P1 + k2*P2
     */
    function multiScalarMul(
        uint256 k1,
        Point memory p1,
        uint256 k2,
        Point memory p2
    ) internal view returns (Point memory) {
        Point memory r1 = scalarMul(k1, p1);
        Point memory r2 = scalarMul(k2, p2);
        return addPoints(r1, r2);
    }

    // ======== ENCODING/DECODING ========

    /**
     * @notice Encode point to compressed format (33 bytes)
     * @param point Point to encode
     * @return compressed 0x02 + x (if y even) or 0x03 + x (if y odd)
     */
    function compress(Point memory point) internal pure returns (bytes memory) {
        if (isInfinity(point)) {
            return abi.encodePacked(uint8(0x00));
        }

        uint8 prefix = (point.y & 1 == 0) ? 0x02 : 0x03;
        return abi.encodePacked(prefix, point.x);
    }

    /**
     * @notice Decompress point from compressed format
     * @param compressed Compressed point (33 bytes)
     * @return point Decompressed point
     */
    function decompress(bytes memory compressed) internal view returns (Point memory point) {
        require(compressed.length == 33, "Invalid compressed point length");

        uint8 prefix = uint8(compressed[0]);
        require(prefix == 0x02 || prefix == 0x03, "Invalid prefix");

        uint256 x;
        assembly {
            x := mload(add(compressed, 33))
        }

        // Compute y^2 = x^3 + 7
        uint256 ySquared = addmod(mulmod(mulmod(x, x, P), x, P), B, P);

        // Compute y = sqrt(ySquared)
        uint256 y = expMod(ySquared, (P + 1) / 4, P);

        // Check if we need to negate y
        bool yParity = (y & 1 == 1);
        bool expectedParity = (prefix == 0x03);

        if (yParity != expectedParity) {
            y = P - y;
        }

        point = Point(x, y);
        require(isOnCurve(point), "Point not on curve");
    }

    /**
     * @notice Hash to scalar (for challenges, nonces)
     * @param data Data to hash
     * @return scalar Hash result mod N
     */
    function hashToScalar(bytes memory data) internal pure returns (uint256) {
        return uint256(keccak256(data)) % N;
    }

    /**
     * @notice Check if two points are equal
     * @param p1 First point
     * @param p2 Second point
     * @return equal True if points are equal
     */
    function equals(Point memory p1, Point memory p2) internal pure returns (bool) {
        return p1.x == p2.x && p1.y == p2.y;
    }
}
