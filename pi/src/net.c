/*
 * RemoteCam - network side.
 *
 * Discovery: every second the Pi broadcasts a JSON beacon over UDP
 * (port 47810) and answers "RCAM?" queries from the PC directly.
 *
 * Stream (TCP 47811), PC -> Pi: packets [type:4][length:u32 LE][data]
 *   "HELO" : JSON {"key":"...","native":[w,h],"client":"..."}
 *   "INFO" : JSON {"native":[w,h]}
 *   "JPEG" : [w:u16 LE][h:u16 LE][JPEG data]
 * Pi -> PC: JSON lines terminated by '\n'
 *   {"type":"mode","w":..,"h":..,"fps":..,"streaming":true,"max_bytes":..}
 *   {"type":"ack"}  (one per received frame: flow control)
 *   {"type":"error","msg":"..."}
 */
#define _GNU_SOURCE
#include "net.h"
#include "common.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define TX_CAP      8192
#define MAX_PAYLOAD (48u << 20)

struct net {
    char name[64];
    char key[128];
    int udp, lsn, cli;
    char peer[64];
    double cli_since;

    /* receive */
    uint8_t hdr[8];
    size_t hdr_got;
    int in_payload;
    char ptype[4];
    uint32_t plen;
    size_t pgot;
    uint8_t *pbuf;
    size_t pcap;
    int authed;

    /* transmit */
    char tx[TX_CAP];
    size_t txlen;

    /* last complete frame */
    uint8_t *fbuf;
    size_t fcap, flen;
    int fw, fh;
    uint64_t fid;
    double ftime;

    /* current mode */
    int mw, mh, mfps, mstream;
    uint32_t mmax;

    int nat_w, nat_h, nat_new;
    double last_beacon;

    /* reception statistics (reset by net_rx_stats) */
    unsigned rx_frames;
    uint64_t rx_bytes;
};

/* ---------- mini JSON (known format, no need for a real parser) ---------- */

static const char *json_find(const char *js, const char *key)
{
    char pat[64];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(js, pat);
    if (!p)
        return NULL;
    p += strlen(pat);
    while (*p == ' ' || *p == '\t' || *p == ':')
        p++;
    return p;
}

static int json_str(const char *js, const char *key, char *out, size_t cap)
{
    const char *p = json_find(js, key);
    if (!p || *p != '"')
        return 0;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < cap) {
        if (*p == '\\' && p[1])
            p++;
        out[i++] = *p++;
    }
    out[i] = 0;
    return 1;
}

static int json_int2(const char *js, const char *key, int *a, int *b)
{
    const char *p = json_find(js, key);
    if (!p || *p != '[')
        return 0;
    return sscanf(p, "[ %d , %d", a, b) == 2;
}

/* ---------- transmit ---------- */

static void close_client(struct net *n);

static void cli_flush(struct net *n)
{
    while (n->cli >= 0 && n->txlen) {
        ssize_t r = send(n->cli, n->tx, n->txlen, MSG_NOSIGNAL);
        if (r < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                return;
            LOG("net: send to %s failed: %s", n->peer, strerror(errno));
            close_client(n);
            return;
        }
        memmove(n->tx, n->tx + r, n->txlen - r);
        n->txlen -= r;
    }
}

static void cli_send(struct net *n, const char *s)
{
    size_t l = strlen(s);
    if (n->cli < 0)
        return;
    if (n->txlen + l > TX_CAP) {
        LOG("net: transmit buffer full, message dropped");
        return;
    }
    memcpy(n->tx + n->txlen, s, l);
    n->txlen += l;
    cli_flush(n);
}

static void send_mode(struct net *n)
{
    char b[256];
    snprintf(b, sizeof b,
             "{\"type\":\"mode\",\"w\":%d,\"h\":%d,\"fps\":%d,\"streaming\":%s,\"max_bytes\":%u}\n",
             n->mw, n->mh, n->mfps, n->mstream ? "true" : "false", n->mmax);
    cli_send(n, b);
}

/* ---------- discovery ---------- */

static int beacon_json(struct net *n, char *buf, size_t cap)
{
    return snprintf(buf, cap,
                    "{\"proto\":\"remotecam\",\"v\":1,\"version\":\"%s\",\"name\":\"%s\",\"port\":%d,"
                    "\"auth\":%s,\"busy\":%s,\"streaming\":%s,\"w\":%d,\"h\":%d,\"fps\":%d}",
                    RC_VERSION, n->name, RC_TCP_PORT, n->key[0] ? "true" : "false",
                    n->cli >= 0 ? "true" : "false", n->mstream ? "true" : "false",
                    n->mw, n->mh, n->mfps);
}

static void send_beacon(struct net *n, const struct sockaddr_in *to)
{
    char b[512];
    int l = beacon_json(n, b, sizeof b);
    if (n->udp >= 0 && l > 0)
        sendto(n->udp, b, l, 0, (const struct sockaddr *)to, sizeof *to);
}

static void udp_read(struct net *n)
{
    for (int i = 0; i < 16; i++) {
        char b[256];
        struct sockaddr_in from;
        socklen_t fl = sizeof from;
        ssize_t r = recvfrom(n->udp, b, sizeof b - 1, 0, (struct sockaddr *)&from, &fl);
        if (r < 0)
            return;
        if (r >= 5 && memcmp(b, "RCAM?", 5) == 0)
            send_beacon(n, &from);
    }
}

/* ---------- TCP client ---------- */

static void reset_rx(struct net *n)
{
    n->hdr_got = 0;
    n->in_payload = 0;
    n->pgot = 0;
    n->plen = 0;
}

static void close_client(struct net *n)
{
    if (n->cli < 0)
        return;
    LOG("net: client %s disconnected", n->peer);
    close(n->cli);
    n->cli = -1;
    n->authed = 0;
    n->txlen = 0;
    reset_rx(n);
}

static void accept_client(struct net *n)
{
    struct sockaddr_in a;
    socklen_t al = sizeof a;
    int fd = accept4(n->lsn, (struct sockaddr *)&a, &al, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (fd < 0)
        return;
    if (n->cli >= 0) {
        LOG("net: new client, replacing the previous one (%s)", n->peer);
        cli_send(n, "{\"type\":\"error\",\"msg\":\"replaced\"}\n");
        close_client(n);
    }
    int one = 1, idle = 5, intvl = 2, cnt = 3;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof idle);
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof intvl);
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof cnt);
    n->cli = fd;
    n->cli_since = mono_now();
    inet_ntop(AF_INET, &a.sin_addr, n->peer, sizeof n->peer);
    n->authed = 0;
    n->txlen = 0;
    reset_rx(n);
    LOG("net: connection from %s", n->peer);
}

static int ensure_cap(uint8_t **buf, size_t *cap, size_t need)
{
    if (*cap >= need)
        return 0;
    size_t nc = need + need / 4 + 4096;
    uint8_t *nb = realloc(*buf, nc);
    if (!nb)
        return -1;
    *buf = nb;
    *cap = nc;
    return 0;
}

static void parse_native(struct net *n, const char *js)
{
    int w, h;
    if (json_int2(js, "native", &w, &h) && w > 0 && h > 0) {
        n->nat_w = w;
        n->nat_h = h;
        n->nat_new = 1;
    }
}

static void handle_packet(struct net *n)
{
    if (!memcmp(n->ptype, "HELO", 4)) {
        n->pbuf[n->plen] = 0;
        char key[128] = "", client[64] = "?";
        json_str((char *)n->pbuf, "key", key, sizeof key);
        json_str((char *)n->pbuf, "client", client, sizeof client);
        if (n->key[0] && strcmp(key, n->key) != 0) {
            LOG("net: key rejected for %s", n->peer);
            cli_send(n, "{\"type\":\"error\",\"msg\":\"bad_key\"}\n");
            close_client(n);
            return;
        }
        n->authed = 1;
        parse_native(n, (char *)n->pbuf);
        LOG("net: client %s (%s) accepted, native screen %dx%d", n->peer, client, n->nat_w, n->nat_h);
        send_mode(n);
    } else if (!memcmp(n->ptype, "INFO", 4)) {
        if (!n->authed)
            return;
        n->pbuf[n->plen] = 0;
        parse_native(n, (char *)n->pbuf);
    } else if (!memcmp(n->ptype, "JPEG", 4)) {
        if (!n->authed || n->plen < 8)
            return;
        /* swap buffers: no copy */
        uint8_t *tb = n->fbuf;
        size_t tc = n->fcap;
        n->fbuf = n->pbuf;
        n->fcap = n->pcap;
        n->pbuf = tb;
        n->pcap = tc;
        n->flen = n->plen;
        n->fw = n->fbuf[0] | (n->fbuf[1] << 8);
        n->fh = n->fbuf[2] | (n->fbuf[3] << 8);
        n->fid++;
        n->ftime = mono_now();
        n->rx_frames++;
        n->rx_bytes += n->plen;
        cli_send(n, "{\"type\":\"ack\"}\n");
    }
    /* unknown types: ignored (forward compatibility) */
}

static void cli_read(struct net *n)
{
    for (int iter = 0; iter < 64 && n->cli >= 0; iter++) {
        ssize_t r;
        if (!n->in_payload) {
            r = recv(n->cli, n->hdr + n->hdr_got, 8 - n->hdr_got, 0);
        } else {
            r = recv(n->cli, n->pbuf + n->pgot, n->plen - n->pgot, 0);
        }
        if (r == 0) {
            close_client(n);
            return;
        }
        if (r < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                return;
            LOG("net: read %s: %s", n->peer, strerror(errno));
            close_client(n);
            return;
        }
        if (!n->in_payload) {
            n->hdr_got += r;
            if (n->hdr_got < 8)
                continue;
            memcpy(n->ptype, n->hdr, 4);
            n->plen = n->hdr[4] | (n->hdr[5] << 8) | (n->hdr[6] << 16) | ((uint32_t)n->hdr[7] << 24);
            n->hdr_got = 0;
            if (n->plen > MAX_PAYLOAD) {
                LOG("net: packet too large (%u bytes), disconnecting", n->plen);
                close_client(n);
                return;
            }
            if (ensure_cap(&n->pbuf, &n->pcap, (size_t)n->plen + 1) < 0) {
                LOG("net: out of memory");
                close_client(n);
                return;
            }
            n->pgot = 0;
            n->in_payload = 1;
            if (n->plen == 0) {
                n->in_payload = 0;
                handle_packet(n);
            }
        } else {
            n->pgot += r;
            if (n->pgot == n->plen) {
                n->in_payload = 0;
                handle_packet(n);
            }
        }
    }
}

/* ---------- API ---------- */

static void sanitize(char *s)
{
    for (; *s; s++) {
        unsigned char c = *s;
        if (c < 32 || c == '"' || c == '\\' || c > 126)
            *s = '_';
    }
}

struct net *net_create(const char *name, const char *key)
{
    struct net *n = calloc(1, sizeof *n);
    if (!n)
        return NULL;
    snprintf(n->name, sizeof n->name, "%s", name);
    snprintf(n->key, sizeof n->key, "%s", key ? key : "");
    sanitize(n->name);
    n->cli = -1;
    n->mw = 1920;
    n->mh = 1080;
    n->mfps = 30;

    int one = 1;
    n->udp = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (n->udp >= 0) {
        setsockopt(n->udp, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
        setsockopt(n->udp, SOL_SOCKET, SO_BROADCAST, &one, sizeof one);
        struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(RC_UDP_PORT),
                                 .sin_addr.s_addr = htonl(INADDR_ANY) };
        if (bind(n->udp, (struct sockaddr *)&a, sizeof a) < 0) {
            LOG("net: bind UDP %d: %s (discovery disabled)", RC_UDP_PORT, strerror(errno));
            close(n->udp);
            n->udp = -1;
        }
    }

    n->lsn = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (n->lsn < 0) {
        free(n);
        return NULL;
    }
    setsockopt(n->lsn, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(RC_TCP_PORT),
                             .sin_addr.s_addr = htonl(INADDR_ANY) };
    if (bind(n->lsn, (struct sockaddr *)&a, sizeof a) < 0 || listen(n->lsn, 2) < 0) {
        LOG("net: bind/listen TCP %d: %s", RC_TCP_PORT, strerror(errno));
        close(n->lsn);
        if (n->udp >= 0)
            close(n->udp);
        free(n);
        return NULL;
    }
    LOG("net: listening (UDP %d, TCP %d), name \"%s\"%s", RC_UDP_PORT, RC_TCP_PORT, n->name,
        n->key[0] ? ", key required" : "");
    return n;
}

void net_destroy(struct net *n)
{
    if (!n)
        return;
    close_client(n);
    if (n->udp >= 0)
        close(n->udp);
    close(n->lsn);
    free(n->pbuf);
    free(n->fbuf);
    free(n);
}

void net_fdset(struct net *n, fd_set *r, fd_set *w, int *maxfd)
{
#define ADD(fd, set) do { FD_SET(fd, set); if ((fd) > *maxfd) *maxfd = (fd); } while (0)
    if (n->udp >= 0)
        ADD(n->udp, r);
    ADD(n->lsn, r);
    if (n->cli >= 0) {
        ADD(n->cli, r);
        if (n->txlen)
            ADD(n->cli, w);
    }
#undef ADD
}

void net_handle(struct net *n, fd_set *r, fd_set *w)
{
    if (n->udp >= 0 && FD_ISSET(n->udp, r))
        udp_read(n);
    int cli = n->cli;
    if (cli >= 0 && FD_ISSET(cli, w))
        cli_flush(n);
    if (cli >= 0 && n->cli == cli && FD_ISSET(cli, r))
        cli_read(n);
    if (FD_ISSET(n->lsn, r))
        accept_client(n);
}

void net_periodic(struct net *n)
{
    double now = mono_now();
    if (now - n->last_beacon >= 1.0) {
        n->last_beacon = now;
        struct sockaddr_in to = { .sin_family = AF_INET, .sin_port = htons(RC_UDP_PORT),
                                  .sin_addr.s_addr = htonl(INADDR_BROADCAST) };
        send_beacon(n, &to);
    }
    if (n->cli >= 0 && !n->authed && now - n->cli_since > 10.0) {
        LOG("net: %s sent no HELO, disconnecting", n->peer);
        close_client(n);
    }
    if (n->cli >= 0 && n->txlen)
        cli_flush(n);
}

void net_set_mode(struct net *n, int w, int h, int fps, int streaming, uint32_t max_bytes)
{
    n->mw = w;
    n->mh = h;
    n->mfps = fps;
    n->mstream = streaming;
    n->mmax = max_bytes;
    if (n->cli >= 0 && n->authed)
        send_mode(n);
}

const uint8_t *net_frame(struct net *n, size_t *len, int *w, int *h, uint64_t *id, double *t)
{
    if (!n->fbuf || n->flen < 8)
        return NULL;
    *len = n->flen - 4;
    *w = n->fw;
    *h = n->fh;
    *id = n->fid;
    *t = n->ftime;
    return n->fbuf + 4;
}

int net_connected(struct net *n)
{
    return n->cli >= 0 && n->authed;
}

const char *net_peer(struct net *n)
{
    return n->cli >= 0 ? n->peer : "";
}

int net_take_native(struct net *n, int *w, int *h)
{
    if (!n->nat_new)
        return 0;
    n->nat_new = 0;
    *w = n->nat_w;
    *h = n->nat_h;
    return 1;
}

void net_rx_stats(struct net *n, unsigned *frames, uint64_t *bytes)
{
    *frames = n->rx_frames;
    *bytes = n->rx_bytes;
    n->rx_frames = 0;
    n->rx_bytes = 0;
}

void net_send_line(struct net *n, const char *line)
{
    if (n->cli >= 0 && n->authed)
        cli_send(n, line);
}
