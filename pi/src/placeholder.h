/* RemoteCam - placeholder picture (shown in Teams while the PC sends nothing) */
#pragma once
#include <stdint.h>

/* Generates a w x h JPEG with centered text lines.
 * *out is allocated with malloc() (release with free()). Returns 0 on success. */
int placeholder_make(int w, int h, const char *const *lines, int nlines,
                     uint8_t **out, unsigned long *outlen);
