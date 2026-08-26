// Copyright (c) 2025-present The Bitcoin Core developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://www.opensource.org/licenses/mit-license.php.

#include <addresstype.h>
#include <bench/bench.h>
#include <blockfilter.h>
#include <index/blockfilterindex.h>
#include <interfaces/chain.h>
#include <net_processing.h>
#include <node/context.h>
#include <pubkey.h>
#include <scheduler.h>
#include <script/script.h>
#include <test/util/setup_common.h>
#include <uint256.h>
#include <util/strencodings.h>

#include <memory>
#include <vector>

using namespace util::hex_literals;

namespace {

// Mines a small chain and brings a BASIC block filter index fully in sync
// with it. Returns the synced filter index.
BlockFilterIndex& SetUpSyncedFilterIndex(TestChain100Setup& test_setup)
{
    CPubKey pubkey{"02ed26169896db86ced4cbb7b3ecef9859b5952825adbeab998fb5b307e54949c9"_hex_u8};
    CScript script = GetScriptForDestination(WitnessV0KeyHash(pubkey));
    std::vector<CMutableTransaction> no_txns;
    for (int i = 0; i < 100; ++i) {
        test_setup.CreateAndProcessBlock(no_txns, script);
    }

    Assert(InitBlockFilterIndex([&] { return interfaces::MakeChain(test_setup.m_node); },
                                 BlockFilterType::BASIC, /*n_cache_size=*/0, /*f_memory=*/true, /*f_wipe=*/true));
    BlockFilterIndex* filter_index = Assert(GetBlockFilterIndex(BlockFilterType::BASIC));
    Assert(filter_index->Init());
    Assert(!filter_index->BlockUntilSyncedToCurrentChain());
    filter_index->Sync();
    Assert(filter_index->GetSummary().synced);

    return *filter_index;
}

} // namespace

// Repeatedly rechecks a compact filter request for a block the index has
// already caught up to. Still takes LOCK(cs_main) -- GetSummary() itself is
// lock-free (atomic load), but the "caught up" comparison against
// ActiveChain().Tip() is not -- it just does one Tip() lookup and a hash
// compare under the lock, versus the Racing case's two LookupBlockIndex
// calls plus a GetAncestor walk under the same lock. This isolates the cost
// of *what's done while holding cs_main*, not whether cs_main is held at all
// (both benchmarks below acquire it).
static void CFilterIndexMayBeRacing_NotRacing(benchmark::Bench& bench)
{
    const auto test_setup = MakeNoLogFileContext<TestChain100Setup>();
    BlockFilterIndex& filter_index = SetUpSyncedFilterIndex(*test_setup);
    const uint256 stop_hash = filter_index.GetSummary().best_block_hash;

    bench.minEpochIterations(100'000).run([&] {
        ankerl::nanobench::doNotOptimizeAway(
            test_setup->m_node.peerman->TestOnlyCFilterIndexMayBeRacing(BlockFilterType::BASIC, stop_hash));
    });

    filter_index.Stop();
    DestroyAllBlockFilterIndexes();
}

// Repeatedly rechecks a compact filter request for a block the index has
// NOT caught up to yet -- the state a parked request sits in while it's
// being polled on every ProcessMessages() call. Stopping the scheduler
// before advancing the chain keeps the BlockConnected notification for the
// new tip permanently queued (never serviced), so the index/chain gap -- and
// therefore the cs_main-guarded ancestor-walk path -- is exercised on every
// iteration, deterministically.
static void CFilterIndexMayBeRacing_Racing(benchmark::Bench& bench)
{
    const auto test_setup = MakeNoLogFileContext<TestChain100Setup>();
    BlockFilterIndex& filter_index = SetUpSyncedFilterIndex(*test_setup);

    test_setup->m_node.scheduler->stop();

    CPubKey pubkey{"02ed26169896db86ced4cbb7b3ecef9859b5952825adbeab998fb5b307e54949c9"_hex_u8};
    CScript script = GetScriptForDestination(WitnessV0KeyHash(pubkey));
    const CBlock new_tip = test_setup->CreateAndProcessBlock({}, script);
    const uint256 stop_hash = new_tip.GetHash();
    Assert(filter_index.GetSummary().best_block_hash != stop_hash);

    bench.minEpochIterations(100'000).run([&] {
        ankerl::nanobench::doNotOptimizeAway(
            test_setup->m_node.peerman->TestOnlyCFilterIndexMayBeRacing(BlockFilterType::BASIC, stop_hash));
    });

    Assert(filter_index.GetSummary().best_block_hash != stop_hash);
    filter_index.Stop();
    DestroyAllBlockFilterIndexes();
}

BENCHMARK(CFilterIndexMayBeRacing_NotRacing);
BENCHMARK(CFilterIndexMayBeRacing_Racing);
