#!/usr/bin/env python3
# Copyright (c) 2025-present The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""AttemptParkedRequest() is retried on every ProcessMessages() pass for a peer
with a pending request (net_processing.cpp) -- not gated on new traffic from
that peer. So N parked peers means N extra cs_main acquisitions on every
message-handler sweep, independent of anything the peers actually send.
That's an aggregate-acquisition-rate story, not a single-call-latency one --
see the added_cs_main_acquisitions_per_sec figure this script logs per
trial. The ping RTT figures are the "does this rate actually cause
observable harm" check on top of that, measured against a control group of
peers sending an ordinary, immediately-resolved request (getdata for a tx)
instead, to isolate the cost of *parking* from the cost of merely having N
more active connections sending N more messages.

Not registered in test_runner.py -- run directly:
    build/test/functional/cfilter_park_retry_rate_bench.py
Requires a bitcoind built with the -testfilterindexdelay debug hook.
"""

import time

from test_framework.messages import (
    FILTER_TYPE_BASIC,
    MSG_TX,
    CInv,
    msg_getcfilters,
    msg_getdata,
    msg_ping,
)
from test_framework.p2p import P2PInterface
from test_framework.test_framework import BitcoinTestFramework

FILTER_INDEX_DELAY_MS = 200
PEER_COUNTS = [0, 1, 10, 50, 100]
PING_SAMPLES_PER_TRIAL = 20
# ThreadMessageHandler's idle sleep is 100ms (net.cpp), so an otherwise-idle
# node retries each parked peer's request at up to this rate.
IDLE_SWEEP_HZ = 10


class RacingPeer(P2PInterface):
    """Sends one getcfilters request per new tip, engineered to race the
    (artificially slowed) filter index and stay parked."""

    def send_request(self, ctx):
        self.send_without_ping(msg_getcfilters(
            filter_type=FILTER_TYPE_BASIC,
            start_height=0,
            stop_hash=int(ctx["block_hash"], 16),
        ))


class OrdinaryPeer(P2PInterface):
    """Control group: sends one ordinary getdata for the just-mined
    coinbase tx. Same shape (one connected peer, one message) as
    RacingPeer, but resolves immediately -- nothing about it parks."""

    def send_request(self, ctx):
        self.send_without_ping(msg_getdata([CInv(MSG_TX, int(ctx["coinbase_txid"], 16))]))


class CFilterParkBench(BitcoinTestFramework):
    def set_test_params(self):
        self.setup_clean_chain = True
        self.num_nodes = 1
        self.extra_args = [[
            "-blockfilterindex=1",
            "-peerblockfilters=1",
            f"-testfilterindexdelay={FILTER_INDEX_DELAY_MS}",
            "-maxconnections=1000",
        ]]

    def measure_control_ping(self, control: P2PInterface, samples: int) -> list:
        rtts = []
        for _ in range(samples):
            control.ping_counter += 1
            nonce = control.ping_counter
            start = time.time()
            control.send_without_ping(msg_ping(nonce=nonce))

            def got_pong():
                return control.last_message.get("pong") and control.last_message["pong"].nonce == nonce
            control.wait_until(got_pong, timeout=60)
            rtts.append(time.time() - start)
        return rtts

    def run_trial(self, node, control, peer_class, n: int) -> tuple:
        peers = []
        for _ in range(n):
            try:
                peers.append(node.add_p2p_connection(peer_class()))
            except Exception as e:
                self.log.warning(f"{peer_class.__name__} n={n}: connection setup failed ({e}), continuing with {len(peers)}")
                break

        new_hash = self.generate(node, 1, sync_fun=self.no_op)[0]
        coinbase_txid = node.getblock(new_hash, 1)["tx"][0]
        ctx = {"block_hash": new_hash, "coinbase_txid": coinbase_txid}

        sent = 0
        for peer in peers:
            if peer.is_connected:
                peer.send_request(ctx)
                sent += 1

        rtts = self.measure_control_ping(control, PING_SAMPLES_PER_TRIAL)
        avg_ms = 1000 * sum(rtts) / len(rtts)
        max_ms = 1000 * max(rtts)

        for peer in peers:
            if peer.is_connected:
                peer.peer_disconnect()
        for peer in peers:
            if peer.is_connected:
                peer.wait_for_disconnect()

        # Let the artificially-delayed index finish this round's block
        # before starting the next trial.
        time.sleep(FILTER_INDEX_DELAY_MS / 1000 + 0.5)

        return avg_ms, max_ms, sent

    def run_test(self):
        node = self.nodes[0]
        self.generate(node, 100)
        node.syncwithvalidationinterfacequeue()

        control = node.add_p2p_connection(P2PInterface())

        self.log.info(
            f"{'n':>4}  {'+cs_main acq/s':>15}  {'racing avg':>11}  {'racing max':>11}"
            f"  {'control avg':>12}  {'control max':>12}"
        )
        for n in PEER_COUNTS:
            r_avg, r_max, r_sent = self.run_trial(node, control, RacingPeer, n)
            c_avg, c_max, c_sent = self.run_trial(node, control, OrdinaryPeer, n)
            added_acq_per_sec = n * IDLE_SWEEP_HZ
            self.log.info(
                f"{n:4d}  {added_acq_per_sec:15d}  {r_avg:9.3f}ms  {r_max:9.3f}ms"
                f"  {c_avg:10.3f}ms  {c_max:10.3f}ms"
                f"  (racing sent={r_sent}, control sent={c_sent})"
            )


if __name__ == '__main__':
    CFilterParkBench(__file__).main()
