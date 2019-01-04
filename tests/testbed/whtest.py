#!/usr/bin/env python3

from base64 import b16encode
import contextlib
import docker
import functools
import io
import json
import logging
import os
import tarfile
import telnetlib
import tempfile
import time
import weakref

def constant(func):
    @property
    @functools.wraps(func)
    def wrapper(self):
        attr = f'_attr_{func.__name__}'
        if not hasattr(self, attr):
            setattr(self, attr, func(self))

        return getattr(self, attr)
    return wrapper

def retry(count=6, wait=0.5):
    def decorator(f):
        for _ in range(count):
            if f():
                break

            time.sleep(wait)

        else:
            raise Exception(f"failed after {count} retry/ies.")

    return decorator

PREFIX = "wh-testbed-"

def check_images(c):
    IMAGES = set((
        'wirehub/testbed-micronet:latest',
        'wirehub/testbed-wh:latest',
    ))

    installed_images = set()
    for i in c.images.list():
        installed_images.update(i.tags)

    missing_images = IMAGES.difference(installed_images)

    if missing_images:
        print("ERROR: missing Docker images")
        for i in sorted(missing_images):
            print(f"- {i}")

        return False

    return True

def clean(c):
    print("cleaning...")
    conts = [c for c in c.containers.list(all=True) if c.name.startswith(PREFIX)]

    for ct in conts:
        if ct.status == "running":
            ct.kill()
        ct.remove()

    nets = [n for n in c.networks.list() if n.name.startswith(PREFIX)]

    for n in nets:
        n.remove()

def write_file(ct, filepath, content):
    fh = io.BytesIO(content)
    tar_info = tarfile.TarInfo(filepath)
    tar_info.size = len(content)

    tar_fh = io.BytesIO()
    t = tarfile.TarFile(mode='w', fileobj=tar_fh)
    t.addfile(tar_info, fh)
    t.close()

    tar_buf = tar_fh.getvalue()
    return ct.put_archive("/", tar_buf)

def read_micronet_conf(c, micronet_conf):
    ct = c.containers.create("wirehub/micronet", "micronet read /conf")
    write_file(ct, "/conf", micronet_conf.encode('utf-8'))
    ct.start()
    @retry
    def f():
        return ct.status == 'running'
    resp = ct.logs().decode('utf-8')
    ct.kill()
    ct.remove()

    r = {}
    for i in resp.strip().split('\n'):
        peer_id, peer_type, peer_up = i.split()

        peer_id = int(peer_id)
        peer_up = int(peer_up)

        r[peer_id] = {"type": peer_type, "up": peer_up}

    return r

class Shell:
    PS1 = b"telnet# "

    def __init__(self, ip, logger):
        self.ip = ip
        self.logger = logger
        self._value = None

        self.t = telnetlib.Telnet(self.ip)
        self.t.read_until(b"login:")
        self.t.write(b"root\n")
        self.t.read_until(b"# ")
        self.t.write(b"export PS1=\"" + self.PS1 + b"\"\n")
        self.t.read_until(b"\n" + self.PS1)

    def execute(self, cmd, encoding='utf-8', blocking=None, env=None):
        if blocking is None: blocking = True

        line = []
        if env:
            line.extend(f"{k}={v}" for k, v in sorted(env.items()))

        line.append(cmd)
        line = " ".join(line)

        self.logger.info(f"# {line}")

        if encoding is not None:
            line = line.encode(encoding)

        line = line + b'\n'

        self._value = None
        self.t.write(line)

        if blocking:
            while True:
                buf = self.t.read_until(b"\r\n")
                if not buf.endswith(b'\r\r\n'):
                    break

            log = self.t.read_until(self.PS1)

            assert(log.endswith(self.PS1))
            log = log[:-(2+len(self.PS1))]

            if encoding is not None:
                log = log.decode(encoding)

            if log:
                trunc_log = log
                truncated = False
                if '\n' in trunc_log:
                    trunc_log = trunc_log.split('\n', 1)[0].strip()
                    truncated=True

                if len(trunc_log) > 128:
                    trunc_log = trunc_log[:128]
                    truncated=True

                if truncated:
                    trunc_log = trunc_log + f" (... truncated {len(log)}B)"

                self.logger.info(f"> {trunc_log}")

            return log

        else:
            self.logger.info("> (non blocking)")

    __call__ = execute

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.t.close()

    @property
    def value(self):
        if self._value is None:
            self._value = int(self("echo $?"))

        return self._value

class Container:
    def __init__(self, logger):
        self.parent_logger = logger
        self.ct = None

    def stop(self):
        if self.ct:
            self.ct.reload()
            if self.ct.status == 'running':
                self.ct.kill()

            self.ct.remove()
            self.ct = None

    @constant
    def logger(self):
        return self.parent_logger.getChild(self.hostname)

    @constant
    def ip(self):
        self.ct.reload()
        while self.ct.status != 'running':
            self.ct.reload()
            time.sleep(.5)

        return self.ct.attrs['NetworkSettings']['Networks'][self.net.name]['IPAddress']

    @property
    def hostname(self):
        return self.ct.attrs['Config']['Hostname']

    def shell(self):
        return Shell(self.ip, self.logger)

class Micronet(Container):
    def __init__(self, c, net, unet_conf, name, logger):
        super().__init__(logger)

        self.c = c
        self.net = net
        self.unet_conf = unet_conf
        self.name = name

    def start(self):
        self.ct = self.c.containers.create(
            image="wirehub/micronet",
            command="micronet server /conf",
            detach=True,
            hostname=self.name,
            name=self.name,
            network=self.net.name,
        )

        write_file(self.ct, "/conf", self.unet_conf.encode('utf-8'))

        self.ct.start()

class WHClient:
    class Error(Exception):
        def __init__(self, value):
            self.value = value
            super().__init__(f"returned value is not 0: {value}")

    def __init__(self, sh):
        self.sh = sh

    def __enter__(self):
        self.sh.__enter__()
        return self

    def __exit__(self, *exc):
        self.sh.__exit__(*exc)

    def __call__(self, *kargs, **kwargs):
        cmd = []

        blocking = kwargs.pop("blocking", True)
        env = kwargs.pop("env", None)

        stdin = kwargs.pop("stdin", None)
        if stdin is not None:
            cmd.extend((
                "echo",
                stdin,
                "|",
            ))

        cmd.append("wh")
        cmd.extend(map(str, kargs))

        for k, v in sorted(kwargs.items()):
            k = k.replace('_', '-')
            if v is True:
                cmd.append(k)

            else:
                cmd.append(k)
                cmd.append(str(v))

        cmd = " ".join(cmd)
        log = self.sh(cmd, blocking=blocking, env=env)

        #if blocking:
        #    if self.sh.value != 0:
        #        raise self.Error(sh.value)

        return log

    def genkey(self, conf):
        sk = self("genkey", conf)
        k = self("pubkey", stdin=sk)

        return sk, k

    def inspect(self, interface):
        buf = self('inspect', interface)

        if not buf:
            return None

        try:
            return json.loads(buf)

        except:
            print(buf)
            raise

class Node(Container):
    def __init__(self, c, net, name, logger):
        super().__init__(logger)

        self.c = c
        self.net = net
        self.name = name

    def start(self):
        kwargs = dict(
            image="wirehub/testbed-wh",
            cap_add = ("NET_ADMIN", ),
            detach=True,
            hostname=self.name,
            name=self.name,
            network=self.net.name,
            volumes={},
        )

        if os.environ.get("WH_DEBUG", "n").lower() == "y":
            repo_path = os.path.dirname(__file__)
            repo_path = os.path.normpath(os.path.join(repo_path, "../../src"))

            kwargs['volumes'][repo_path] = {'bind': '/opt/wh', 'mode': 'ro'}

        self.ct = self.c.containers.run(**kwargs)

    def start_micronet(self, server_ip, peer_id):
        sh = self.shell()
        sh("mkdir /dev/net")
        sh("mknod /dev/net/tun c 10 200")
        sh(f"UNET_SERVERNAME={server_ip} micronet client {peer_id}", blocking=False)

        self.unet_sh = sh

        # check micronet is started
        with self.shell() as sh:
            for _ in range(5):
                sh("ip link show micronet")
                if sh.value == 0:   # found
                    break
            else:
                raise Exception("something wrong with micronet")

    def wh(self):
        return WHClient(self.shell())

class Env:
    def __init__(self, unet_conf, c=None, name=None):
        if c is None:
            c = docker.from_env()

        if name is None:
            name = b16encode(os.urandom(4)).decode('ascii').lower()

        self.logger = logging.getLogger(f"env.{name}")
        self.c = c
        self.prefix = f"{PREFIX}{name}"
        self.unet_conf = unet_conf

        self.unet = None
        self.nodes = {}

    @constant
    def unet_desc(self):
        return read_micronet_conf(self.c, self.unet_conf)

    @constant
    def peer_count(self):
        return max(k for k, v in self.unet_desc.items() if v["type"] == "peer")

    @property
    def _priv_node(self):
        return self.nodes[1]

    @constant
    def _priv_wh(self):
        return self._priv_node.wh()

    def start(self):
        self.logger.info(f"initialize network")

        self.net = self.c.networks.create(
            name=self.prefix,
            driver="bridge"
        )

        self.logger.info("run micronet network")
        self.unet = Micronet(
            c=self.c,
            net=self.net,
            unet_conf=self.unet_conf,
            name=f"{self.prefix}-micronet",
            logger=self.logger
        )

        self.unet.start()

        for i in range(self.peer_count):
            peer_id = i+1

            self.logger.info(f"run node #{peer_id}")
            self.nodes[peer_id] = n = Node(
                c=self.c,
                net=self.net,
                name=f"{self.prefix}-{peer_id}",
                logger=self.logger
            )

            n.start()
            n.start_micronet(self.unet.ip, peer_id)

            with n.shell() as sh:
                sh("rm -f /etc/wirehub/*")


    def stop(self):
        self.logger.info("stopping environment")
        for peer_id, n in sorted(self.nodes.items()):
            self.logger.info(f"stop node #{peer_id}")
            n.stop()

        if self.unet:
            self.logger.info(f"stop micronet")
            self.unet.stop()

        if self.net:
            self.logger.info(f"remove network")
            self.net.remove()

    def __getitem__(self, i):
        return self.nodes[i]

    def lua(self, script, *kargs):
        write_file(self._priv_node.ct, "/tmp/script.lua", script.encode('utf-8'))
        return self._priv_wh.sh("LUA_PATH=/opt/wh/?.lua lua /tmp/script.lua " + " ".join(map(str, kargs)))

    def wh(self, *kargs, **kwargs):
        r = {}
        for peer_id, n in self.nodes.items():
            with n.wh() as wh:
                r[peer_id] = wh(*kargs, **kwargs)

        return r

    def setup_public(self, bootstrap_ip='1.1.1.1', workbit=8, env=None):
        """ Setup a network where first peer is a bootstrap. """

        bstp_n = self.nodes[1]

        # setup network
        self.wh("clearconf", "public")
        self.wh("set", "public", workbit=8)

        # generate keys for network
        for peer_id, n in self.nodes.items():
            with n.wh() as wh:
                n.sk, n.k = wh.genkey("public")
                wh.sh(f"echo {n.sk} > /sk")
                wh.sh(f"echo {n.k} > /k")

        # setup bootstrap
        self.wh("set", "public", endpoint=bootstrap_ip, bootstrap="yes", untrusted=True, peer=bstp_n.k)

        # start bootstrap
        bstp_n.daemon_wh = bstp_n.wh()
        bstp_n.daemon_wh("up", "public", private_key="/sk", mode="direct", blocking=False, env=env)

@contextlib.contextmanager
def env(*kargs, **kwargs):
    e = Env(*kargs, **kwargs)
    try:
        e.start()
        yield e

    finally:
        e.stop()

@contextlib.contextmanager
def env_single_node():
    with env("M(wan() | peer())") as e:
        yield e.nodes[1]
