// Generated test driver; pinned Core owns all target arithmetic.
#include <arith_uint256.h>
#include <uint256.h>
#include <array>
#include <iomanip>
#include <iostream>
#include <optional>
#include <string>
std::optional<arith_uint256> DeriveTarget(unsigned int nBits, const uint256 pow_limit)
{
    bool fNegative;
    bool fOverflow;
    arith_uint256 bnTarget;

    bnTarget.SetCompact(nBits, &fNegative, &fOverflow);

    // Check range
    if (fNegative || bnTarget == 0 || fOverflow || bnTarget > UintToArith256(pow_limit))
        return {};

    return bnTarget;
}
std::string format_target(arith_uint256 value) {
    constexpr char digits[] = "0123456789abcdef";
    std::string text(64, '0');
    for (int i = 31; i >= 0; --i) {
        unsigned byte = value.GetLow64() & 0xff;
        text[2*i] = digits[byte >> 4];
        text[2*i+1] = digits[byte & 15];
        value >>= 8;
    }
    return text;
}
int main() {
    std::array<arith_uint256, 4> limits;
    limits[0].SetCompact(0x1d00ffff);
    limits[1].SetCompact(0x1e0377ae);
    limits[2].SetCompact(0x207fffff);
    limits[3] = ~arith_uint256(0);
    uint32_t bits;
    while (std::cin >> std::hex >> bits) {
        std::cout << std::hex << std::setw(8) << std::setfill('0') << bits;
        for (const auto& limit : limits) {
            auto target = DeriveTarget(bits, ArithToUint256(limit));
            std::cout << '\t' << (target ? format_target(*target) : "-");
        }
        std::cout << '\n';
    }
    return std::cin.eof() ? 0 : 1;
}
