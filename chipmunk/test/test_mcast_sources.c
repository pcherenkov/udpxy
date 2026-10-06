/* Regression: shared multicast ports must preserve each socket's source filter.
 * Build from the repository root (Linux):
 * cc -D_DEFAULT_SOURCE -std=c99 -Wall -Wextra -Ichipmunk \
 *   chipmunk/test/test_mcast_sources.c chipmunk/netop.c chipmunk/util.c \
 *   chipmunk/extrn.c -o /tmp/test_mcast_sources
 * Run in an isolated network namespace:
 * sudo unshare -n sh chipmunk/test/run_mcast_sources.sh /tmp/test_mcast_sources
 */

#include <arpa/inet.h>
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "netop.h"
#include "uopt.h"

#define READERS 3
#define PACKETS 64
#define SOURCE_IP "198.18.0.1"
#define RECEIVER_IP "198.18.0.2"
#define GROUP_IP "232.255.123.45"
#define CONTROL_PORT 5501

extern FILE* g_flog; /* defined in extrn.c */
struct udpxy_opt g_uopt;

static void
address( struct sockaddr_in* addr, const char* ip, int port )
{
    memset( addr, 0, sizeof(*addr) );
    addr->sin_family = AF_INET;
    addr->sin_port = htons(port);
    (void) inet_pton( AF_INET, ip, &addr->sin_addr );
}

static long
milliseconds( void )
{
    struct timespec now;
    (void) clock_gettime( CLOCK_MONOTONIC, &now );
    return now.tv_sec * 1000L + now.tv_nsec / 1000000L;
}

static int
request( int fd, struct sockaddr_in* target, char command, int timeout )
{
    struct pollfd input = { fd, POLLIN, 0 };
    long deadline = milliseconds() + timeout;
    char reply;
    int rc, remaining;

    if( sendto( fd, &command, 1, 0, (struct sockaddr*)target, sizeof(*target) ) != 1 )
        return -1;
    while( milliseconds() < deadline ) {
        remaining = (int)(deadline - milliseconds());
        if( remaining <= 0 ) break;
        rc = poll( &input, 1, remaining );
        if( rc < 0 && errno == EINTR ) continue;
        if( rc <= 0 ) break;
        if( recv( fd, &reply, 1, MSG_DONTWAIT ) == 1 && reply == command ) return 0;
    }
    errno = ETIMEDOUT;
    return -1;
}

static int
sender_main( void )
{
    const char payload[] = "udpxy multicast source regression";
    struct sockaddr_in local, group, client;
    struct timeval timeout = { 5, 0 };
    socklen_t length;
    int fd, sender, attempt, rc = 1;
    char command;

    address( &local, SOURCE_IP, CONTROL_PORT );
    address( &group, GROUP_IP, 5500 );
    fd = socket( AF_INET, SOCK_DGRAM, 0 );
    if( fd < 0 ) { perror("control socket"); return 1; }
    /* The fixture moves and configures our veth after entering this namespace. */
    for( attempt = 0; attempt < 250; ++attempt ) {
        if( !bind( fd, (struct sockaddr*)&local, sizeof(local) ) ) break;
        if( errno != EADDRNOTAVAIL ) goto done;
        (void) usleep(20000);
    }
    if( attempt == 250 ||
        setsockopt( fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout) ) ) goto done;
    local.sin_port = 0;
    for( ;; ) {
        length = sizeof(client);
        if( recvfrom( fd, &command, 1, 0, (struct sockaddr*)&client, &length ) != 1 )
            goto done;
        if( command == 'P' ) {
            sender = socket( AF_INET, SOCK_DGRAM, 0 );
            if( sender < 0 ) goto done;
            if( bind( sender, (struct sockaddr*)&local, sizeof(local) ) ||
                setsockopt( sender, IPPROTO_IP, IP_MULTICAST_IF,
                            &local.sin_addr, sizeof(local.sin_addr) ) ||
                sendto( sender, payload, sizeof(payload), 0,
                        (struct sockaddr*)&group, sizeof(group) ) != sizeof(payload) ) {
                (void) close(sender);
                goto done;
            }
            (void) close(sender);
        }
        if( sendto( fd, &command, 1, 0, (struct sockaddr*)&client, length ) != 1 )
            goto done;
    }
done:
    perror("sender");
    (void) close(fd);
    return rc;
}

static int
collect( struct pollfd* readers, int* received, int timeout )
{
    char payload[128];
    int i, rc;
    ssize_t count;

    do rc = poll( readers, READERS, timeout ); while( rc < 0 && errno == EINTR );
    if( rc < 0 ) { perror("poll"); return -1; }
    for( i = 0; i < READERS; ++i ) {
        if( readers[i].revents & (POLLERR | POLLHUP | POLLNVAL) ) {
            (void) fprintf( stderr, "receiver %d: poll error\n", i );
            return -1;
        }
        if( !(readers[i].revents & POLLIN) ) continue;
        do {
            count = recv( readers[i].fd, payload, sizeof(payload), MSG_DONTWAIT );
            if( count >= 0 && ++received[i] > PACKETS ) {
                (void) fprintf( stderr, "receiver %d: unexpected extra packets\n", i );
                return -1;
            }
        } while( count >= 0 || errno == EINTR );
        if( errno != EAGAIN && errno != EWOULDBLOCK ) {
            perror("recv");
            return -1;
        }
    }
    return 0;
}

int
main( int argc, char** argv )
{
    const char* sources[READERS] = { "198.18.0.99", SOURCE_IP, SOURCE_IP };
    struct sockaddr_in group, source[READERS], local, control_addr;
#ifdef UDPXY_MCAST_IFINDEX
    struct mcast_iface interface;
#else
    struct in_addr interface;
#endif
    struct pollfd readers[READERS];
    int received[READERS] = { 0, 0, 0 };
    int control = -1, failed = 0, i, packet, phase;

    if( argc == 2 && !strcmp( argv[1], "--sender" ) ) return sender_main();
    if( argc != 1 ) return 2;

    g_flog = stderr;
    memset( source, 0, sizeof(source) );
    memset( readers, 0, sizeof(readers) );
    for( i = 0; i < READERS; ++i ) readers[i].fd = -1;
    address( &group, GROUP_IP, 5500 );
    address( &local, RECEIVER_IP, 0 );
    address( &control_addr, SOURCE_IP, CONTROL_PORT );
#ifdef UDPXY_MCAST_IFINDEX
    memset( &interface, 0, sizeof(interface) );
    interface.addr = local.sin_addr; /* index zero exercises the address path */
#else
    interface = local.sin_addr;
#endif
    control = socket( AF_INET, SOCK_DGRAM, 0 );
    if( control < 0 || bind( control, (struct sockaddr*)&local, sizeof(local) ) ) {
        perror("receiver control");
        failed = 1;
        goto done;
    }
    for( i = 0; i < 100; ++i )
        if( !request( control, &control_addr, 'R', 50 ) ) break;
    if( i == 100 ) { perror("sender readiness"); failed = 1; goto done; }

    /* Join the nonexistent source first, before any real-source membership. */
    for( i = 0; i < READERS - 1; ++i ) {
        source[i].sin_family = AF_INET;
        (void) inet_pton( AF_INET, sources[i], &source[i].sin_addr );
        if( setup_mcast_listener( &source[i], &group, &interface,
                                  &readers[i].fd, 65536 ) ) {
            (void) fprintf( stderr, "receiver %d setup failed\n", i );
            failed = 1;
            goto done;
        }
        readers[i].events = POLLIN;
    }

    /* A unique matching socket exposes early-demux/reuseport selection;
     * a second matching socket separately checks concurrent delivery.
     */
    for( phase = 0; phase < 2; ++phase ) {
        if( phase ) {
            source[2].sin_family = AF_INET;
            (void) inet_pton( AF_INET, sources[2], &source[2].sin_addr );
            if( setup_mcast_listener( &source[2], &group, &interface,
                                      &readers[2].fd, 65536 ) ) {
                (void) fprintf( stderr, "second correct receiver setup failed\n" );
                failed = 1;
                goto done;
            }
            readers[2].events = POLLIN;
        }
        /* Each phase starts with empty queues and independent counters. */
        memset( received, 0, sizeof(received) );
        for( packet = 0; packet < PACKETS; ++packet ) {
            /* Different source ports exercise reuseport selection across flows. */
            if( request( control, &control_addr, 'P', 1000 ) ) {
                perror("send packet control");
                failed = 1;
                goto done;
            }
            if( collect( readers, received, 20 ) ) {
                failed = 1;
                goto done;
            }
        }
        if( collect( readers, received, 100 ) ) failed = 1;
        for( i = 0; i < READERS; ++i ) {
            int expected = i && (i == 1 || phase) ? PACKETS : 0;
            if( received[i] != expected ) {
                (void) fprintf( stderr, "phase %d receiver %d (%s): expected %d, received %d\n",
                                phase + 1, i, sources[i], expected, received[i] );
                ++failed;
            }
        }
    }

done:
    if( control >= 0 ) (void) close(control);
    for( i = 0; i < READERS; ++i ) {
        if( readers[i].fd >= 0 )
            close_mcast_listener( readers[i].fd, &interface, &source[i].sin_addr );
    }
    if( failed ) return 1;
    (void) printf("multicast sources: filtering and concurrent delivery passed (%d packets each)\n",
                 PACKETS);
    return 0;
}
