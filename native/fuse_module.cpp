// Read-only Linux filesystem adapter. All media generation belongs to the Python sequence.
#define FUSE_USE_VERSION 31
#include <fuse.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <vector>
#include <unistd.h>

namespace py = pybind11;

namespace {
py::object& backend() {
    return *static_cast<py::object*>(fuse_get_context()->private_data);
}

int failure(py::error_already_set& error) {
    if (error.matches(PyExc_OSError)) {
        auto number = error.value().attr("errno");
        if (!number.is_none()) return -number.cast<int>();
    }
    // Keep a failed decompression or corrupt metadata visible in the mount's log.
    PySys_WriteStderr("mcrawsome mount: %s\n", error.what());
    return -EIO;
}

int getattr_cb(const char* path, struct stat* out, struct fuse_file_info*) {
    py::gil_scoped_acquire gil;
    std::memset(out, 0, sizeof(*out));
    out->st_uid = getuid();
    out->st_gid = getgid();
    out->st_blksize = 4096;
    try {
        const auto modified = backend().attr("modified_ns").cast<int64_t>();
        constexpr int64_t second = 1'000'000'000;
        out->st_mtim.tv_sec = modified / second;
        out->st_mtim.tv_nsec = modified % second;
        if (out->st_mtim.tv_nsec < 0) {
            out->st_mtim.tv_nsec += second;
            --out->st_mtim.tv_sec;
        }
        if (std::strcmp(path, "/") == 0) {
            out->st_mode = S_IFDIR | 0500;
            out->st_nlink = 2;
            return 0;
        }
        out->st_size = backend().attr("size")(path + 1).cast<off_t>();
        out->st_mode = S_IFREG | 0400;
        out->st_nlink = 1;
        out->st_blocks = (out->st_size + 511) / 512;
        return 0;
    } catch (py::error_already_set& error) { return failure(error); }
}

int readdir_cb(const char* path, void* buffer, fuse_fill_dir_t fill, off_t offset,
               struct fuse_file_info*, enum fuse_readdir_flags) {
    if (std::strcmp(path, "/") != 0) return -ENOENT;
    py::gil_scoped_acquire gil;
    try {
        auto names = backend().attr("names")().cast<std::vector<std::string>>();
        names.insert(names.begin(), {".", ".."});
        for (size_t i = static_cast<size_t>(offset); i < names.size(); ++i) {
            if (fill(buffer, names[i].c_str(), nullptr, i + 1, static_cast<fuse_fill_dir_flags>(0))) break;
        }
        return 0;
    } catch (py::error_already_set& error) { return failure(error); }
}

int open_cb(const char* path, struct fuse_file_info* info) {
    if ((info->flags & O_ACCMODE) != O_RDONLY) return -EROFS;
    struct stat attr{};
    int result = getattr_cb(path, &attr, info);
    // Stable file contents permit the kernel's reclaimable page cache and memory-mapped readers.
    info->keep_cache = 1;
    if (result == 0) {
        py::gil_scoped_acquire gil;
        try { backend().attr("opened")(); }
        catch (py::error_already_set& error) { return failure(error); }
    }
    return result;
}

int release_cb(const char*, struct fuse_file_info*) {
    py::gil_scoped_acquire gil;
    try { backend().attr("released")(); return 0; }
    catch (py::error_already_set& error) { return failure(error); }
}

int opendir_cb(const char* path, struct fuse_file_info*) {
    if (std::strcmp(path, "/") != 0) return -ENOENT;
    py::gil_scoped_acquire gil;
    try { backend().attr("opened")(); return 0; }
    catch (py::error_already_set& error) { return failure(error); }
}

int read_cb(const char* path, char* buffer, size_t size, off_t offset, struct fuse_file_info*) {
    py::gil_scoped_acquire gil;
    try {
        auto result = backend().attr("read")(path + 1, offset, size).cast<py::buffer>();
        // Accept bytes and read-only contiguous pixel views without an intermediate
        // Python bytes allocation. buffer_info retains the exporter until after memcpy.
        auto view = result.request();
        if (view.ndim != 1 || view.itemsize != 1 || view.strides[0] != 1 ||
            view.size < 0 || static_cast<size_t>(view.size) > size) return -EIO;
        if (view.size) std::memcpy(buffer, view.ptr, view.size);
        return static_cast<int>(view.size);
    } catch (py::error_already_set& error) { return failure(error); }
}

void* init_cb(struct fuse_conn_info* connection, struct fuse_config*) {
    // Header padding covers this bounded read-ahead; full frame reads retain efficient batching.
    connection->max_readahead = 128 * 1024;
    py::gil_scoped_acquire gil;
    try { backend().attr("ready")(); }
    catch (py::error_already_set& error) { failure(error); }
    return fuse_get_context()->private_data;
}

void serve(py::object sequence, const std::string& path) {
    struct fuse_operations operations{};
    operations.getattr = getattr_cb;
    operations.readdir = readdir_cb;
    operations.open = open_cb;
    operations.release = release_cb;
    operations.opendir = opendir_cb;
    operations.releasedir = release_cb;
    operations.read = read_cb;
    operations.init = init_cb;
    char name[] = "mcrawsome";
    char option[] = "-o";
    char flags[] = "ro,nodev,nosuid,default_permissions,subtype=mcrawsome,fsname=mcrawsome";
    char* argv[] = {name, option, flags};
    struct fuse_args args = FUSE_ARGS_INIT(3, argv);
    py::gil_scoped_release release;
    // FUSE 3 parses the mount options here and applies them in fuse_mount.
    auto* fs = fuse_new(&args, &operations, sizeof(operations), &sequence);
    fuse_opt_free_args(&args);
    if (!fs) throw std::runtime_error("FUSE could not create the read-only filesystem");
    if (fuse_mount(fs, path.c_str()) != 0) {
        fuse_destroy(fs);
        throw std::runtime_error("FUSE could not mount the sequence; check /dev/fuse and fusermount3");
    }
    // One request at a time: Clip owns a seekable reader and its frame cache is bounded.
    const int result = fuse_loop(fs);
    fuse_unmount(fs);
    fuse_destroy(fs);
    if (result != 0) throw std::runtime_error("FUSE request loop failed");
}
}  // namespace

PYBIND11_MODULE(_fuse, module) {
    module.doc() = "Read-only FUSE adapter for on-demand CinemaDNG sequences";
    module.def("serve", &serve);
}
