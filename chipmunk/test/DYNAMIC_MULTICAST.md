# Multicast interface address-change regression

`test_dynamic_mcast.py` runs the actual udpxy binary against a UDP multicast
sender. It requires Linux, Python 3, iproute2 (`ip`), util-linux (`unshare`, `mount`),
and root with `CAP_NET_ADMIN` and `CAP_SYS_ADMIN`. A container must allow network
namespace creation; root inside a restricted container is not sufficient.

Build the binary, then run from the repository root:

```sh
make -C chipmunk release
sudo python3 chipmunk/test/test_dynamic_mcast.py --binary chipmunk/udpxy
```

The normal seven-case suite takes approximately 75 seconds, because udpxy's
smallest nonzero `-M` value is 30 seconds. A quick run skips renewal cases and
runs the other five cases:

```sh
sudo python3 chipmunk/test/test_dynamic_mcast.py --binary chipmunk/udpxy --quick
sudo python3 chipmunk/test/test_dynamic_mcast.py --binary chipmunk/udpxy --case renew-ssm
```

Each selected case launches a new foreground udpxy process. The cases verify:

| Case | Multicast mode | Expected result |
| --- | --- | --- |
| `dynamic-asm` | ASM, `-m upx_rx` | Before and after `.2` becomes `.5`, a new HTTP request receives real multicast input without replacing the main process. |
| `dynamic-ssm` | SSM, `-m upx_rx` | The same address-change behavior with an explicit source address. |
| `fixed-asm` | ASM, `-m 198.18.0.2` | Initially succeeds; a new request fails after that literal address is removed. |
| `fixed-ssm` | SSM, `-m 198.18.0.2` | Literal address selection retains the same fixed semantics with SSM. |
| `default-asm` | ASM, no `-m` | The kernel selects the receiving interface from an isolated multicast route; new requests work before and after the IPv4 change. |
| `renew-asm` | ASM, `-m upx_rx -M 30` | An established stream receives data across an address change and renewal; a subsequent new request also works. |
| `renew-ssm` | SSM, `-m upx_rx -M 30` | The same renewal check with an explicit source address. |

The renewal cases deliberately check the original streaming connection as well
as a new request. This tests the multicast drop/add path. It does not promise
that a stream survives a link going down, deletion of the interface, a prolonged
packet outage, or replacement of a network namespace.

To demonstrate the original regression, build an unpatched revision separately
and pass that executable with `--binary`. The same assertions run against both
builds. The unpatched build should fail the two `dynamic-*` cases after the
address change. The two `fixed-*` cases should pass if the optional source parser
is deterministic; an uninitialized optional source or unterminated source string
in the original parser can cause unrelated HTTP 400 failures. A failing suite
exits with status 1 and prints up to 50 diagnostic log lines; setup errors use
status 2. `--quick` includes the dynamic-interface, fixed-address, and
default-interface cases.

The HTTP cases also exercise the existing source-address parser. For a
deterministic comparison, apply the optional-source parser repair from
[PR #42](https://github.com/pcherenkov/udpxy/pull/42) to both the control and
interface-patched builds. That repair is deliberately not part of this patch.

Source filtering and simultaneous delivery to multiple clients are covered by
[PR #43](https://github.com/pcherenkov/udpxy/pull/43) and its direct socket
regression. They are outside this address-change suite and are not included in
this branch.

## Network isolation

The script first re-executes itself in fresh network and private mount
namespaces, verifies that both differ from its parent, and checks that only
loopback exists. A temporary directory is bind-mounted over `/var/run` inside
the private mount namespace, so udpxy's default PID lock cannot collide with
real services or parallel tests. This covers the default Linux `HAS_VARRUN`
build, not a custom `PIDFILE_DIR` pointing elsewhere. The mount uses
`--no-mtab` to avoid writing a shared `/etc/mtab`. Only then does it create the receiver's
`upx_rx` veth interface. The
sender runs in a second fresh, unnamed namespace, joined only by the veth peer
`upx_tx`. No bridge, real NIC, multicast router, DHCP server, named global
namespace, or host route is used. These namespaces disappear after their
processes exit.

`198.18.0.0/24` is used only inside the test namespaces; the receiver addresses
are configured as `/32` so Linux's secondary-address promotion cannot obscure
the intended remove/add operation. An isolated route to the sender gives reverse
path filtering a valid return path. HTTP listens only on the isolated loopback
interface, and the sender uses TTL 1 with local multicast loopback disabled.
The multicast groups and ports are local test fixtures, not real IPTV channels.
Only the `default-asm` case adds a multicast route, inside the receiver namespace,
and removes it at the end of that case.

All managed process groups are terminated on normal exit or test failure. Do
not pass the hidden `--inside-netns`, `--parent-mountns` or `--sender` arguments
manually; they are
used only for the isolation handshake.

## Test limits and timing

The sender emits marked MPEG-TS null packets continuously, so this checks
transport and payload identity rather than playable audio/video or decoding.
HTTP reads have a three-second timeout. Severe CPU starvation can therefore
produce a timeout; rerun on an otherwise idle Linux test machine before treating
one as a code regression. The two namespaces avoid dependence on an external
IGMP querier, firewall, NIC driver, or operator network.

Renewal is checked by leaving the stream open for 35 seconds, beyond the minimum
30-second refresh interval, with continuous payload consumption. The test checks
observable delivery rather than syscall tracing; `strace` can be added manually
when examining exact membership calls. Interface-index reuse and hotplug remain
separate scenarios.

The status page continues to display the IPv4 snapshot resolved at startup;
it is not an indication of the interface selected for a later stream. The patch
also retains the startup requirement that a named interface have an IPv4 address.
These Linux tests exercise interface-index membership. On platforms using the
IPv4-address fallback, refreshing the address for a new request does not imply
that an existing stream's renewal survives an address change.
