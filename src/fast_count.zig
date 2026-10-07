//! Record counting for `SELECT COUNT(*)` with no WHERE, GROUP BY or JOIN.
//!
//! The general scalar-aggregate path splits every row into fields before
//! incrementing a counter it could have incremented from the raw bytes. On a
//! 746 MB file that was 10.6 GB/s where nothing but the record boundaries
//! matter. Counting those directly is memory-bandwidth bound.
//!
//! A record ends at a newline that is not inside a quoted field, so the
//! newline mask has to be filtered by quote state. Quote state is sequential:
//! whether byte N is inside a field depends on every quote before it. The
//! prefix-XOR trick turns that into a few shifts per 64-byte block (the same
//! one `csv.findRecordEndPrefixXor` uses), and `""` needs no special case
//! because two quotes toggle parity twice and land back where they started.
//!
//! Chunks still cannot know whether they begin inside a quote, which is what
//! normally forces this kind of scan to be serial. Instead each chunk carries
//! both answers: for a given block the newlines-outside-quotes mask under
//! "started outside" is `nl & ~px` and under "started inside" is `nl & px`,
//! because the two quote states are exact complements. One pass maintains both
//! counters, and a serial walk over the per-chunk results (one iteration per
//! thread, not per row) picks the right one and threads the parity through.

const std = @import("std");
const builtin = @import("builtin");

const Allocator = std.mem.Allocator;

/// Bytes per SIMD block. 64 keeps each mask in a single u64.
const BLOCK = 64;

inline fn prefixXor(x: u64) u64 {
    var r = x;
    r ^= r << 1;
    r ^= r << 2;
    r ^= r << 4;
    r ^= r << 8;
    r ^= r << 16;
    r ^= r << 32;
    return r;
}

/// Build a bitmask of positions in `block` equal to `needle`, bit 0 = byte 0.
inline fn maskOf(block: *const [BLOCK]u8, comptime needle: u8) u64 {
    const V = @Vector(BLOCK, u8);
    const v: V = block.*;
    const hits = v == @as(V, @splat(needle));
    return @as(u64, @bitCast(hits));
}

/// What one chunk contributes, under each hypothesis about its start state.
const ChunkCount = struct {
    /// Newlines outside quotes if the chunk begins outside a quoted field.
    if_outside: u64 = 0,
    /// Newlines outside quotes if the chunk begins inside one.
    if_inside: u64 = 0,
    /// Whether the chunk contains an odd number of quotes, so the caller can
    /// thread the state to the next chunk.
    flips_state: bool = false,
};

/// Count newlines outside quoted fields in `data`, both hypotheses at once.
fn countChunk(data: []const u8) ChunkCount {
    var out = ChunkCount{};
    // Quote state for the "started outside" hypothesis. The other hypothesis
    // is always its complement, so one bit tracks both.
    var carry: u1 = 0;

    // Separate accumulators so the adds do not serialize on one register.
    var out_a: u64 = 0;
    var out_b: u64 = 0;
    var in_a: u64 = 0;
    var in_b: u64 = 0;

    var i: usize = 0;
    // Two blocks per iteration. Each block's quote parity feeds the next, so
    // the pair is still ordered, but the popcounts interleave.
    while (i + 2 * BLOCK <= data.len) : (i += 2 * BLOCK) {
        inline for (0..2) |half| {
            const block: *const [BLOCK]u8 = @ptrCast(data[i + half * BLOCK ..][0..BLOCK]);
            const q = maskOf(block, '"');
            const nl = maskOf(block, '\n');
            if (q == 0) {
                // No quote in this block, so quote state cannot change inside
                // it and every newline is outside a field exactly when the
                // incoming state says so. This is every block of an unquoted
                // file, and it skips the six shift/xor pairs below.
                if (carry == 0) {
                    if (half == 0) out_a += @popCount(nl) else out_b += @popCount(nl);
                } else {
                    if (half == 0) in_a += @popCount(nl) else in_b += @popCount(nl);
                }
            } else {
                const px = prefixXor(q);
                // Newlines outside a quoted field, under each hypothesis.
                const when0 = nl & ~px;
                const when1 = nl & px;
                if (carry == 0) {
                    if (half == 0) {
                        out_a += @popCount(when0);
                        in_a += @popCount(when1);
                    } else {
                        out_b += @popCount(when0);
                        in_b += @popCount(when1);
                    }
                } else {
                    if (half == 0) {
                        out_a += @popCount(when1);
                        in_a += @popCount(when0);
                    } else {
                        out_b += @popCount(when1);
                        in_b += @popCount(when0);
                    }
                }
                carry ^= @intCast(@popCount(q) & 1);
            }
        }
    }
    while (i + BLOCK <= data.len) : (i += BLOCK) {
        const block: *const [BLOCK]u8 = @ptrCast(data[i..][0..BLOCK]);
        const q = maskOf(block, '"');
        const nl = maskOf(block, '\n');
        if (q == 0) {
            if (carry == 0) out_a += @popCount(nl) else in_a += @popCount(nl);
        } else {
            const px = prefixXor(q);
            const when0 = nl & ~px;
            const when1 = nl & px;
            if (carry == 0) {
                out_a += @popCount(when0);
                in_a += @popCount(when1);
            } else {
                out_a += @popCount(when1);
                in_a += @popCount(when0);
            }
            carry ^= @intCast(@popCount(q) & 1);
        }
    }
    out.if_outside = out_a + out_b;
    out.if_inside = in_a + in_b;

    // Tail shorter than a block: same logic, one byte at a time.
    var in_quote_outside_hyp = carry == 1;
    while (i < data.len) : (i += 1) {
        switch (data[i]) {
            '"' => in_quote_outside_hyp = !in_quote_outside_hyp,
            '\n' => {
                if (in_quote_outside_hyp) out.if_inside += 1 else out.if_outside += 1;
            },
            else => {},
        }
    }
    // `carry` started at 0 for this chunk and `in_quote_outside_hyp` carried
    // it through the tail, so it now holds the chunk's own quote parity.
    out.flips_state = in_quote_outside_hyp;
    return out;
}

const Worker = struct {
    data: []const u8,
    result: ChunkCount = .{},

    fn run(self: *Worker) void {
        self.result = countChunk(self.data);
    }
};

/// Number of data records in `data`, excluding the header when `has_header`.
///
/// Counts record-terminating newlines, then adds the final record when the
/// file does not end in one. A file whose last line is empty (trailing
/// newline) contributes no extra record, which is what every other path does.
pub fn countRecords(allocator: Allocator, data: []const u8, has_header: bool, thread_count: usize) !u64 {
    if (data.len == 0) return 0;

    const threads = @max(1, thread_count);
    // Below this, splitting costs more than it saves.
    const min_per_thread = 1 << 20;
    const usable = @min(threads, @max(1, data.len / min_per_thread));

    var newlines: u64 = 0;

    if (usable == 1) {
        const c = countChunk(data);
        newlines = c.if_outside;
    } else {
        const workers = try allocator.alloc(Worker, usable);
        defer allocator.free(workers);
        const handles = try allocator.alloc(std.Thread, usable);
        defer allocator.free(handles);

        const per = data.len / usable;
        for (workers, 0..) |*w, i| {
            const start = i * per;
            const end = if (i == usable - 1) data.len else (i + 1) * per;
            w.* = .{ .data = data[start..end] };
            handles[i] = try std.Thread.spawn(.{}, Worker.run, .{w});
        }
        for (handles) |h| h.join();

        // One iteration per thread. A record cannot start inside a quoted
        // field, so the first chunk begins outside one.
        var inside = false;
        for (workers) |w| {
            newlines += if (inside) w.result.if_inside else w.result.if_outside;
            if (w.result.flips_state) inside = !inside;
        }
    }

    // A final record with no trailing newline was never counted.
    const unterminated: u64 = if (data[data.len - 1] != '\n') 1 else 0;
    const total = newlines + unterminated;

    if (!has_header) return total;
    return if (total == 0) 0 else total - 1;
}

test "countRecords: plain rows" {
    const a = std.testing.allocator;
    try std.testing.expectEqual(@as(u64, 2), try countRecords(a, "h\n1\n2\n", true, 1));
    try std.testing.expectEqual(@as(u64, 3), try countRecords(a, "h\n1\n2\n", false, 1));
}

test "countRecords: no trailing newline" {
    const a = std.testing.allocator;
    try std.testing.expectEqual(@as(u64, 2), try countRecords(a, "h\n1\n2", true, 1));
}

test "countRecords: newline inside a quoted field is not a record end" {
    const a = std.testing.allocator;
    try std.testing.expectEqual(@as(u64, 1), try countRecords(a, "h\n\"a\nb\"\n", true, 1));
    try std.testing.expectEqual(@as(u64, 2), try countRecords(a, "h\n\"a\nb\"\nc\n", true, 1));
}

test "countRecords: doubled quotes toggle parity twice" {
    const a = std.testing.allocator;
    // The "" is an escaped quote inside the field, so the field stays open
    // and the newline within it does not end the record.
    try std.testing.expectEqual(@as(u64, 1), try countRecords(a, "h\n\"a\"\"b\nc\"\n", true, 1));
}

test "countRecords: empty and header-only" {
    const a = std.testing.allocator;
    try std.testing.expectEqual(@as(u64, 0), try countRecords(a, "", true, 1));
    try std.testing.expectEqual(@as(u64, 0), try countRecords(a, "h\n", true, 1));
    try std.testing.expectEqual(@as(u64, 0), try countRecords(a, "h", true, 1));
}

test "countRecords: CRLF" {
    const a = std.testing.allocator;
    try std.testing.expectEqual(@as(u64, 2), try countRecords(a, "h\r\n1\r\n2\r\n", true, 1));
}

test "countRecords: parallel agrees with serial across block boundaries" {
    const a = std.testing.allocator;
    var buf = std.ArrayList(u8){};
    defer buf.deinit(a);
    try buf.appendSlice(a, "h\n");
    // Long enough to cross many 64-byte blocks and several chunks, with a
    // quoted embedded newline every few rows so quote state straddles both.
    for (0..20000) |i| {
        if (i % 7 == 0) {
            try buf.appendSlice(a, "\"x\ny\",2\n");
        } else {
            try buf.appendSlice(a, "aaaaaaaaaaaaaaaaaaaa,1\n");
        }
    }
    const serial = try countRecords(a, buf.items, true, 1);
    try std.testing.expectEqual(@as(u64, 20000), serial);
    for ([_]usize{ 2, 3, 4, 8, 12 }) |t| {
        try std.testing.expectEqual(serial, try countRecords(a, buf.items, true, t));
    }
}
