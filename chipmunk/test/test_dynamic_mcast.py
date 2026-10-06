#!/usr/bin/env python3
"""Exercise multicast interface selection in disposable Linux net namespaces.

Run as root. The outer process always creates a fresh network namespace before
creating or changing any interface; no existing network is used for the test.
"""

import argparse
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


RX_IF = "upx_rx"
TX_IF = "upx_tx"
OLD_IP = "198.18.0.2"
NEW_IP = "198.18.0.5"
SOURCE_IP = "198.18.0.1"
ASM_GROUP = "239.255.123.1"
SSM_GROUP = "232.255.123.1"
UDP_PORT = 5500
HTTP_PORT = 4022
MARKER = b"UDPXY-NETNS-REGRESSION"
CASES = (
    "dynamic-asm", "dynamic-ssm", "fixed-asm", "fixed-ssm",
    "default-asm", "renew-asm", "renew-ssm",
)


def run_ip(*args):
    return subprocess.run(
        ["ip", *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def netns_id():
    return os.stat("/proc/self/ns/net").st_ino


def mountns_id():
    return os.stat("/proc/self/ns/mnt").st_ino


def assert_fresh_netns(parent_id):
    if netns_id() == parent_id:
        raise RuntimeError("refusing to test without a new network namespace")
    interfaces = [name for _, name in socket.if_nameindex()]
    if interfaces != ["lo"]:
        raise RuntimeError("fresh namespace must contain only lo: %r" % interfaces)


def stop_process(process):
    if process is None:
        return
    try:
        # The foreground udpxy worker children share the subprocess group.
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def sender_worker(parent_id):
    assert_fresh_netns(parent_id)
    print(os.getpid(), flush=True)
    if sys.stdin.readline().strip() != "go":
        raise RuntimeError("sender did not receive setup handshake")
    run_ip("link", "set", "lo", "up")
    run_ip("addr", "add", SOURCE_IP + "/24", "dev", TX_IF)
    run_ip("link", "set", TX_IF, "up")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SOURCE_IP, 0))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(SOURCE_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    # Seven 188-byte MPEG-TS null packets: udpxy sees real UDP input, not a
    # source-file shortcut. A marker lets the HTTP client identify this input.
    packet = b"\x47\x1f\xff\x10" + MARKER
    packet += b"\xff" * (188 - len(packet))
    payload = packet * 7
    print("ready", flush=True)
    while True:
        for group in (ASM_GROUP, SSM_GROUP):
            sock.sendto(payload, (group, UDP_PORT))
        time.sleep(0.01)


def read_line(process, timeout=5):
    readable, _, _ = select.select([process.stdout], [], [], timeout)
    if not readable:
        raise RuntimeError("sender setup timed out")
    line = process.stdout.readline().strip()
    if not line:
        raise RuntimeError("sender exited during setup")
    return line


def setup_network():
    run_ip("link", "set", "lo", "up")
    sender = subprocess.Popen(
        ["unshare", "--net", "--fork", sys.executable,
         str(Path(__file__).resolve()), "--sender", str(netns_id())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        sender_pid = int(read_line(sender))
        run_ip("link", "add", RX_IF, "type", "veth", "peer", "name", TX_IF)
        run_ip("link", "set", TX_IF, "netns", str(sender_pid))
        run_ip("addr", "add", OLD_IP + "/32", "dev", RX_IF)
        run_ip("link", "set", RX_IF, "up")
        # Give strict reverse-path filtering a valid return route too. All
        # routes are contained in this disposable receiver namespace.
        run_ip("route", "add", SOURCE_IP + "/32", "dev", RX_IF)
        sender.stdin.write("go\n")
        sender.stdin.flush()
        if read_line(sender) != "ready":
            raise RuntimeError("unexpected sender handshake")
        return sender
    except Exception:
        stop_process(sender)
        raise


def set_receiver_address(address):
    other = NEW_IP if address == OLD_IP else OLD_IP
    # Add before removing the previous /32 to avoid an address-less interval
    # or Linux secondary-address promotion influencing this test.
    existing = run_ip("-o", "-4", "addr", "show", "dev", RX_IF)
    if address + "/32" not in existing:
        run_ip("addr", "add", address + "/32", "dev", RX_IF)
    if other + "/32" in existing:
        run_ip("addr", "del", other + "/32", "dev", RX_IF)


class Stream:
    def __init__(self, source_specific):
        self.sock = socket.create_connection(("127.0.0.1", HTTP_PORT), timeout=3)
        try:
            group = SSM_GROUP if source_specific else ASM_GROUP
            target = (SOURCE_IP + "@" if source_specific else "") + group
            request = "GET /udp/%s:%d HTTP/1.0\r\nHost: localhost\r\n\r\n" % (
                target, UDP_PORT,
            )
            self.sock.sendall(request.encode("ascii"))
            self.pending = b""
            self.status = None
            data = b""
            while b"\r\n\r\n" not in data and b"\n\n" not in data:
                block = self.sock.recv(4096)
                if not block:
                    raise RuntimeError("HTTP connection ended before response headers")
                data += block
                if len(data) > 32768:
                    raise RuntimeError("HTTP response headers too large")
            delimiter = b"\r\n\r\n" if b"\r\n\r\n" in data else b"\n\n"
            header, self.pending = data.split(delimiter, 1)
            self.status = int(header.splitlines()[0].split()[1])
        except BaseException:
            self.sock.close()
            raise

    def read_payload(self, seconds=0):
        if self.status != 200:
            raise RuntimeError("HTTP status %s instead of 200" % self.status)
        deadline = time.monotonic() + seconds
        observed = False
        count = 0
        recent = b""
        while True:
            block = self.pending
            self.pending = b""
            if not block:
                block = self.sock.recv(8192)
            if not block:
                raise RuntimeError("HTTP video stream ended")
            count += len(block)
            recent = (recent + block)[-16384:]
            observed = observed or MARKER in recent
            if observed and time.monotonic() >= deadline:
                return count

    def close(self):
        self.sock.close()


def expect_stream(source_specific, seconds=0):
    stream = None
    try:
        stream = Stream(source_specific)
        return stream.read_payload(seconds)
    finally:
        if stream is not None:
            stream.close()


def expect_failure(source_specific):
    try:
        expect_stream(source_specific)
    except (RuntimeError, OSError):
        return
    raise AssertionError("removed literal IPv4 unexpectedly received multicast")


def wait_for_http(process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("udpxy exited with status %s" % process.returncode)
        try:
            with socket.create_connection(("127.0.0.1", HTTP_PORT), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("udpxy did not open its HTTP listener")


def test_case(binary, case, log_directory):
    set_receiver_address(OLD_IP)
    source_specific = case.endswith("ssm")
    literal = case.startswith("fixed")
    default_interface = case == "default-asm"
    interface = OLD_IP if literal else RX_IF
    if default_interface:
        run_ip("route", "add", ASM_GROUP + "/32", "dev", RX_IF)
    log_path = log_directory / (case + ".log")
    with log_path.open("wb") as log:
        command = [str(binary), "-T", "-v", "-a", "127.0.0.1", "-p", str(HTTP_PORT),
                   "-c", "8", "-B", "65536", "-R", "1", "-H", "1",
                   "-M", "30" if case.startswith("renew") else "0"]
        if not default_interface:
            command.extend(("-m", interface))
        process = subprocess.Popen(
            command,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        pid = process.pid
        try:
            wait_for_http(process)
            if case.startswith("renew"):
                stream = Stream(source_specific)
                try:
                    stream.read_payload()
                    set_receiver_address(NEW_IP)
                    # -M accepts a minimum of 30 s. This forces the original
                    # playback worker through at least one drop/add cycle.
                    count = stream.read_payload(seconds=35)
                    assert count > 1316, "insufficient traffic across renewal"
                finally:
                    stream.close()
                expect_stream(source_specific)
            else:
                expect_stream(source_specific)
                set_receiver_address(NEW_IP)
                if literal:
                    expect_failure(source_specific)
                else:
                    expect_stream(source_specific)
            assert process.poll() is None, "main udpxy process exited"
            assert process.pid == pid, "main udpxy process changed"
        finally:
            stop_process(process)
            if default_interface:
                run_ip("route", "del", ASM_GROUP + "/32", "dev", RX_IF)
    return log_path


def diagnostic_log_tail(path):
    lines = [line for line in path.read_text(errors="replace").splitlines()
             if line.strip()]
    keywords = ("error", "invalid", "failed", "failure", "setsockopt", "no such",
                "cannot", "multicast", "mcast", "client process", "new_socket",
                "http/", "usage:")
    diagnostic = [line for line in lines
                  if any(keyword in line.lower() for keyword in keywords)]
    return "\n".join((diagnostic or lines)[-50:])


def run_suite(args):
    assert_fresh_netns(args.inside_netns)
    if args.parent_mountns is None or mountns_id() == args.parent_mountns:
        raise RuntimeError("refusing to test without a new mount namespace")
    selected = args.case or list(CASES)
    if args.quick:
        selected = [case for case in selected if not case.startswith("renew")]
    if not selected:
        raise RuntimeError("no tests selected")
    sender = None
    failures = []
    with tempfile.TemporaryDirectory(prefix="udpxy-mcast-test-") as temporary:
        logs = Path(temporary)
        # Root-owned foreground udpxy still writes /var/run/udpxyPORT.pid.
        # Keep its lock separate from real services and concurrent test runs.
        pid_directory = logs / "run"
        pid_directory.mkdir()
        if not Path("/var/run").is_dir():
            raise RuntimeError("default Linux PID directory /var/run is unavailable")
        subprocess.run(["mount", "--no-mtab", "--bind", str(pid_directory), "/var/run"],
                       check=True)
        try:
            sender = setup_network()
            for case in selected:
                print("RUN %s" % case, flush=True)
                try:
                    test_case(args.binary, case, logs)
                    print("PASS %s" % case, flush=True)
                except (AssertionError, RuntimeError, OSError) as error:
                    failures.append(case)
                    print("FAIL %s: %s" % (case, error), flush=True)
                    log = logs / (case + ".log")
                    if log.exists():
                        print(diagnostic_log_tail(log), flush=True)
                if sender.poll() is not None:
                    raise RuntimeError("multicast sender exited during tests")
        finally:
            stop_process(sender)
    print("%d/%d passed" % (len(selected) - len(failures), len(selected)), flush=True)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "udpxy",
                        help="compiled udpxy executable (default: ../udpxy)")
    parser.add_argument("--case", choices=CASES, action="append",
                        help="run only selected case(s); repeat to select several")
    parser.add_argument("--quick", action="store_true",
                        help="skip the two 35-second renewal cases")
    parser.add_argument("--inside-netns", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--parent-mountns", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--sender", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Linux root is required (including CAP_NET_ADMIN and CAP_SYS_ADMIN)")
    for command in ("ip", "unshare", "mount"):
        if shutil.which(command) is None:
            parser.error("required command is unavailable: " + command)
    if args.sender is not None:
        sender_worker(args.sender)
        return 0
    args.binary = args.binary.resolve()
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        parser.error("--binary must name an executable udpxy build")
    if args.inside_netns is not None:
        return run_suite(args)
    # Namespace creation happens before any mutation. In particular, failure
    # of unshare (common in restricted containers) leaves host networking alone.
    command = ["unshare", "--net", "--mount", "--propagation", "private",
               "--fork", sys.executable,
               str(Path(__file__).resolve()), "--binary", str(args.binary),
               "--inside-netns", str(netns_id()),
               "--parent-mountns", str(mountns_id())]
    if args.quick:
        command.append("--quick")
    for case in args.case or []:
        command.extend(("--case", case))
    return subprocess.run(command).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        sys.exit(2)
