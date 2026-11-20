CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu11
LDLIBS  := -ljpeg

SRC := src/uvc_bridge.c src/net.c src/placeholder.c
HDR := $(wildcard src/*.h)

uvc-bridge: $(SRC) $(HDR)
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDLIBS)

clean:
	rm -f uvc-bridge

.PHONY: clean
