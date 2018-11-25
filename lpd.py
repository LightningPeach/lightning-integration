from binascii import hexlify
from utils import TailableProc
from ephemeral_port_reserve import reserve

import src.lpd.python_binding.common_pb2 as common_pb2
import src.lpd.python_binding.channel_pb2 as channel_pb2
import src.lpd.python_binding.payment_pb2 as payment_pb2
import src.lpd.python_binding.routing_pb2 as routing_pb2
from src.lpd.python_binding.channel_pb2_grpc import ChannelServiceStub
from src.lpd.python_binding.payment_pb2_grpc import PaymentServiceStub
from src.lpd.python_binding.routing_pb2_grpc import RoutingServiceStub

import grpc
import logging
import os
import time
import codecs


class LpdD(TailableProc):

    def __init__(self, lightning_dir, bitcoind, port):
        super().__init__(lightning_dir, 'lpd({})'.format(port))
        self.lightning_dir = lightning_dir
        self.bitcoind = bitcoind
        self.port = port
        self.rpc_port = str(reserve())
        self.prefix = 'lpd'

        self.cmd_line = [
            'bin/lpd',
            '--rpclisten=127.0.0.1:{}'.format(self.rpc_port),
        ]

        if not os.path.exists(lightning_dir):
            os.makedirs(lightning_dir)

    def start(self):
        super().start()
        self.wait_for_log('RPC server listening on')
        self.wait_for_log('Done catching up block hashes')
        time.sleep(5)

        logging.info('LPD started (pid: {})'.format(self.proc.pid))

    def stop(self):
        self.proc.terminate()
        time.sleep(3)
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        super().save_log()


class LpdNode(object):

    displayName = 'lpd'

    def __init__(self, lightning_dir, lightning_port, bitcoind, executor=None, node_id=0):
        self.bitcoin = bitcoind
        self.executor = executor
        self.daemon = LpdD(lightning_dir, bitcoind, port=lightning_port)
        self.rpc = LpdRpc(self.daemon.rpc_port)
        self.logger = logging.getLogger('lpd-node({})'.format(lightning_port))
        self.myid = None
        self.node_id = node_id

    def id(self):
        if not self.myid:
            self.myid = self.info()['id']
        return self.myid

    def ping(self):
        """ Simple liveness test to see if the node is up and running

        Returns true if the node is reachable via RPC, false otherwise.
        """
        try:
            self.rpc.routing.GetInfo(common_pb2.Void())
            return True
        except Exception as e:
            print(e)
            return False

    def peers(self):
        peers = self.rpc.routing.ListPeers(common_pb2.Void()).peers
        return [p.pub_key for p in peers]

    def check_channel(self, remote):
        """ Make sure that we have an active channel with remote
        """
        self_id = self.id()
        remote_id = remote.id()
        channels = self.rpc.channel.List(channel_pb2.ChannelFilter()).channels
        channel_by_remote = {c.remote_pubkey: c for c in channels}
        if remote_id not in channel_by_remote:
            self.logger.warning("Channel {} -> {} not found".format(self_id, remote_id))
            return False

        channel = channel_by_remote[remote_id]
        self.logger.debug("Channel {} -> {} state: {}".format(self_id, remote_id, channel))
        return channel.active

    def addfunds(self, bitcoind, satoshis):
        req = wallet_pb2.NewAddressRequest(type=1)
        addr = self.rpc.wallet.NewAddress(req).address
        bitcoind.rpc.sendtoaddress(addr, float(satoshis) / 10**8)
        self.daemon.wait_for_log("Inserting unconfirmed transaction")
        bitcoind.rpc.generate(1)
        self.daemon.wait_for_log("Marking unconfirmed transaction")

        # The above still doesn't mean the wallet balance is updated,
        # so let it settle a bit
        i = 0
        while self.rpc.wallet.WalletBalance(wallet_pb2.WalletBalanceRequest()).total_balance == satoshis and i < 30:
            time.sleep(1)
            i += 1
        assert(self.rpc.wallet.WalletBalance(wallet_pb2.WalletBalanceRequest()).total_balance == satoshis)

    def openchannel(self, node_id, host, port, satoshis):
        peers = self.rpc.routing.ListPeers(common_pb2.Void).peers
        peers_by_pubkey = {p.pub_key: p for p in peers}
        if node_id not in peers_by_pubkey:
            raise ValueError("Could not find peer {} in peers {}".format(node_id, peers))
        peer = peers_by_pubkey[node_id]
        self.rpc.channel.Open(channel_pb2.OpenChannelRequest(
            node_pubkey=codecs.decode(peer.pub_key, 'hex_codec'),
            local_funding_amount=common_pb2.Satoshi(value=satoshis),
            push_sat=0
        ))

        # Somehow broadcasting a tx is slow from time to time
        time.sleep(5)

    def getchannels(self):
        req = routing_pb2.ChannelGraphRequest()
        rep = self.rpc.routing.DescribeGraph(req)
        channels = []

        for e in rep.edges:
            channels.append((e.node1_pub, e.node2_pub))
            channels.append((e.node2_pub, e.node1_pub))
        return channels

    def getnodes(self):
        req = routing_pb2.ChannelGraphRequest()
        rep = self.rpc.routing.DescribeGraph(req)
        nodes = set([n.pub_key for n in rep.nodes]) - set([self.id()])
        return nodes

    def invoice(self, amount):
        req = payment_pb2.Invoice(value=common_pb2.Satoshi(value=int(amount)))
        rep = self.rpc.payment.AddInvoice(req)
        return rep.payment_request

    def send(self, bolt11):
        req = payment_pb2.SendRequest(payment_request=bolt11)
        res = self.rpc.payment.SendPaymentSync(req)
        if res.payment_error:
            raise ValueError(res.payment_error)
        return hexlify(res.payment_preimage)

    def connect(self, host, port, node_id):
        addr = routing_pb2.LightningAddress(pubkey=node_id, host="{}:{}".format(host, port))
        req = routing_pb2.ConnectPeerRequest(addr=addr, perm=True)
        logging.debug(self.rpc.routing.ConnectPeer(req))

    def info(self):
        r = self.rpc.routing.GetInfo(common_pb2.Void())
        return {
            'id': r.identity_pubkey,
            'blockheight': r.block_height,
        }

    def block_sync(self, blockhash):
        print("Waiting for node to learn about", blockhash)
        self.daemon.wait_for_log('NTFN: New block: height=([0-9]+), sha={}'.format(blockhash))

    def restart(self):
        self.daemon.stop()
        time.sleep(5)
        self.daemon.start()
        self.rpc = LpdRpc(self.daemon.rpc_port)

    def check_route(self, node_id, amount):
        try:
            req = routing_pb2.QueryRoutesRequest(pub_key=node_id, amt=int(amount/1000), num_routes=1)
            r = self.rpc.routing.QueryRoutes(req)
        except grpc._channel._Rendezvous as e:
            if str(e).find("unable to find a path to destination") > 0:
                return False
            raise
        return True


class LpdRpc(object):
    def __init__(self, rpc_port):
        self.port = rpc_port
        cred = grpc.ssl_channel_credentials(open('tls.cert').read())
        channel = grpc.secure_channel('localhost:{}'.format(rpc_port), cred)
        self.channel = ChannelServiceStub(channel)
        self.payment = PaymentServiceStub(channel)
        self.routing = RoutingServiceStub(channel)
