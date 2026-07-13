#!/usr/bin/env python3
"""Docker environment management.  Requires: ``pip install docker``."""

import ctypes, ctypes.util, os, signal, socket, struct, threading, time

import docker
from docker import types as _dt

# ------------------------------------------------------------------ setns + pcap helpers ---


def _load_libc():
    for name in ('libc.so.6', ctypes.util.find_library('c'),
                 'libc.musl-x86_64.so.1', 'libc.so'):
        if not name:
            continue
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    return None


_libc = _load_libc()
if _libc is not None:
    _libc.setns.restype = ctypes.c_int
    _libc.setns.argtypes = [ctypes.c_int, ctypes.c_int]
_CLONE_NEWNET = 0x40000000

_PCAP_MAGIC = 0xA1B2C3D4
_PCAP_SNAPLEN = 65535


def _setns(fd, nstype):
    if _libc is None:
        raise RuntimeError("libc with setns() is not available in this environment")
    if _libc.setns(fd, nstype):
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def _write_pcap_header(f):
    f.write(struct.pack('=IHHiIII', _PCAP_MAGIC, 2, 4, 0, 0, _PCAP_SNAPLEN, 1))


def _write_pcap_packet(f, data, ts):
    sec, usec = int(ts), int((ts - int(ts)) * 1_000_000)
    f.write(struct.pack('=IIII', sec, usec, len(data), len(data)))
    f.write(data); f.flush()


def _ns_list_ifaces(pid):
    r, w = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(r)
        try:
            fd = os.open(f'/proc/{pid}/ns/net', os.O_RDONLY)
            _setns(fd, _CLONE_NEWNET); os.close(fd)
            ifaces = sorted(n for _, n in socket.if_nameindex() if n != 'lo')
            os.write(w, '\n'.join(ifaces).encode())
        except Exception:
            os.write(w, b'')
        finally:
            os.close(w); os._exit(0)

    os.close(w)
    data = b''.join(iter(lambda: os.read(r, 4096), b''))
    os.close(r); os.waitpid(child, 0)
    return [i for i in data.decode().split('\n') if i]


def _capture_worker(pid, iface, path):
    fd = os.open(f'/proc/{pid}/ns/net', os.O_RDONLY)
    _setns(fd, _CLONE_NEWNET); os.close(fd)

    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    s.bind((iface, 0)); s.settimeout(0.5)

    run = True

    def _stop(*_):
        nonlocal run; run = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        with open(path, 'wb') as f:
            _write_pcap_header(f)
            while run:
                try:
                    data = s.recv(_PCAP_SNAPLEN)
                    _write_pcap_packet(f, data, time.time())
                except socket.timeout:
                    continue
                except (OSError, IOError):
                    break
    finally:
        s.close(); os._exit(0)


# ----------------------------------------------------------------------------------- class ---

class DockerEnvironment:

    def __init__(self, base_url='unix://var/run/docker.sock'):
        self.c = docker.from_env()
        self.a = docker.APIClient(base_url=base_url)

    # -- resolve helpers ------------------------------------------------

    def _c(self, ref):
        return ref if not isinstance(ref, str) else self.c.containers.get(ref)

    def _n(self, ref):
        return ref if not isinstance(ref, str) else self.c.networks.get(ref)

    def _v(self, ref):
        return ref if not isinstance(ref, str) else self.c.volumes.get(ref)

    # -- networks -------------------------------------------------------

    def create_network(self, name, driver='bridge', subnet=None, gateway=None,
                       labels=None, internal=False, attachable=False, **kw):
        ipam = _dt.IPAMConfig(pool_configs=[_dt.IPAMPool(subnet=subnet, gateway=gateway)]) if subnet else None
        return self.c.networks.create(name, driver=driver, ipam=ipam, labels=labels,
                                      internal=internal, attachable=attachable, **kw)

    def remove_network(self, name):
        self._n(name).remove()

    def get_network(self, name):
        return self._n(name)

    def list_networks(self, **kw):
        return self.c.networks.list(**kw)

    # -- containers -----------------------------------------------------

    def create_container(self, image, name=None, command=None, environment=None,
                         networks=None, volumes=None, ports=None,
                         restart_policy=None, cap_add=None, privileged=False,
                         start=True, entrypoint=None, working_dir=None,
                         user=None, hostname=None, **kw):
        mounts = self._mounts(volumes)
        net_specs = self._nets(networks)
        host = self.a.create_host_config(
            mounts=mounts, port_bindings=ports, restart_policy=restart_policy,
            cap_add=cap_add, privileged=privileged,
        )
        nw_cfg = None
        if net_specs:
            eps = {}
            for ns in net_specs:
                ep = {}
                if ns.get('ip'): ep['ipv4_address'] = ns['ip']
                if ns.get('aliases'): ep['aliases'] = ns['aliases']
                eps[ns['name']] = self.a.create_endpoint_config(**ep)
            nw_cfg = self.a.create_networking_config(eps)
        resp = self.a.create_container(
            image=image, name=name, command=command, environment=environment,
            host_config=host, networking_config=nw_cfg,
            entrypoint=entrypoint, working_dir=working_dir, user=user,
            hostname=hostname, **kw,
        )
        c = self.c.containers.get(resp['Id'])
        if start:
            c.start()
        return c

    def remove_container(self, name, force=False, v=False):
        self._c(name).remove(force=force, v=v)

    def get_container(self, name):
        return self._c(name)

    def list_containers(self, all=False, **kw):
        return self.c.containers.list(all=all, **kw)

    # -- network attachment ---------------------------------------------

    def connect_to_network(self, container, network, ip=None, aliases=None):
        kw = {}
        if ip: kw['ipv4_address'] = ip
        if aliases: kw['aliases'] = aliases
        self._n(network).connect(self._c(container), **kw)

    def disconnect_from_network(self, container, network, force=False):
        self._n(network).disconnect(self._c(container), force=force)

    # -- volumes --------------------------------------------------------

    def create_volume(self, name=None, driver='local', driver_opts=None, labels=None):
        return self.c.volumes.create(name=name, driver=driver, driver_opts=driver_opts, labels=labels)

    def remove_volume(self, name, force=False):
        self._v(name).remove(force=force)

    def get_volume(self, name):
        return self._v(name)

    def list_volumes(self, **kw):
        return self.c.volumes.list(**kw)

    # -- images ----------------------------------------------------------

    def image_exists(self, name):
        try:
            self.c.images.get(name)
            return True
        except Exception:
            return False

    def build_image(self, path, tag=None, dockerfile=None, buildargs=None, **kw):
        tag = tag or os.path.basename(os.path.abspath(path))
        stream = self.a.build(
            path=path, tag=tag, dockerfile=dockerfile,
            buildargs=buildargs, decode=True, **kw,
        )
        for chunk in stream:
            if 'stream' in chunk:
                pass  # print(chunk['stream'].rstrip())  # uncomment for verbose
            elif 'error' in chunk:
                raise RuntimeError(chunk['error'])
        return self.c.images.get(tag)

    # -- container lifecycle --------------------------------------------

    def stop_container(self, name, timeout=10):
        self._c(name).stop(timeout=timeout)

    def start_container(self, name):
        self._c(name).start()

    def restart_container(self, name, timeout=10):
        self._c(name).restart(timeout=timeout)

    def pause_container(self, name):
        self._c(name).pause()

    def unpause_container(self, name):
        self._c(name).unpause()

    # -- logs -----------------------------------------------------------

    def get_logs(self, name, tail='all', since=None, until=None,
                 follow=False, stream=False, timestamps=False,
                 stdout=True, stderr=True):
        logs = self._c(name).logs(tail=tail, since=since, until=until,
                                  follow=follow, stream=stream, timestamps=timestamps,
                                  stdout=stdout, stderr=stderr)
        if stream:
            return logs
        return logs.decode('utf-8', errors='replace') if isinstance(logs, bytes) else logs

    # -- exec -----------------------------------------------------------

    def exec_command(self, name, cmd, workdir=None, environment=None,
                     user=None, detach=False, tty=False, stdin=False):
        if isinstance(cmd, str):
            cmd = ['/bin/sh', '-c', cmd]
        r = self._c(name).exec_run(cmd, workdir=workdir, environment=environment,
                                   user=user, detach=detach, tty=tty, stdin=stdin)
        if detach:
            return r
        code, out = r
        return code, (out.decode('utf-8', errors='replace') if isinstance(out, bytes) else out)

    # -- dump collection ------------------------------------------------

    def collect_dump(self, container_name, output_dir, timeout=None,
                     interface_filter=None, block=True):
        os.makedirs(output_dir, exist_ok=True)

        pid = self._pid(container_name)
        if not pid:
            raise RuntimeError(f"Container '{container_name}' is not running")

        ifaces = _ns_list_ifaces(pid)
        if interface_filter:
            ifaces = [i for i in ifaces if i in interface_filter]
        if not ifaces:
            raise RuntimeError(f"No non-lo interfaces in '{container_name}'")

        pids, pids_file = [], os.path.join(output_dir, '.tcpdump_pids')
        for iface in ifaces:
            child = os.fork()
            if child == 0:
                _capture_worker(pid, iface, os.path.join(output_dir, f'{iface}.pcap'))
            pids.append(child)

        with open(pids_file, 'w') as f:
            f.write('\n'.join(map(str, pids)) + '\n')

        info = {'output_dir': output_dir, 'pids': pids, 'pids_file': pids_file, 'interfaces': ifaces}
        if not block:
            return info

        ev = threading.Event()
        def _on_sig(*_):
            ev.set()

        old = {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL}
        try:
            for sig in old:
                old[sig] = signal.signal(sig, _on_sig)
        except ValueError:
            pass

        try:
            if timeout is not None:
                ev.wait(timeout)
            else:
                while not ev.is_set():
                    ev.wait(0.1)
        except (KeyboardInterrupt, OSError):
            pass
        finally:
            for sig, prev in old.items():
                try: signal.signal(sig, prev)
                except (ValueError, OSError): pass
            self.stop_dump(info)
        return info

    def stop_dump(self, info):
        for pid in info.get('pids', []):
            try: os.kill(pid, signal.SIGTERM)
            except (ProcessLookUpError, OSError): pass
        for pid in info.get('pids', []):
            try: os.waitpid(pid, 0)
            except (ChildProcessError, OSError): pass
        pf = info.get('pids_file')
        if pf and os.path.exists(pf):
            try: os.remove(pf)
            except OSError: pass

    # -- internals ------------------------------------------------------

    def _pid(self, name):
        try: return self.a.inspect_container(name)['State']['Pid']
        except (KeyError, TypeError): return 0

    @staticmethod
    def _nets(networks):
        if networks is None:
            return []
        if isinstance(networks, str):
            return [{'name': networks}]
        if isinstance(networks, dict):
            return [{'name': k, 'ip': v} for k, v in networks.items()]
        out = []
        for i in networks:
            if isinstance(i, str):
                out.append({'name': i})
            elif isinstance(i, dict):
                out.append({'name': i['name'], 'ip': i.get('ip'), 'aliases': i.get('aliases')})
        return out

    @staticmethod
    def _mounts(volumes):
        if not volumes:
            return []
        ms = []
        for src, spec in volumes.items():
            if isinstance(spec, _dt.Mount):
                ms.append(spec)
            elif isinstance(spec, str):
                ms.append(_dt.Mount(target=spec, source=src, type='volume'))
            elif isinstance(spec, dict):
                t = spec.get('bind') or spec.get('target')
                mt = 'bind' if 'bind' in spec else spec.get('type', 'volume')
                kw = {'target': t, 'source': src, 'type': mt}
                if spec.get('mode') == 'ro':
                    kw['read_only'] = True
                ms.append(_dt.Mount(**kw))
        return ms
