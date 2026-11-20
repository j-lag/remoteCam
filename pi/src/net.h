/* RemoteCam - network side (UDP discovery + TCP image reception) */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <sys/select.h>

struct net;

struct net *net_create(const char *name, const char *key);
void net_destroy(struct net *n);

/* select() loop integration */
void net_fdset(struct net *n, fd_set *r, fd_set *w, int *maxfd);
void net_handle(struct net *n, fd_set *r, fd_set *w);
void net_periodic(struct net *n);   /* beacon, timeouts */

/* Current video mode requested by the USB host (Teams...): forwarded to the PC */
void net_set_mode(struct net *n, int w, int h, int fps, int streaming, uint32_t max_bytes);

/* Last JPEG frame received (NULL if none). Pointer valid until the next net_handle(). */
const uint8_t *net_frame(struct net *n, size_t *len, int *w, int *h, uint64_t *id, double *t);

int net_connected(struct net *n);
const char *net_peer(struct net *n);

/* Native resolution announced by the PC (returns 1 only once per announcement) */
int net_take_native(struct net *n, int *w, int *h);
