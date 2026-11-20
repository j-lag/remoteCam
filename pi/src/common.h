/* RemoteCam - common definitions */
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <time.h>

#define RC_UDP_PORT 47810   /* discovery (beacon / "RCAM?" query) */
#define RC_TCP_PORT 47811   /* image stream */
#define RC_VERSION  "1.0"

#define MAX_FRAMES 16

struct frame_desc {
    int index;   /* USB bFrameIndex (1..n) */
    int width;
    int height;
};

static inline double mono_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

#define LOG(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } while (0)
