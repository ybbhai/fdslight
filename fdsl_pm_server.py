#!/usr/bin/env python3
import sys, getopt, os, signal, importlib, socket

BASE_DIR = os.path.dirname(sys.argv[0])

if not BASE_DIR: BASE_DIR = "."

sys.path.append(BASE_DIR)

PID_FILE = "/tmp/fdslight_pm.pid"
LOG_FILE = "/tmp/fdslight_pm.log"
ERR_FILE = "/tmp/fdslight_pm_error.log"

import pywind.evtframework.evt_dispatcher as dispatcher
import pywind.lib.configfile as configfile

import freenet.lib.proc as proc
import freenet.handlers.dns_proxy as dns_proxy
import freenet.handlers.tundev as tundev
import freenet.lib.utils as utils
import freenet.lib.base_proto.utils as proto_utils
import freenet.lib.nat as nat
import freenet.handlers.tunnels as tunnels
import freenet.lib.ip6dgram as ip6dgram
import freenet.handlers.traffic_pass as traffic_pass
import freenet.lib.logging as logging
import freenet.lib.fn_utils as fn_utils


class _fdslight_server(dispatcher.dispatcher):
    __configs = None
    __debug = None

    __access = None
    __mbuf = None

    __nat4 = None
    __nat6 = None

    __udp6_fileno = -1
    __tcp6_fileno = -1

    __udp_fileno = -1
    __tcp_fileno = -1

    __tcp_crypto = None
    __udp_crypto = None

    __crypto_configs = None

    __support_ip4_protocols = (6, 17, 132, 136,)
    __support_ip6_protocols = (6, 17, 132, 136,)

    __tundev_fileno = -1

    __DEVNAME = "fdslight_port_map"

    __IP6_ROUTER_TIMEOUT = 900

    @property
    def http_configs(self):
        configs = self.__configs.get("tunnel_over_http", {})

        pyo = {"auth_id": configs.get("auth_id", "fdslight"), "origin": configs.get("origin", "example.com")}

        return pyo

    def init_func(self, debug, configs, enable_nat_module=False):
        self.create_poll()

        self.__configs = configs
        self.__debug = debug

        signal.signal(signal.SIGINT, self.__exit)
        signal.signal(signal.SIGUSR1, self.__handle_user_change_signal)

        conn_config = self.__configs["connection"]
        mod_name = "freenet.access.%s" % conn_config["access_module"]

        try:
            access = importlib.import_module(mod_name)
        except ImportError:
            print("cannot found access module")
            sys.exit(-1)

        crypto_mod_name = conn_config["crypto_module"]

        tcp_crypto = "freenet.lib.crypto.nonay.noany_tcp"
        udp_crypto = "freenet.lib.crypto.noany.noany_udp"

        crypto_configfile = "%s/fdslight_etc/%s" % (BASE_DIR, conn_config["crypto_configfile"])

        try:
            self.__tcp_crypto = importlib.import_module(tcp_crypto)
            self.__udp_crypto = importlib.import_module(udp_crypto)
        except ImportError:
            print("cannot found tcp or udp crypto module")
            sys.exit(-1)

        if not os.path.isfile(crypto_configfile):
            print("cannot found crypto configfile")
            sys.exit(-1)

        try:
            self.__crypto_configs = proto_utils.load_crypto_configfile(crypto_configfile)
        except:
            print("crypto configfile should be json file")
            sys.exit(-1)

        enable_ipv6 = bool(int(conn_config["enable_ipv6"]))

        listen_port = int(conn_config["listen_port"])

        conn_timeout = int(conn_config["conn_timeout"])

        listen_ip = conn_config["listen_ip"]
        listen_ip6 = conn_config["listen_ip6"]

        listen = (listen_ip, listen_port,)
        listen6 = (listen_ip6, listen_port)

        over_http = bool(int(conn_config["tunnel_over_http"]))

        if enable_ipv6:
            self.__tcp6_fileno = self.create_handler(-1, tunnels.tcp_tunnel, listen6, self.__tcp_crypto,
                                                     self.__crypto_configs, conn_timeout=conn_timeout, is_ipv6=True,
                                                     over_http=over_http)
            self.__udp6_fileno = self.create_handler(-1, tunnels.udp_tunnel, listen6, self.__udp_crypto,
                                                     self.__crypto_configs, is_ipv6=True)

        self.__tcp_fileno = self.create_handler(-1, tunnels.tcp_tunnel, listen, self.__tcp_crypto,
                                                self.__crypto_configs, conn_timeout=conn_timeout, is_ipv6=False,
                                                over_http=over_http)
        self.__udp_fileno = self.create_handler(-1, tunnels.udp_tunnel, listen, self.__udp_crypto,
                                                self.__crypto_configs, is_ipv6=False)

        self.__tundev_fileno = self.create_handler(-1, tundev.tundevs, self.__DEVNAME)
        self.__mbuf = utils.mbuf()

        if not debug:
            sys.stdout = open(LOG_FILE, "a+")
            sys.stderr = open(ERR_FILE, "a+")

    def myloop(self):
        return

    def handle_msg_from_tunnel(self, fileno, session_id, address, action, message):
        size = len(message)
        pass

    def __handle_ipv4data_from_tunnel(self, session_id):
        self.__mbuf.offset = 9
        protocol = self.__mbuf.get_part(1)

        hdrlen = self.__get_ip4_hdrlen()
        if hdrlen + 8 < 28: return False

        # 检查IP数据报长度是否合法
        self.__mbuf.offset = 2
        payload_length = utils.bytes2number(self.__mbuf.get_part(2))
        if payload_length != self.__mbuf.payload_size: return False

        if protocol not in self.__support_ip4_protocols: return False

        # 对没有启用NAT内核模块UDP和UDPLite进行特殊处理,以支持内网穿透
        if (protocol == 17 or protocol == 136) and not self.__enable_nat_module:
            is_udplite = False
            if protocol == 136: is_udplite = True
            self.__handle_ipv4_dgram_from_tunnel(session_id, is_udplite=is_udplite)
            return True
        self.__mbuf.offset = 0

        rs = self.__nat4.get_ippkt2sLan_from_cLan(session_id, self.__mbuf)
        if not rs:
            logging.print_error("cannot modify source address from client packet for ipv4")
            return
        self.__mbuf.offset = 0
        self.get_handler(self.__tundev_fileno).handle_msg_from_tunnel(self.__mbuf.get_data())
        return True

    def send_msg_to_tunnel_from_tun(self, message):
        if len(message) > utils.MBUF_AREA_SIZE: return

        self.__mbuf.copy2buf(message)

        ip_ver = self.__mbuf.ip_version()

        if ip_ver == 6 and not self.__enable_nat6: return
        if ip_ver == 4:
            ok, session_id = self.__nat4.get_ippkt2cLan_from_sLan(self.__mbuf)
        else:
            ok, session_id = self.__nat6.get_ippkt2cLan_from_sLan(self.__mbuf)

        if not ok: return

        self.__mbuf.offset = 0

    def __exit(self, signum, frame):
        pass


def __start_service(debug, enable_nat_module):
    if not debug and os.path.isfile(PID_FILE):
        print("the fdsl_server process exists")
        return

    if not debug:
        pid = os.fork()
        if pid != 0: sys.exit(0)

        os.setsid()
        os.umask(0)
        pid = os.fork()

        if pid != 0: sys.exit(0)
        proc.write_pid(PID_FILE)

    configs = configfile.ini_parse_from_file("%s/fdslight_etc/fn_server.ini" % BASE_DIR)
    cls = _fdslight_server()

    if debug:
        cls.ioloop(debug, configs, enable_nat_module=enable_nat_module)
        return
    try:
        cls.ioloop(debug, configs, enable_nat_module=enable_nat_module)
    except:
        logging.print_error()

    os.remove(PID_FILE)


def __stop_service():
    pid = proc.get_pid(PID_FILE)

    if pid < 0:
        print("cannot found fdslight server process")
        return

    os.kill(pid, signal.SIGINT)


def __update_user_configs():
    pid = proc.get_pid(PID_FILE)

    if pid < 0:
        print("cannot found fdslight server process")
        return

    os.kill(pid, signal.SIGUSR1)


def main():
    help_doc = """
    -d      debug | start | stop    debug,start or stop application
    --enable_nat_module             enable kernel cone nat module
    -u      user_configs            update configs           
    """
    try:
        opts, args = getopt.getopt(sys.argv[1:], "u:m:d:", ["enable_nat_module"])
    except getopt.GetoptError:
        print(help_doc)
        return
    d = ""
    u = ""

    enable_nat_module = False

    for k, v in opts:
        if k == "-d": d = v
        if k == "-u": u = v
        if k == "--enable_nat_module": enable_nat_module = True

    if not u and not d:
        print(help_doc)
        return

    if u and u != "user_configs":
        print(help_doc)
        return

    if u:
        __update_user_configs()
        return

    if not d:
        print(help_doc)
        return

    if d not in ("debug", "start", "stop"):
        print(help_doc)
        return

    debug = False

    if d == "stop":
        __stop_service()
        return

    if d == "debug": debug = True
    if d == "start": debug = False

    __start_service(debug, enable_nat_module)


if __name__ == '__main__': main()