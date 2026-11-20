/*
 * RemoteCam - uvc-bridge
 *
 * Userspace side of the UVC gadget (f_uvc, configfs) on the Raspberry Pi Zero:
 *  - answers the host's UVC control requests (PROBE/COMMIT negotiation),
 *  - pushes the MJPEG frames received from the PC (via net.c) into V4L2 buffers,
 *  - shows a placeholder picture (IP, name...) while the PC sends nothing.
 *
 * The USB gadget is created beforehand by gadget.sh, which writes
 * /run/remotecam/uvc.info (streaming interface, resolutions, intervals).
 *
 * Environment variables:
 *   RC_NAME    name announced on the network (default "RemoteCam")
 *   RC_KEY     optional shared key
 *   RC_NATIVE  "auto": adds the PC native resolution to the gadget
 *   RC_INFO    path of the uvc.info file
 *   RC_DEVICE  forces the gadget /dev/videoN
 *   RC_DEBUG   verbose logging of UVC requests
 */
#define _GNU_SOURCE
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/timerfd.h>
#include <unistd.h>

#include <linux/videodev2.h>
#include <linux/usb/ch9.h>
#include <linux/usb/video.h>
#include <linux/usb/g_uvc.h>

#include "common.h"
#include "net.h"
#include "placeholder.h"

#define NBUF            3
#define MAX_QUEUED      2
#define MAX_INTERVALS   8
#define NATIVE_FILE     "/var/lib/remotecam/native.txt"

struct buf {
    void *mem;
    size_t len;
    size_t used;
    int queued;
    uint64_t fid;
};

struct app {
    int vfd, tfd;
    struct net *net;
    char name[64];

    struct frame_desc frames[MAX_FRAMES];
    int nframes, def_frame;
    uint32_t intervals[MAX_INTERVALS];
    int nintervals;
    int vs_intf;
    uint32_t maxpayload;

    struct uvc_streaming_control probe, commit;
    int set_cur_target;   /* 0 none, 1 probe, 2 commit, -1 VC control */
    uint8_t vc_error;

    int cur;              /* index in frames[] */
    uint32_t cur_interval;
    uint32_t sizeimage;

    int streaming;
    struct buf bufs[NBUF];
    int nbufs;

    uint8_t *ph;
    unsigned long ph_len;
    int ph_w, ph_h, ph_connected;
    double ph_time;
    uint64_t ph_gen;

    int native_auto, pend_nw, pend_nh;

    unsigned st_frames, st_pc;
    double st_time;

    /* bottleneck diagnostics, reported to the PC every 2 s */
    unsigned rp_ticks, rp_frames, rp_new, rp_busy, rp_copies;
    double rp_copy, rp_time;
    uint64_t last_pc_fid;
    double last_queued;
    unsigned long long cpu_total, cpu_idle, cpu_soft, cpu_self;
};

static volatile sig_atomic_t g_running = 1;
static int g_debug;

static void on_signal(int s)
{
    (void)s;
    g_running = 0;
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/* Maximum JPEG size of a frame: the value declared by gadget.sh (camera.frame_bpp),
 * or 8 bits/pixel for an old uvc.info. */
static size_t frame_bufsize(const struct frame_desc *f)
{
    if (f->size)
        return f->size;
    size_t s = (size_t)f->width * f->height;
    return s < 524288 ? 524288 : s;
}

static int fps_of(uint32_t ival)
{
    return ival ? (int)((10000000u + ival / 2) / ival) : 30;
}

static int find_frame(struct app *a, int bidx)
{
    for (int i = 0; i < a->nframes; i++)
        if (a->frames[i].index == bidx)
            return i;
    return -1;
}

/* Intervals offered for a resolution (gadget.sh limits the frame rate of each one to
 * what the Pi can sustain), sorted ascending (fps descending). */
static const uint32_t *frame_intervals(struct app *a, int fi, int *n)
{
    if (fi >= 0 && fi < a->nframes && a->frames[fi].nivals > 0) {
        *n = a->frames[fi].nivals;
        return a->frames[fi].ivals;
    }
    *n = a->nintervals;
    return a->intervals;
}

static uint32_t clamp_interval(struct app *a, int fi, uint32_t req)
{
    int n;
    const uint32_t *iv = frame_intervals(a, fi, &n);
    if (req == 0)
        return iv[0];
    for (int i = 0; i < n; i++)
        if (iv[i] + 100 >= req)
            return iv[i];
    return iv[n - 1];
}

static void get_ip(char *out, size_t cap)
{
    struct ifaddrs *ifa, *p;
    snprintf(out, cap, "%s", "");
    if (getifaddrs(&ifa) < 0)
        return;
    for (p = ifa; p; p = p->ifa_next) {
        if (!p->ifa_addr || p->ifa_addr->sa_family != AF_INET || (p->ifa_flags & IFF_LOOPBACK))
            continue;
        inet_ntop(AF_INET, &((struct sockaddr_in *)p->ifa_addr)->sin_addr, out, cap);
        break;
    }
    freeifaddrs(ifa);
}

static int load_info(struct app *a, const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    char line[512];
    int def_index = -1;
    while (fgets(line, sizeof line, f)) {
        int i, w, h, pos = 0;
        unsigned v, size = 0;
        if (sscanf(line, "frame %d %d %d %u%n", &i, &w, &h, &size, &pos) >= 3) {
            if (a->nframes < MAX_FRAMES && w > 0 && h > 0) {
                struct frame_desc *fd = &a->frames[a->nframes];
                memset(fd, 0, sizeof *fd);
                fd->index = i;
                fd->width = w;
                fd->height = h;
                fd->size = size;
                /* optional per-resolution intervals after the size */
                char *p = pos ? line + pos : NULL, *end;
                while (p && fd->nivals < (int)(sizeof fd->ivals / sizeof fd->ivals[0])) {
                    unsigned long x = strtoul(p, &end, 10);
                    if (end == p)
                        break;
                    if (x > 0)
                        fd->ivals[fd->nivals++] = (uint32_t)x;
                    p = end;
                }
                a->nframes++;
            }
        } else if (sscanf(line, "vs_intf %d", &i) == 1) {
            a->vs_intf = i;
        } else if (sscanf(line, "maxpayload %u", &v) == 1) {
            a->maxpayload = v;
        } else if (sscanf(line, "default %d", &i) == 1) {
            def_index = i;
        } else if (strncmp(line, "intervals", 9) == 0) {
            char *p = line + 9, *end;
            while (a->nintervals < MAX_INTERVALS) {
                unsigned long x = strtoul(p, &end, 10);
                if (end == p)
                    break;
                if (x > 0)
                    a->intervals[a->nintervals++] = (uint32_t)x;
                p = end;
            }
        }
    }
    fclose(f);
    a->def_frame = 0;
    if (def_index > 0 && find_frame(a, def_index) >= 0)
        a->def_frame = find_frame(a, def_index);
    return 0;
}

/* ------------------------------------------------------------------ */
/* Format / UVC negotiation                                            */
/* ------------------------------------------------------------------ */

static void fill_ctrl(struct app *a, struct uvc_streaming_control *c, int fi, uint32_t ival)
{
    memset(c, 0, sizeof *c);
    c->bmHint = 1;
    c->bFormatIndex = 1;
    c->bFrameIndex = a->frames[fi].index;
    c->dwFrameInterval = ival;
    c->dwMaxVideoFrameSize = frame_bufsize(&a->frames[fi]);
    c->dwMaxPayloadTransferSize = a->maxpayload;
    c->bmFramingInfo = 3;
    c->bPreferedVersion = 1;
    c->bMaxVersion = 1;
}

static void set_format(struct app *a)
{
    const struct frame_desc *f = &a->frames[a->cur];
    struct v4l2_format fmt;
    memset(&fmt, 0, sizeof fmt);
    fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    fmt.fmt.pix.width = f->width;
    fmt.fmt.pix.height = f->height;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    fmt.fmt.pix.field = V4L2_FIELD_NONE;
    fmt.fmt.pix.sizeimage = frame_bufsize(f);
    if (ioctl(a->vfd, VIDIOC_S_FMT, &fmt) < 0) {
        LOG("uvc: S_FMT %dx%d: %s", f->width, f->height, strerror(errno));
        a->sizeimage = frame_bufsize(f);
    } else {
        a->sizeimage = fmt.fmt.pix.sizeimage ? fmt.fmt.pix.sizeimage
                                             : frame_bufsize(f);
    }
}

static void notify_mode(struct app *a)
{
    const struct frame_desc *f = &a->frames[a->cur];
    uint32_t maxb = a->sizeimage > 8192 ? a->sizeimage - 4096 : a->sizeimage;
    net_set_mode(a->net, f->width, f->height, fps_of(a->cur_interval), a->streaming, maxb);
}

static void arm_timer(struct app *a, int on)
{
    struct itimerspec its;
    memset(&its, 0, sizeof its);
    if (on) {
        uint64_t ns = (uint64_t)a->cur_interval * 100;
        its.it_interval.tv_sec = ns / 1000000000ull;
        its.it_interval.tv_nsec = ns % 1000000000ull;
        its.it_value = its.it_interval;
    }
    timerfd_settime(a->tfd, 0, &its, NULL);
}

static void apply_commit(struct app *a, int fi, uint32_t ival)
{
    a->cur = fi;
    a->cur_interval = ival;
    if (!a->streaming)
        set_format(a);
    else
        arm_timer(a, 1);
    LOG("uvc: format selected by the host: %dx%d @ %d fps",
        a->frames[fi].width, a->frames[fi].height, fps_of(ival));
    notify_mode(a);
}

/* ------------------------------------------------------------------ */
/* Image sources                                                       */
/* ------------------------------------------------------------------ */

static void ensure_placeholder(struct app *a)
{
    const struct frame_desc *f = &a->frames[a->cur];
    int conn = net_connected(a->net);
    double now = mono_now();
    if (a->ph && a->ph_w == f->width && a->ph_h == f->height && a->ph_connected == conn &&
        now - a->ph_time < 15.0)
        return;

    char l0[96], l1[96], l2[96], l3[96], l4[96], ip[64];
    get_ip(ip, sizeof ip);
    snprintf(l0, sizeof l0, "REMOTECAM - %s", a->name);
    snprintf(l1, sizeof l1, "%s", conn ? "PC CONNECTED - WAITING FOR IMAGE" : "WAITING FOR PC...");
    if (ip[0])
        snprintf(l2, sizeof l2, "IP WIFI: %s", ip);
    else
        snprintf(l2, sizeof l2, "WIFI NOT CONNECTED");
    snprintf(l3, sizeof l3, "%dX%d - %d FPS", f->width, f->height, fps_of(a->cur_interval));
    snprintf(l4, sizeof l4, "SETUP: REMOTECAM.INI FILE ON THE USB DRIVE");
    const char *lines[] = { l0, l1, l2, l3, l4 };

    uint8_t *out = NULL;
    unsigned long len = 0;
    if (placeholder_make(f->width, f->height, lines, 5, &out, &len) == 0 && out) {
        free(a->ph);
        a->ph = out;
        a->ph_len = len;
        a->ph_w = f->width;
        a->ph_h = f->height;
        a->ph_connected = conn;
        a->ph_time = now;
        a->ph_gen++;
    }
}

/* returns 1 if the frame comes from the PC, 0 for the placeholder, -1 if none */
static int pick_source(struct app *a, const uint8_t **data, size_t *len, uint64_t *fid)
{
    const struct frame_desc *f = &a->frames[a->cur];
    size_t l;
    int fw, fh;
    uint64_t id;
    double ft;
    const uint8_t *d = net_frame(a->net, &l, &fw, &fh, &id, &ft);
    if (d && fw == f->width && fh == f->height &&
        (net_connected(a->net) || mono_now() - ft < 3.0)) {
        *data = d;
        *len = l;
        *fid = id;
        return 1;
    }
    ensure_placeholder(a);
    if (!a->ph)
        return -1;
    *data = a->ph;
    *len = a->ph_len;
    *fid = (1ull << 63) | a->ph_gen;
    return 0;
}

/* ------------------------------------------------------------------ */
/* V4L2 buffers                                                        */
/* ------------------------------------------------------------------ */

static void queue_frame(struct app *a)
{
    int i;
    for (i = 0; i < a->nbufs; i++)
        if (!a->bufs[i].queued)
            break;
    if (i == a->nbufs)
        return;
    struct buf *b = &a->bufs[i];

    const uint8_t *data;
    size_t len;
    uint64_t fid;
    int src = pick_source(a, &data, &len, &fid);
    if (src < 0)
        return;
    if (len > b->len) {
        static double last;
        if (mono_now() - last > 5) {
            LOG("uvc: frame too large (%zu > %zu bytes), dropped - lower the JPEG quality",
                len, b->len);
            last = mono_now();
        }
        if (src == 1 && a->ph && a->ph_len <= b->len) {
            data = a->ph;
            len = a->ph_len;
            fid = (1ull << 63) | a->ph_gen;
            src = 0;
        } else if (b->fid == 0) {
            return;
        } else {
            fid = b->fid; /* resend the previous buffer content */
            len = b->used;
            data = NULL;
        }
    }
    if (data && b->fid != fid) {
        double t0 = mono_now();
        memcpy(b->mem, data, len);
        a->rp_copy += mono_now() - t0;
        a->rp_copies++;
        b->fid = fid;
        b->used = len;
    }

    struct v4l2_buffer vb;
    memset(&vb, 0, sizeof vb);
    vb.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    vb.memory = V4L2_MEMORY_MMAP;
    vb.index = i;
    vb.bytesused = b->used;
    vb.field = V4L2_FIELD_NONE;
    if (ioctl(a->vfd, VIDIOC_QBUF, &vb) < 0) {
        if (g_debug)
            LOG("uvc: QBUF: %s", strerror(errno));
        return;
    }
    b->queued = 1;
    a->last_queued = mono_now();
    a->st_frames++;
    a->rp_frames++;
    if (src == 1) {
        a->st_pc++;
        if (fid != a->last_pc_fid) {
            a->rp_new++;
            a->last_pc_fid = fid;
        }
    }
}

static void dequeue_done(struct app *a)
{
    for (;;) {
        struct v4l2_buffer vb;
        memset(&vb, 0, sizeof vb);
        vb.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
        vb.memory = V4L2_MEMORY_MMAP;
        if (ioctl(a->vfd, VIDIOC_DQBUF, &vb) < 0) {
            if (errno != EAGAIN) {
                if (g_debug)
                    LOG("uvc: DQBUF: %s", strerror(errno));
                usleep(10000); /* avoid busy looping if the queue is in error */
            }
            return;
        }
        if (vb.index < (unsigned)a->nbufs)
            a->bufs[vb.index].queued = 0;
    }
}

static void free_buffers(struct app *a)
{
    for (int i = 0; i < a->nbufs; i++)
        if (a->bufs[i].mem && a->bufs[i].mem != MAP_FAILED)
            munmap(a->bufs[i].mem, a->bufs[i].len);
    memset(a->bufs, 0, sizeof a->bufs);
    a->nbufs = 0;
    struct v4l2_requestbuffers rb;
    memset(&rb, 0, sizeof rb);
    rb.count = 0;
    rb.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    rb.memory = V4L2_MEMORY_MMAP;
    ioctl(a->vfd, VIDIOC_REQBUFS, &rb);
}

static void stop_stream(struct app *a)
{
    if (!a->streaming)
        return;
    int type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    ioctl(a->vfd, VIDIOC_STREAMOFF, &type);
    free_buffers(a);
    arm_timer(a, 0);
    a->streaming = 0;
    LOG("uvc: stream stopped by the host");
    notify_mode(a);
}

static void start_stream(struct app *a)
{
    if (a->streaming)
        stop_stream(a);

    struct v4l2_requestbuffers rb;
    memset(&rb, 0, sizeof rb);
    rb.count = NBUF;
    rb.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    rb.memory = V4L2_MEMORY_MMAP;
    if (ioctl(a->vfd, VIDIOC_REQBUFS, &rb) < 0 || rb.count == 0) {
        LOG("uvc: REQBUFS: %s", strerror(errno));
        return;
    }
    a->nbufs = rb.count > NBUF ? NBUF : (int)rb.count;
    for (int i = 0; i < a->nbufs; i++) {
        struct v4l2_buffer vb;
        memset(&vb, 0, sizeof vb);
        vb.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
        vb.memory = V4L2_MEMORY_MMAP;
        vb.index = i;
        if (ioctl(a->vfd, VIDIOC_QUERYBUF, &vb) < 0) {
            LOG("uvc: QUERYBUF: %s", strerror(errno));
            free_buffers(a);
            return;
        }
        a->bufs[i].len = vb.length;
        a->bufs[i].mem = mmap(NULL, vb.length, PROT_READ | PROT_WRITE, MAP_SHARED, a->vfd,
                              vb.m.offset);
        if (a->bufs[i].mem == MAP_FAILED) {
            LOG("uvc: mmap: %s", strerror(errno));
            free_buffers(a);
            return;
        }
    }
    a->streaming = 1;
    queue_frame(a);
    int type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    if (ioctl(a->vfd, VIDIOC_STREAMON, &type) < 0) {
        LOG("uvc: STREAMON: %s", strerror(errno));
        free_buffers(a);
        a->streaming = 0;
        return;
    }
    arm_timer(a, 1);
    const struct frame_desc *f = &a->frames[a->cur];
    LOG("uvc: stream started %dx%d @ %d fps (%d buffers of %zu bytes)",
        f->width, f->height, fps_of(a->cur_interval), a->nbufs, a->bufs[0].len);
    a->st_frames = a->st_pc = 0;
    a->st_time = mono_now();
    notify_mode(a);
}

static int queued_count(struct app *a)
{
    int q = 0;
    for (int i = 0; i < a->nbufs; i++)
        q += a->bufs[i].queued;
    return q;
}

/* Frame timer: keeps the host fed at the negotiated rate (repeats the last frame or
 * shows the placeholder). Skipped when a fresh PC frame was just pushed. */
static void tick(struct app *a)
{
    a->rp_ticks++;
    if (mono_now() - a->last_queued < a->cur_interval * 1e-7 * 0.5)
        return;
    if (queued_count(a) < MAX_QUEUED)
        queue_frame(a);
    else
        a->rp_busy++; /* USB host has not consumed the previous frames yet */
}

/* A new frame from the PC goes to USB as soon as it arrives, instead of waiting for
 * the next timer tick (timer and network are not in sync: frames were dropped). */
static void push_new_frame(struct app *a)
{
    size_t l;
    int fw, fh;
    uint64_t id;
    double ft;
    const struct frame_desc *f = &a->frames[a->cur];
    if (!net_frame(a->net, &l, &fw, &fh, &id, &ft) || id == a->last_pc_fid ||
        fw != f->width || fh != f->height)
        return;
    if (queued_count(a) < MAX_QUEUED)
        queue_frame(a);
}

/* ------------------------------------------------------------------ */
/* Bottleneck diagnostics                                              */
/* ------------------------------------------------------------------ */

/* CPU usage since the previous call: whole system, softirq (WiFi/TCP), this process */
static void cpu_usage(struct app *a, int *total, int *soft, int *self)
{
    unsigned long long v[10] = { 0 }, sum = 0, ut = 0, st = 0;
    *total = *soft = *self = -1;
    FILE *f = fopen("/proc/stat", "r");
    if (f) {
        if (fscanf(f, "cpu %llu %llu %llu %llu %llu %llu %llu %llu",
                   &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]) == 8) {
            for (int i = 0; i < 8; i++)
                sum += v[i];
            unsigned long long idle = v[3] + v[4];
            unsigned long long dt = sum - a->cpu_total;
            if (a->cpu_total && dt) {
                *total = (int)(100 * (dt - (idle - a->cpu_idle)) / dt);
                *soft = (int)(100 * (v[6] - a->cpu_soft) / dt);
            }
            f = freopen("/proc/self/stat", "r", f);
            if (f) {
                char buf[1024];
                size_t n = fread(buf, 1, sizeof buf - 1, f);
                buf[n] = 0;
                const char *p = strrchr(buf, ')');
                /* after ")": state(3) ... utime(14) stime(15) */
                if (p && sscanf(p + 2, "%*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %llu %llu",
                                &ut, &st) == 2) {
                    if (a->cpu_total && dt)
                        *self = (int)(100 * (ut + st - a->cpu_self) / dt);
                    a->cpu_self = ut + st;
                }
            }
            a->cpu_total = sum;
            a->cpu_idle = idle;
            a->cpu_soft = v[6];
        }
        if (f)
            fclose(f);
    }
}

static void report_stats(struct app *a, double now)
{
    double dt = now - a->rp_time;
    if (dt <= 0)
        return;
    unsigned rx_frames;
    uint64_t rx_bytes;
    net_rx_stats(a->net, &rx_frames, &rx_bytes);
    int cpu, soft, self;
    cpu_usage(a, &cpu, &soft, &self);
    char b[512];
    snprintf(b, sizeof b,
             "{\"type\":\"stats\",\"dt\":%.2f,\"host_fps\":%d,\"streaming\":%s,"
             "\"ticks\":%u,\"usb_frames\":%u,\"usb_new\":%u,\"usb_busy\":%u,"
             "\"rx_frames\":%u,\"rx_bytes\":%llu,\"copy_ms\":%.2f,"
             "\"cpu\":%d,\"cpu_softirq\":%d,\"cpu_bridge\":%d}\n",
             dt, fps_of(a->cur_interval), a->streaming ? "true" : "false",
             a->rp_ticks, a->rp_frames, a->rp_new, a->rp_busy,
             rx_frames, (unsigned long long)rx_bytes,
             a->rp_copies ? 1000.0 * a->rp_copy / a->rp_copies : 0.0, cpu, soft, self);
    net_send_line(a->net, b);
    a->rp_ticks = a->rp_frames = a->rp_new = a->rp_busy = a->rp_copies = 0;
    a->rp_copy = 0;
    a->rp_time = now;
}

/* ------------------------------------------------------------------ */
/* USB control requests                                                */
/* ------------------------------------------------------------------ */

static void vs_request(struct app *a, uint8_t req, uint8_t cs, uint16_t wlen,
                       struct uvc_request_data *resp)
{
    if (cs != UVC_VS_PROBE_CONTROL && cs != UVC_VS_COMMIT_CONTROL)
        return; /* stall */
    struct uvc_streaming_control *ctrl = (struct uvc_streaming_control *)resp->data;
    int len = wlen < sizeof *ctrl ? wlen : (int)sizeof *ctrl;

    if (g_debug && req != UVC_SET_CUR)
        LOG("uvc: %s GET %02x", cs == UVC_VS_PROBE_CONTROL ? "PROBE" : "COMMIT", req);
    switch (req) {
    case UVC_SET_CUR:
        a->set_cur_target = (cs == UVC_VS_PROBE_CONTROL) ? 1 : 2;
        resp->length = len;
        break;
    case UVC_GET_CUR:
        memcpy(ctrl, cs == UVC_VS_PROBE_CONTROL ? &a->probe : &a->commit, sizeof *ctrl);
        resp->length = len;
        break;
    case UVC_GET_MIN:
    case UVC_GET_DEF:
        fill_ctrl(a, ctrl, a->def_frame, clamp_interval(a, a->def_frame, 0));
        resp->length = len;
        break;
    case UVC_GET_MAX:
        fill_ctrl(a, ctrl, a->nframes - 1, clamp_interval(a, a->nframes - 1, 0));
        resp->length = len;
        break;
    case UVC_GET_RES:
        memset(ctrl, 0, sizeof *ctrl);
        resp->length = len;
        break;
    case UVC_GET_LEN:
        resp->data[0] = sizeof *ctrl;
        resp->data[1] = 0;
        resp->length = 2;
        break;
    case UVC_GET_INFO:
        resp->data[0] = 0x03;
        resp->length = 1;
        break;
    }
}

/* Control interface: camera terminal, processing unit...
 * The gadget normally exposes no control (bmControls = 0); we still answer
 * plausibly so that a curious host does not fail. */
static void vc_request(struct app *a, uint8_t req, uint8_t entity, uint8_t cs, uint16_t wlen,
                       struct uvc_request_data *resp)
{
    if (entity == 0) {
        if (cs == UVC_VC_REQUEST_ERROR_CODE_CONTROL) {
            if (req == UVC_GET_CUR) {
                resp->data[0] = a->vc_error;
                resp->length = 1;
            } else if (req == UVC_GET_INFO) {
                resp->data[0] = 0x01;
                resp->length = 1;
            }
            return;
        }
        a->vc_error = 0x06; /* invalid control */
        return;
    }

    int n = wlen > (int)sizeof resp->data ? (int)sizeof resp->data : wlen;
    if (n <= 0) {
        a->vc_error = 0x06;
        return;
    }
    memset(resp->data, 0, n);
    int ae_mode = (entity == 1 && cs == UVC_CT_AE_MODE_CONTROL);
    switch (req) {
    case UVC_SET_CUR:
        a->set_cur_target = -1;
        resp->length = n;
        break;
    case UVC_GET_INFO:
        resp->data[0] = 0x03;
        resp->length = 1;
        break;
    case UVC_GET_MIN:
        resp->length = n;
        break;
    case UVC_GET_MAX:
        resp->data[0] = ae_mode ? 0x01 : 0xFF;
        resp->length = n;
        break;
    case UVC_GET_RES:
        resp->data[0] = 0x01;
        resp->length = n;
        break;
    case UVC_GET_CUR:
    case UVC_GET_DEF:
        resp->data[0] = ae_mode ? 0x01 : 0x80;
        resp->length = n;
        break;
    default:
        a->vc_error = 0x07; /* invalid request */
        return;
    }
    a->vc_error = 0;
}

static void handle_setup(struct app *a, const struct usb_ctrlrequest *req)
{
    struct uvc_request_data resp;
    memset(&resp, 0, sizeof resp);
    resp.length = -EL2HLT; /* stall by default */

    uint16_t windex = le16toh(req->wIndex);
    uint16_t wvalue = le16toh(req->wValue);
    uint16_t wlen = le16toh(req->wLength);
    uint8_t intf = windex & 0xff, entity = windex >> 8, cs = wvalue >> 8;

    if (g_debug)
        LOG("uvc: setup type=%02x req=%02x intf=%u entity=%u cs=%u len=%u",
            req->bRequestType, req->bRequest, intf, entity, cs, wlen);

    if ((req->bRequestType & USB_TYPE_MASK) == USB_TYPE_CLASS &&
        (req->bRequestType & USB_RECIP_MASK) == USB_RECIP_INTERFACE) {
        a->set_cur_target = 0;
        if (intf == a->vs_intf)
            vs_request(a, req->bRequest, cs, wlen, &resp);
        else
            vc_request(a, req->bRequest, entity, cs, wlen, &resp);
    }
    if (ioctl(a->vfd, UVCIOC_SEND_RESPONSE, &resp) < 0)
        LOG("uvc: SEND_RESPONSE: %s", strerror(errno));
}

static void handle_data(struct app *a, const struct uvc_request_data *d)
{
    int target = a->set_cur_target;
    a->set_cur_target = 0;
    if (target != 1 && target != 2)
        return;

    struct uvc_streaming_control in;
    memset(&in, 0, sizeof in);
    int n = d->length > (int)sizeof in ? (int)sizeof in : d->length;
    if (n > 0)
        memcpy(&in, d->data, n);

    int fi = find_frame(a, in.bFrameIndex);
    if (fi < 0)
        fi = a->def_frame;
    uint32_t ival = clamp_interval(a, fi, in.dwFrameInterval);
    if (g_debug)
        LOG("uvc: %s SET_CUR: hint %04x frame %u interval %u -> frame %d interval %u (%d fps)",
            target == 1 ? "PROBE" : "COMMIT", in.bmHint, in.bFrameIndex, in.dwFrameInterval,
            a->frames[fi].index, ival, fps_of(ival));

    if (target == 1) {
        fill_ctrl(a, &a->probe, fi, ival);
    } else {
        fill_ctrl(a, &a->commit, fi, ival);
        apply_commit(a, fi, ival);
    }
}

static void process_events(struct app *a)
{
    struct v4l2_event ev;
    for (int k = 0; k < 32; k++) {
        memset(&ev, 0, sizeof ev);
        if (ioctl(a->vfd, VIDIOC_DQEVENT, &ev) < 0)
            return;
        struct uvc_event *uev = (struct uvc_event *)&ev.u.data;
        switch (ev.type) {
        case UVC_EVENT_CONNECT:
            LOG("uvc: connected to the USB host");
            break;
        case UVC_EVENT_DISCONNECT:
            LOG("uvc: disconnected from the USB host");
            stop_stream(a);
            break;
        case UVC_EVENT_SETUP:
            handle_setup(a, &uev->req);
            break;
        case UVC_EVENT_DATA:
            handle_data(a, &uev->data);
            break;
        case UVC_EVENT_STREAMON:
            start_stream(a);
            break;
        case UVC_EVENT_STREAMOFF:
            stop_stream(a);
            break;
        }
    }
}

/* ------------------------------------------------------------------ */
/* PC native resolution: added to the gadget (USB re-enumeration)      */
/* ------------------------------------------------------------------ */

static void check_native(struct app *a)
{
    int w, h;
    if (net_take_native(a->net, &w, &h) && a->native_auto) {
        a->pend_nw = w & ~1;
        a->pend_nh = h & ~1;
    }
    if (!a->pend_nw)
        return;
    w = a->pend_nw;
    h = a->pend_nh;
    a->pend_nw = a->pend_nh = 0;
    if (w < 160 || h < 120 || w > 4096 || h > 2304)
        return;

    /* native.txt holds the resolution added for the PC screen; it follows the
     * current screen, so an old native resolution does not stay first in the list */
    char want[32], have[32] = "";
    snprintf(want, sizeof want, "%dx%d", w, h);
    FILE *f = fopen(NATIVE_FILE, "r");
    if (f) {
        if (!fgets(have, sizeof have, f))
            have[0] = 0;
        fclose(f);
        have[strcspn(have, "\r\n")] = 0;
    }
    if (!strcmp(have, want))
        return; /* already requested: avoids a loop if the kernel rejected it */

    int exposed = 0;
    for (int i = 0; i < a->nframes; i++)
        if (a->frames[i].width == w && a->frames[i].height == h)
            exposed = 1;
    if (exposed) {
        /* the screen resolution is one of the configured ones: no extra resolution */
        if (!have[0])
            return;
        if (unlink(NATIVE_FILE) < 0) {
            LOG("native: cannot remove %s", NATIVE_FILE);
            return;
        }
        LOG("native: screen is now %s (configured): %s will be removed at the next start "
            "of the Pi (unplug / replug it)", want, have);
    } else {
        f = fopen(NATIVE_FILE, "w");
        if (!f) {
            LOG("native: cannot write %s", NATIVE_FILE);
            return;
        }
        fprintf(f, "%s\n", want);
        fclose(f);
        LOG("native: screen is now %s: offered from the next start of the Pi "
            "(unplug / replug it), replacing %s", want, have[0] ? have : "none");
    }
    sync();
    /* Applied at the next start of the Pi (next USB plug-in), never live: tearing down
     * the UVC gadget while the system runs crashes the kernel driver (oops in
     * uvc_function_unbind, seen on a Zero 2 W after a few stream starts/stops). */
}

/* ------------------------------------------------------------------ */
/* Initialization                                                      */
/* ------------------------------------------------------------------ */

static int open_uvc_device(void)
{
    const char *forced = getenv("RC_DEVICE");
    if (forced && *forced) {
        int fd = open(forced, O_RDWR | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0)
            LOG("uvc: opening %s: %s", forced, strerror(errno));
        return fd;
    }
    for (int tries = 0; tries < 50; tries++) {
        for (int i = 0; i < 64; i++) {
            char p[32];
            snprintf(p, sizeof p, "/dev/video%d", i);
            int fd = open(p, O_RDWR | O_NONBLOCK | O_CLOEXEC);
            if (fd < 0)
                continue;
            struct v4l2_capability cap;
            memset(&cap, 0, sizeof cap);
            if (ioctl(fd, VIDIOC_QUERYCAP, &cap) == 0 &&
                strstr((const char *)cap.driver, "g_uvc") &&
                ((cap.capabilities | cap.device_caps) & V4L2_CAP_VIDEO_OUTPUT)) {
                LOG("uvc: gadget device %s (%s)", p, cap.card);
                return fd;
            }
            close(fd);
        }
        usleep(200000);
    }
    return -1;
}

static int subscribe(int fd, unsigned type)
{
    struct v4l2_event_subscription sub;
    memset(&sub, 0, sizeof sub);
    sub.type = type;
    return ioctl(fd, VIDIOC_SUBSCRIBE_EVENT, &sub);
}

static void sanitize_name(char *dst, size_t cap, const char *src)
{
    size_t i = 0;
    for (; *src && i + 1 < cap; src++) {
        unsigned char c = *src;
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
            c == ' ' || c == '-' || c == '_' || c == '.')
            dst[i++] = c;
        else
            dst[i++] = '-';
    }
    dst[i] = 0;
    if (!i)
        snprintf(dst, cap, "RemoteCam");
}

int main(void)
{
    static struct app A;
    struct app *a = &A;
    memset(a, 0, sizeof *a);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    g_debug = getenv("RC_DEBUG") && *getenv("RC_DEBUG");
    const char *name = getenv("RC_NAME");
    sanitize_name(a->name, sizeof a->name, name && *name ? name : "RemoteCam");
    const char *key = getenv("RC_KEY");
    const char *native = getenv("RC_NATIVE");
    a->native_auto = !native || !*native || !strcmp(native, "auto");
    const char *info = getenv("RC_INFO");
    if (!info || !*info)
        info = "/run/remotecam/uvc.info";

    a->vs_intf = 1;
    a->maxpayload = 1024;
    if (load_info(a, info) < 0)
        LOG("warning: %s not found, using defaults", info);
    if (a->nframes == 0) {
        a->frames[0] = (struct frame_desc){ .index = 1, .width = 1920, .height = 1080 };
        a->frames[1] = (struct frame_desc){ .index = 2, .width = 1280, .height = 720 };
        a->nframes = 2;
        a->def_frame = 0;
    }
    if (a->nintervals == 0)
        a->intervals[a->nintervals++] = 333333;
    for (int i = 0; i < a->nframes; i++)
        LOG("uvc: resolution %d : %dx%d", a->frames[i].index, a->frames[i].width, a->frames[i].height);

    a->vfd = open_uvc_device();
    if (a->vfd < 0) {
        LOG("error: no UVC gadget video device (did gadget.sh up run?)");
        return 1;
    }
    a->tfd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    a->net = net_create(a->name, key);
    if (!a->net || a->tfd < 0) {
        LOG("error: network/timer initialization");
        return 1;
    }

    a->cur = a->def_frame;
    a->cur_interval = clamp_interval(a, a->cur, 0);
    fill_ctrl(a, &a->probe, a->cur, a->cur_interval);
    fill_ctrl(a, &a->commit, a->cur, a->cur_interval);
    set_format(a);
    notify_mode(a);

    /* SETUP last: subscribing to it triggers the USB connection (recent kernels) */
    subscribe(a->vfd, UVC_EVENT_CONNECT);
    subscribe(a->vfd, UVC_EVENT_DISCONNECT);
    subscribe(a->vfd, UVC_EVENT_STREAMON);
    subscribe(a->vfd, UVC_EVENT_STREAMOFF);
    subscribe(a->vfd, UVC_EVENT_DATA);
    if (subscribe(a->vfd, UVC_EVENT_SETUP) < 0) {
        LOG("error: subscribing to UVC events: %s", strerror(errno));
        return 1;
    }
    LOG("uvc-bridge %s ready (streaming interface %d, max packet %u)", RC_VERSION, a->vs_intf,
        a->maxpayload);

    double last_stat = mono_now();
    while (g_running) {
        fd_set r, w, e;
        FD_ZERO(&r);
        FD_ZERO(&w);
        FD_ZERO(&e);
        int maxfd = a->vfd;
        /* The kernel reports POLLERR while the stream is stopped, so we only watch
         * for writability while streaming (events come through exceptfds). */
        FD_SET(a->vfd, &e);
        if (a->streaming) {
            FD_SET(a->vfd, &w);
            FD_SET(a->tfd, &r);
            if (a->tfd > maxfd)
                maxfd = a->tfd;
        }
        net_fdset(a->net, &r, &w, &maxfd);
        struct timeval tv = { 0, 200000 };
        int n = select(maxfd + 1, &r, &w, &e, &tv);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            LOG("select: %s", strerror(errno));
            break;
        }
        int was_streaming = a->streaming;
        if (FD_ISSET(a->vfd, &e))
            process_events(a);
        if (was_streaming && a->streaming && FD_ISSET(a->vfd, &w))
            dequeue_done(a);
        if (was_streaming && a->streaming && FD_ISSET(a->tfd, &r)) {
            uint64_t exp;
            if (read(a->tfd, &exp, sizeof exp) == sizeof exp)
                tick(a);
        }
        net_handle(a->net, &r, &w);
        if (a->streaming)
            push_new_frame(a);
        net_periodic(a->net);
        check_native(a);

        double now = mono_now();
        if (now - a->rp_time >= 2.0)
            report_stats(a, now);
        if (now - last_stat >= 30.0) {
            if (a->streaming)
                LOG("stats: %.1f fps to the host (%.1f from the PC), client: %s",
                    a->st_frames / (now - a->st_time), a->st_pc / (now - a->st_time),
                    net_connected(a->net) ? net_peer(a->net) : "none");
            a->st_frames = a->st_pc = 0;
            a->st_time = now;
            last_stat = now;
        }
    }

    LOG("stopping");
    stop_stream(a);
    net_destroy(a->net);
    close(a->tfd);
    close(a->vfd);
    free(a->ph);
    return 0;
}
