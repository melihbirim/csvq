//! Read-only memory mapping of a whole file, on POSIX and Windows.
//!
//! Windows used to fall back to readToEndAlloc: a single-threaded copy of the
//! entire file into the heap before the first byte was scanned, plus a
//! file-sized allocation (8 GB for an 8 GB CSV). A mapped view instead lets
//! the parallel scanners fault pages in concurrently straight from the page
//! cache, the same as mmap on POSIX.

const std = @import("std");
const builtin = @import("builtin");

const win = struct {
    const windows = std.os.windows;
    const PAGE_READONLY: windows.DWORD = 0x02;
    const FILE_MAP_READ: windows.DWORD = 0x04;

    extern "kernel32" fn CreateFileMappingW(
        hFile: windows.HANDLE,
        lpFileMappingAttributes: ?*anyopaque,
        flProtect: windows.DWORD,
        dwMaximumSizeHigh: windows.DWORD,
        dwMaximumSizeLow: windows.DWORD,
        lpName: ?windows.LPCWSTR,
    ) callconv(.winapi) ?windows.HANDLE;

    extern "kernel32" fn MapViewOfFile(
        hFileMappingObject: windows.HANDLE,
        dwDesiredAccess: windows.DWORD,
        dwFileOffsetHigh: windows.DWORD,
        dwFileOffsetLow: windows.DWORD,
        dwNumberOfBytesToMap: usize,
    ) callconv(.winapi) ?[*]u8;

    extern "kernel32" fn UnmapViewOfFile(lpBaseAddress: *const anyopaque) callconv(.winapi) windows.BOOL;
};

/// Map `size` bytes of `file` read-only. Release with `unmap`.
/// A zero-length file yields an empty slice (neither OS can map 0 bytes).
pub fn map(file: std.fs.File, size: u64) ![]const u8 {
    if (size == 0) return &.{};
    const len = std.math.cast(usize, size) orelse return error.FileTooBig;

    if (builtin.os.tag == .windows) {
        const mapping = win.CreateFileMappingW(file.handle, null, win.PAGE_READONLY, 0, 0, null) orelse
            return error.MapFailed;
        // The view keeps the mapping object alive; the handle can go now.
        defer std.os.windows.CloseHandle(mapping);
        const ptr = win.MapViewOfFile(mapping, win.FILE_MAP_READ, 0, 0, len) orelse
            return error.MapFailed;
        return ptr[0..len];
    }

    const mapped = try std.posix.mmap(null, len, std.posix.PROT.READ, .{ .TYPE = .SHARED }, file.handle, 0);
    std.posix.madvise(mapped.ptr, mapped.len, std.posix.MADV.SEQUENTIAL) catch {};
    return mapped;
}

pub fn unmap(data: []const u8) void {
    if (data.len == 0) return;
    if (builtin.os.tag == .windows) {
        _ = win.UnmapViewOfFile(data.ptr);
    } else {
        std.posix.munmap(@alignCast(data));
    }
}

test "map reads a file's exact bytes and unmaps" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const content = "id,name\n1,a\n2,b\n";
    try tmp.dir.writeFile(.{ .sub_path = "t.csv", .data = content });
    const f = try tmp.dir.openFile("t.csv", .{});
    defer f.close();
    const data = try map(f, (try f.stat()).size);
    defer unmap(data);
    try std.testing.expectEqualStrings(content, data);
}

test "map of an empty file is an empty slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(.{ .sub_path = "e.csv", .data = "" });
    const f = try tmp.dir.openFile("e.csv", .{});
    defer f.close();
    const data = try map(f, 0);
    defer unmap(data);
    try std.testing.expectEqual(@as(usize, 0), data.len);
}
