#!/usr/bin/python
# @lint-avoid-python-3-compatibility-imports
#
# opensnoop Trace open() syscalls.
#           For Linux, uses BCC, eBPF. Embedded C.
#
# USAGE: opensnoop [-h] [-t] [-x] [-p PID]
#
# Copyright (c) 2015 Brendan Gregg.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 17-Sep-2015   Brendan Gregg   Created this.
# 29-Apr-2016   Allan McAleavy updated for BPF_PERF_OUTPUT

from __future__ import print_function
from bcc import BPF
import argparse
import ctypes as ct

# arguments
examples = """examples:
    ./opensnoop           # trace all open() syscalls
    ./opensnoop -t        # include timestamps
    ./opensnoop -x        # only show failed opens
    ./opensnoop -p 181    # only trace PID 181
"""
parser = argparse.ArgumentParser(
    description="Trace open() syscalls",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-t", "--timestamp", action="store_true",
    help="include timestamp on output")
parser.add_argument("-x", "--failed", action="store_true",
    help="only show failed opens")
parser.add_argument("-p", "--pid",
    help="trace this PID only")
args = parser.parse_args()
debug = 0

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <uapi/linux/limits.h>
#include <linux/sched.h>

struct val_t {
    u32 pid;
    u64 ts;
    char comm[TASK_COMM_LEN];
    const char *fname;
};

struct data_t {
    u32 pid;
    u64 ts;
    u64 delta;
    int ret;
    char comm[TASK_COMM_LEN];
    char fname[NAME_MAX];
};

BPF_HASH(args_filename, u32, const char *);
BPF_HASH(infotmp, u32, struct val_t);
BPF_PERF_OUTPUT(events);

int trace_entry(struct pt_regs *ctx, const char __user *filename)
{
    struct val_t val = {};
    u32 pid = bpf_get_current_pid_tgid();

    FILTER
    if (bpf_get_current_comm(&val.comm, sizeof(val.comm)) == 0) {
        val.pid = bpf_get_current_pid_tgid();
        val.ts = bpf_ktime_get_ns();
        val.fname = filename;
        infotmp.update(&pid, &val);
    }

    return 0;
};

int trace_return(struct pt_regs *ctx)
{
    u32 pid = bpf_get_current_pid_tgid();
    struct val_t *valp;
    struct data_t data = {};

    u64 tsp = bpf_ktime_get_ns();

    valp = infotmp.lookup(&pid);
    if (valp == 0) {
        // missed entry
        return 0;
    }
    bpf_probe_read(&data.comm, sizeof(data.comm), valp->comm);
    bpf_probe_read(&data.fname, sizeof(data.fname), (void *)valp->fname);
    data.pid = valp->pid;
    data.delta = tsp - valp->ts;
    data.ts = tsp / 1000;
    data.ret = PT_REGS_RC(ctx);

    events.perf_submit(ctx, &data, sizeof(data));
    infotmp.delete(&pid);
    args_filename.delete(&pid);

    return 0;
}
"""
if args.pid:
    bpf_text = bpf_text.replace('FILTER',
        'if (pid != %s) { return 0; }' % args.pid)
else:
    bpf_text = bpf_text.replace('FILTER', '')
if debug:
    print(bpf_text)

# initialize BPF
b = BPF(text=bpf_text)
b.attach_kprobe(event="sys_open", fn_name="trace_entry")
b.attach_kretprobe(event="sys_open", fn_name="trace_return")

TASK_COMM_LEN = 16    # linux/sched.h
NAME_MAX = 255        # linux/limits.h

class Data(ct.Structure):
    _fields_ = [
        ("pid", ct.c_ulonglong),
        ("ts", ct.c_ulonglong),
        ("delta", ct.c_ulonglong),
        ("ret", ct.c_int),
        ("comm", ct.c_char * TASK_COMM_LEN),
        ("fname", ct.c_char * NAME_MAX)
    ]

start_ts = 0
prev_ts = 0
delta = 0

# header
if args.timestamp:
    print("%-14s" % ("TIME(s)"), end="")
print("%-6s %-16s %4s %3s %s" % ("PID", "COMM", "FD", "ERR", "PATH"))

# process event
def print_event(cpu, data, size):
    event = ct.cast(data, ct.POINTER(Data)).contents
    global start_ts
    global prev_ts
    global delta
    global cont

    # split return value into FD and errno columns
    if event.ret >= 0:
        fd_s = event.ret
        err = 0
    else:
        fd_s = -1
        err = - event.ret

    if start_ts == 0:
        prev_ts = start_ts

    if start_ts == 1:
        delta = float(delta) + (event.ts - prev_ts)

    if (args.failed and (event.ret >= 0)):
        start_ts = 1
        prev_ts = event.ts
        return

    if args.timestamp:
        print("%-14.9f" % (delta / 1000000), end="")

    print("%-6d %-16s %4d %3d %s" % (event.pid, event.comm,
        fd_s, err, event.fname))

    prev_ts = event.ts
    start_ts = 1

# loop with callback to print_event
b["events"].open_perf_buffer(print_event)
while 1:
    b.kprobe_poll()
