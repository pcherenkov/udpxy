/* @(#) regression test for parse_udprelay
 *
 * Run from the repository root:
 * cc -std=c99 -Wall -Wextra -Werror -Ichipmunk \
 *   chipmunk/test/test_udprelay.c chipmunk/rparse.c -o /tmp/test_udprelay
 * /tmp/test_udprelay
 */

#include <stdio.h>
#include <string.h>
#include <netinet/in.h>

#include "rparse.h"

static int
check_asm( void )
{
    const char opt[] = "239.1.2.3:5000";
    char source[ INET_ADDRSTRLEN ];
    char group[ INET_ADDRSTRLEN ];
    uint16_t port = 0;
    int rc;

    (void) memset( source, 'X', sizeof(source) );
    (void) memset( group, 'X', sizeof(group) );
    rc = parse_udprelay( opt, sizeof(opt), source, sizeof(source),
                        group, sizeof(group), &port );
    if( rc || source[0] != '\0' ||
        memcmp( group, "239.1.2.3", sizeof("239.1.2.3") ) ||
        port != 5000 ) {
        (void) fprintf( stderr, "ASM must clear the source buffer\n" );
        return 1;
    }

    return 0;
}

static int
check_ssm( size_t source_size )
{
    const char opt[] = "192.0.2.1@232.1.2.3:5000";
    const char expected[] = "192.0.2.1";
    char source[ INET_ADDRSTRLEN ];
    char group[ INET_ADDRSTRLEN ];
    uint16_t port = 0;
    int rc;

    (void) memset( source, 'X', sizeof(source) );
    (void) memset( group, 'X', sizeof(group) );
    rc = parse_udprelay( opt, sizeof(opt), source, source_size,
                        group, sizeof(group), &port );
    if( rc || memcmp( source, expected, sizeof(expected) ) ||
        memcmp( group, "232.1.2.3", sizeof("232.1.2.3") ) ||
        port != 5000 ) {
        (void) fprintf( stderr,
                        "SSM source must be exact and NUL-terminated "
                        "(buffer size %lu)\n", (unsigned long)source_size );
        return 1;
    }

    return 0;
}

static int
check_source_overflow( size_t source_size )
{
    const char opt[] = "192.0.2.1@232.1.2.3:5000";
    char source[ INET_ADDRSTRLEN ];
    char group[ INET_ADDRSTRLEN ];
    uint16_t port = 0;
    size_t i;
    int rc;

    (void) memset( source, 'X', sizeof(source) );
    (void) memset( group, 'X', sizeof(group) );
    rc = parse_udprelay( opt, sizeof(opt), source, source_size,
                        group, sizeof(group), &port );
    if( !rc ) {
        (void) fprintf( stderr,
                        "Oversized SSM source must be rejected "
                        "(buffer size %lu)\n", (unsigned long)source_size );
        return 1;
    }

    for( i = source_size; i < sizeof(source); ++i ) {
        if( source[i] != 'X' ) {
            (void) fprintf( stderr, "SSM source buffer overflow\n" );
            return 1;
        }
    }

    return 0;
}

int
main( void )
{
    int failed = 0;

    failed += check_asm();
    failed += check_ssm( INET_ADDRSTRLEN );
    failed += check_ssm( sizeof("192.0.2.1") );
    failed += check_source_overflow( 8 );
    failed += check_source_overflow( sizeof("192.0.2.1") - 1 );

    if( failed ) return 1;

    (void) printf( "parse_udprelay: 5 regression cases passed\n" );
    return 0;
}

/* __EOF__ */
