/* Narrow POSIX host service for the Elisa IDE.
 *
 * Policy lives in Elisa; this file owns only what the language cannot express
 * safely: variadic open(2), the pollfd layout, posix_spawn file actions, and
 * waitpid. Every exported symbol is namespaced `designer_*` so it can never
 * collide with a libc name the compiler already knows about.
 *
 * Typed outcomes stay with the Elisa adapter (posix_file.elisa,
 * posix_process.elisa); the functions here return plain status values:
 *   >= 0 success (bytes, descriptor, or exit status)
 *   <  0 failure (POSIX errno negated where meaningful)
 */

#include <errno.h>
#include <fcntl.h>
#include <float.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifdef __APPLE__
#include <CoreGraphics/CoreGraphics.h>
#include <CoreFoundation/CoreFoundation.h>
#include <ImageIO/ImageIO.h>
#include <mach-o/dyld.h>
#endif

extern char** environ;

/* Resolve helper executables beside a development build or in an app bundle.
 * LaunchServices does not preserve the source checkout as its working folder. */
long designer_app_helper_path(const char* helper_name, char* output, size_t capacity) {
    if (helper_name == NULL || output == NULL || capacity == 0) return -EINVAL;
    size_t name_length = strnlen(helper_name, 128);
    if (name_length == 0 || name_length >= 128) return -EINVAL;
    for (size_t index = 0; index < name_length; index++) {
        unsigned char byte = (unsigned char)helper_name[index];
        if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
              (byte >= '0' && byte <= '9') || byte == '_' || byte == '-' || byte == '.'))
            return -EINVAL;
    }
#ifdef __APPLE__
    char executable[PATH_MAX];
    uint32_t executable_capacity = (uint32_t)sizeof(executable);
    if (_NSGetExecutablePath(executable, &executable_capacity) != 0) return -ENAMETOOLONG;
    char resolved_executable[PATH_MAX];
    if (realpath(executable, resolved_executable) == NULL) {
        size_t length = strnlen(executable, sizeof(executable));
        if (length == 0 || length >= sizeof(executable)) return -ENAMETOOLONG;
        memcpy(resolved_executable, executable, length + 1);
    }
    char* executable_directory_end = strrchr(resolved_executable, '/');
    if (executable_directory_end == NULL) return -EINVAL;
    *executable_directory_end = '\0';

    char candidate[PATH_MAX];
    int candidate_length = snprintf(candidate, sizeof(candidate), "%s/../Resources/%s",
                                    resolved_executable, helper_name);
    if (candidate_length >= 0 && (size_t)candidate_length < sizeof(candidate) && access(candidate, X_OK) == 0) {
        char resolved_candidate[PATH_MAX];
        if (realpath(candidate, resolved_candidate) != NULL) {
            size_t resolved_length = strlen(resolved_candidate);
            if (resolved_length >= capacity) return -ENAMETOOLONG;
            memcpy(output, resolved_candidate, resolved_length + 1);
            return 0;
        }
    }

    candidate_length = snprintf(candidate, sizeof(candidate), "%s/%s", resolved_executable, helper_name);
    if (candidate_length >= 0 && (size_t)candidate_length < sizeof(candidate) && access(candidate, X_OK) == 0) {
        char resolved_candidate[PATH_MAX];
        if (realpath(candidate, resolved_candidate) != NULL) {
            size_t resolved_length = strlen(resolved_candidate);
            if (resolved_length >= capacity) return -ENAMETOOLONG;
            memcpy(output, resolved_candidate, resolved_length + 1);
            return 0;
        }
    }
    return -ENOENT;
#else
    (void)name_length;
    return -ENOTSUP;
#endif
}

/* ------------------------------------------------------------------ stdio --- */

/* Raw byte I/O for the document host's request/reply protocol. The standard
 * library does not expose read/write, so the host uses these bounded wrappers
 * instead of declaring libc names that collide with compiler-known ones. */
long designer_stdin_read(void* out, size_t capacity) {
    ssize_t amount = read(STDIN_FILENO, out, capacity);
    if (amount < 0) return errno == EINTR ? 0 : -(long)errno;
    return (long)amount;
}

long designer_stdout_write(const void* bytes, size_t count) {
    const char* cursor = (const char*)bytes;
    size_t remaining = count;
    while (remaining > 0) {
        ssize_t written = write(STDOUT_FILENO, cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) continue;
            return -(long)errno;
        }
        cursor += written;
        remaining -= (size_t)written;
    }
    return (long)count;
}

/* ----------------------------------------------------------------- clock --- */

long designer_monotonic_ms(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (long)((int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000);
}

/* Peak resident set size in bytes. macOS reports bytes for ru_maxrss; Linux
 * reports kilobytes, so the unit is normalized here. */
long designer_rss_bytes(void) {
    struct rusage usage;
    if (getrusage(RUSAGE_SELF, &usage) != 0) return -1;
#ifdef __APPLE__
    return (long)usage.ru_maxrss;
#else
    return (long)usage.ru_maxrss * 1024;
#endif
}

/* Deterministic `label<number>` line for headless reports that must not depend
 * on the language's generic formatting path. */
void designer_report_i64(const char* label, long value) {
    printf("%s%ld\n", label, value);
    fflush(stdout);
}

void designer_report_text(const char* text) {
    printf("%s\n", text);
    fflush(stdout);
}

/* One geometry record per line: `rect <name> <x> <y> <width> <height>` with
 * integer logical units. The M00 worker transport is text so the parent needs
 * no parser library; the M04 protocol replaces this with a framed record. */
void designer_report_rect(const char* name, long x, long y, long width, long height) {
    printf("rect %s %ld %ld %ld %ld\n", name, x, y, width, height);
    fflush(stdout);
}

/* Exact Q10 geometry from the isolated preview worker. Each record carries
 * the revision and snapshot signature so the shell can reject stale output. */
void designer_report_geometry(const char* name, long revision, long signature,
                              long x_q10, long y_q10, long width_q10,
                              long height_q10, long visible, long selectable) {
    printf("geometry_v1 %ld %ld %s %ld %ld %ld %ld %ld %ld\n",
           revision, signature, name, x_q10, y_q10, width_q10, height_q10,
           visible, selectable);
    fflush(stdout);
}

/* Create request-private paths for one isolated preview worker. The private
 * directory is mode 0700 and has a random mkdtemp suffix, so two IDE sessions
 * and overlapping retries cannot replace each other's snapshot or PNG. The
 * caller owns the returned paths and must pass them to the paired cleanup
 * routine after the worker has exited and the image has been consumed. */
static int designer_copy_path(char* out, size_t capacity, const char* path) {
    if (out == NULL || capacity == 0 || path == NULL) return -EINVAL;
    size_t length = strlen(path);
    if (length >= capacity) return -ENAMETOOLONG;
    memcpy(out, path, length + 1);
    return 0;
}

long designer_preview_paths_create(char* input_path, size_t input_capacity,
                                   char* frame_path, size_t frame_capacity) {
    if (input_path == NULL || frame_path == NULL || input_capacity == 0 || frame_capacity == 0)
        return -EINVAL;
    input_path[0] = '\0';
    frame_path[0] = '\0';

    const char* temp_root = getenv("TMPDIR");
    if (temp_root == NULL || temp_root[0] == '\0') temp_root = "/tmp";
    size_t root_length = strlen(temp_root);
    int has_separator = root_length > 0 && temp_root[root_length - 1] == '/';
    static const char suffix[] = "elisa-ide-preview-XXXXXX";
    size_t template_capacity = root_length + (has_separator ? 1 : 2) + sizeof(suffix);
    char* directory = (char*)malloc(template_capacity);
    if (directory == NULL) return -ENOMEM;
    int template_length = snprintf(directory, template_capacity, "%s%selisa-ide-preview-XXXXXX",
                                   temp_root, has_separator ? "" : "/");
    if (template_length < 0 || (size_t)template_length >= template_capacity) {
        free(directory);
        return -ENAMETOOLONG;
    }
    if (mkdtemp(directory) == NULL) {
        long failure = -(long)errno;
        free(directory);
        return failure;
    }

    char input_candidate[PATH_MAX];
    char frame_candidate[PATH_MAX];
    int input_length = snprintf(input_candidate, sizeof(input_candidate), "%s/input.bin", directory);
    int frame_length = snprintf(frame_candidate, sizeof(frame_candidate), "%s/frame.png", directory);
    int failure = 0;
    if (input_length < 0 || (size_t)input_length >= sizeof(input_candidate) ||
        frame_length < 0 || (size_t)frame_length >= sizeof(frame_candidate)) {
        failure = -ENAMETOOLONG;
    } else {
        failure = designer_copy_path(input_path, input_capacity, input_candidate);
        if (failure == 0) failure = designer_copy_path(frame_path, frame_capacity, frame_candidate);
    }
    if (failure != 0) {
        input_path[0] = '\0';
        frame_path[0] = '\0';
        (void)rmdir(directory);
        free(directory);
        return failure;
    }
    free(directory);
    return 0;
}

/* Cleanup accepts only the exact paired basenames inside one generated
 * elisa-ide-preview-XXXXXX directory. It never recurses and will leave the
 * directory in place if unexpected files remain. */
long designer_preview_paths_cleanup(const char* input_path, const char* frame_path) {
    if (input_path == NULL || frame_path == NULL) return -EINVAL;
    size_t input_length = strnlen(input_path, PATH_MAX);
    size_t frame_length = strnlen(frame_path, PATH_MAX);
    static const char input_suffix[] = "/input.bin";
    static const char frame_suffix[] = "/frame.png";
    if (input_length >= PATH_MAX || frame_length >= PATH_MAX ||
        input_length <= sizeof(input_suffix) - 1 || frame_length <= sizeof(frame_suffix) - 1)
        return -EINVAL;
    size_t directory_length = input_length - (sizeof(input_suffix) - 1);
    if (frame_length - (sizeof(frame_suffix) - 1) != directory_length ||
        memcmp(input_path, frame_path, directory_length) != 0 ||
        strcmp(input_path + directory_length, input_suffix) != 0 ||
        strcmp(frame_path + directory_length, frame_suffix) != 0)
        return -EINVAL;
    const char* base = input_path + directory_length;
    while (base > input_path && base[-1] != '/') base--;
    static const char directory_prefix[] = "elisa-ide-preview-";
    size_t base_length = directory_length - (size_t)(base - input_path);
    if (base_length != sizeof(directory_prefix) - 1 + 6 ||
        memcmp(base, directory_prefix, sizeof(directory_prefix) - 1) != 0)
        return -EINVAL;
    for (size_t index = sizeof(directory_prefix) - 1; index < base_length; index++) {
        char byte = base[index];
        if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
              (byte >= '0' && byte <= '9'))) return -EINVAL;
    }

    struct stat directory_status;
    char directory[PATH_MAX];
    if (directory_length >= sizeof(directory)) return -ENAMETOOLONG;
    memcpy(directory, input_path, directory_length);
    directory[directory_length] = '\0';
    if (lstat(directory, &directory_status) != 0) return errno == ENOENT ? 0 : -(long)errno;
    if (!S_ISDIR(directory_status.st_mode) || S_ISLNK(directory_status.st_mode) ||
        directory_status.st_uid != geteuid() || (directory_status.st_mode & 077) != 0)
        return -EPERM;

    if (unlink(input_path) != 0 && errno != ENOENT) return -(long)errno;
    if (unlink(frame_path) != 0 && errno != ENOENT) return -(long)errno;
    return rmdir(directory) == 0 || errno == ENOENT ? 0 : -(long)errno;
}

/* Give each document-host process private request/reply files. The app's
 * working directory contains user projects and may be cloud-synchronized;
 * host traffic belongs in a short-lived, private local directory instead. */
long designer_host_ipc_paths_create(char* request_path, size_t request_capacity,
                                    char* reply_path, size_t reply_capacity) {
    if (request_path == NULL || reply_path == NULL || request_capacity == 0 || reply_capacity == 0)
        return -EINVAL;
    request_path[0] = '\0';
    reply_path[0] = '\0';

    const char* temp_root = getenv("TMPDIR");
    if (temp_root == NULL || temp_root[0] == '\0') temp_root = "/tmp";
    size_t root_length = strlen(temp_root);
    int has_separator = root_length > 0 && temp_root[root_length - 1] == '/';
    static const char suffix[] = "elisa-ide-host-XXXXXX";
    size_t template_capacity = root_length + (has_separator ? 1 : 2) + sizeof(suffix);
    char* directory = (char*)malloc(template_capacity);
    if (directory == NULL) return -ENOMEM;
    int template_length = snprintf(directory, template_capacity, "%s%selisa-ide-host-XXXXXX",
                                   temp_root, has_separator ? "" : "/");
    if (template_length < 0 || (size_t)template_length >= template_capacity) {
        free(directory);
        return -ENAMETOOLONG;
    }
    if (mkdtemp(directory) == NULL) {
        long failure = -(long)errno;
        free(directory);
        return failure;
    }

    char request_candidate[PATH_MAX];
    char reply_candidate[PATH_MAX];
    int request_length = snprintf(request_candidate, sizeof(request_candidate), "%s/request.txt", directory);
    int reply_length = snprintf(reply_candidate, sizeof(reply_candidate), "%s/reply.bin", directory);
    int failure = 0;
    if (request_length < 0 || (size_t)request_length >= sizeof(request_candidate) ||
        reply_length < 0 || (size_t)reply_length >= sizeof(reply_candidate)) {
        failure = -ENAMETOOLONG;
    } else {
        failure = designer_copy_path(request_path, request_capacity, request_candidate);
        if (failure == 0) failure = designer_copy_path(reply_path, reply_capacity, reply_candidate);
    }
    if (failure != 0) {
        request_path[0] = '\0';
        reply_path[0] = '\0';
        (void)rmdir(directory);
        free(directory);
        return failure;
    }
    free(directory);
    return 0;
}

/* Remove only the two known files inside a generated, owner-only host IPC
 * directory. Unexpected entries keep the directory in place for inspection. */
long designer_host_ipc_paths_cleanup(const char* request_path, const char* reply_path) {
    if (request_path == NULL || reply_path == NULL) return -EINVAL;
    size_t request_length = strnlen(request_path, PATH_MAX);
    size_t reply_length = strnlen(reply_path, PATH_MAX);
    static const char request_suffix[] = "/request.txt";
    static const char reply_suffix[] = "/reply.bin";
    if (request_length >= PATH_MAX || reply_length >= PATH_MAX ||
        request_length <= sizeof(request_suffix) - 1 || reply_length <= sizeof(reply_suffix) - 1)
        return -EINVAL;
    size_t directory_length = request_length - (sizeof(request_suffix) - 1);
    if (reply_length - (sizeof(reply_suffix) - 1) != directory_length ||
        memcmp(request_path, reply_path, directory_length) != 0 ||
        strcmp(request_path + directory_length, request_suffix) != 0 ||
        strcmp(reply_path + directory_length, reply_suffix) != 0)
        return -EINVAL;
    const char* base = request_path + directory_length;
    while (base > request_path && base[-1] != '/') base--;
    static const char directory_prefix[] = "elisa-ide-host-";
    size_t base_length = directory_length - (size_t)(base - request_path);
    if (base_length != sizeof(directory_prefix) - 1 + 6 ||
        memcmp(base, directory_prefix, sizeof(directory_prefix) - 1) != 0)
        return -EINVAL;
    for (size_t index = sizeof(directory_prefix) - 1; index < base_length; index++) {
        char byte = base[index];
        if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
              (byte >= '0' && byte <= '9'))) return -EINVAL;
    }

    struct stat directory_status;
    char directory[PATH_MAX];
    if (directory_length >= sizeof(directory)) return -ENAMETOOLONG;
    memcpy(directory, request_path, directory_length);
    directory[directory_length] = '\0';
    if (lstat(directory, &directory_status) != 0) return errno == ENOENT ? 0 : -(long)errno;
    if (!S_ISDIR(directory_status.st_mode) || S_ISLNK(directory_status.st_mode) ||
        directory_status.st_uid != geteuid() || (directory_status.st_mode & 077) != 0)
        return -EPERM;

    if (unlink(request_path) != 0 && errno != ENOENT) return -(long)errno;
    if (unlink(reply_path) != 0 && errno != ENOENT) return -(long)errno;
    return rmdir(directory) == 0 || errno == ENOENT ? 0 : -(long)errno;
}

/* FNV-1a is used here as a bounded corruption/cross-frame identity check, not
 * as an authentication primitive. The two 32-bit words keep the wire values
 * inside the shell's checked signed-decimal parser. */
static long designer_preview_file_identity(const char* path, size_t* length_out,
                                           uint32_t* hash_high_out, uint32_t* hash_low_out) {
    if (path == NULL || length_out == NULL || hash_high_out == NULL || hash_low_out == NULL)
        return -EINVAL;
    int descriptor = open(path, O_RDONLY);
    if (descriptor < 0) return -(long)errno;
    uint64_t hash = UINT64_C(14695981039346656037);
    size_t total = 0;
    unsigned char buffer[8192];
    for (;;) {
        ssize_t amount = read(descriptor, buffer, sizeof(buffer));
        if (amount < 0) {
            if (errno == EINTR) continue;
            long failure = -(long)errno;
            (void)close(descriptor);
            return failure;
        }
        if (amount == 0) break;
        if ((size_t)amount > SIZE_MAX - total) {
            (void)close(descriptor);
            return -EFBIG;
        }
        total += (size_t)amount;
        for (ssize_t index = 0; index < amount; index++) {
            hash ^= (uint64_t)buffer[index];
            hash *= UINT64_C(1099511628211);
        }
    }
    if (close(descriptor) != 0) return -(long)errno;
    if (total == 0) return -ENODATA;
    *length_out = total;
    *hash_high_out = (uint32_t)(hash >> 32);
    *hash_low_out = (uint32_t)hash;
    return 0;
}

/* This small rolling signature matches PreviewWire::snapshot_signature. It
 * stays in the native shim so the optimized UI and worker execute the exact
 * same byte loop even when they are compiled at different optimization levels. */
long designer_preview_snapshot_signature(const unsigned char* bytes, size_t length) {
    if (bytes == NULL && length != 0) return -EINVAL;
    int64_t signature = 17;
    for (size_t index = 0; index < length; index++)
        signature = (signature * 131 + bytes[index]) % INT64_C(2147483647);
    return signature == 0 ? 1 : (long)signature;
}

long designer_preview_frame_identity(const char* path, size_t* length_out,
                                     uint32_t* hash_high_out, uint32_t* hash_low_out) {
    return designer_preview_file_identity(path, length_out, hash_high_out, hash_low_out);
}

long designer_report_frame_identity(const char* path, long revision, long signature) {
    size_t length = 0;
    uint32_t hash_high = 0;
    uint32_t hash_low = 0;
    long result = designer_preview_file_identity(path, &length, &hash_high, &hash_low);
    if (result != 0) return result;
    printf("frame_v1 %ld %ld %zu %u %u\n", revision, signature, length, hash_high, hash_low);
    fflush(stdout);
    return 0;
}

long designer_setenv(const char* name, const char* value) {
    return setenv(name, value, 1) == 0 ? 0 : -(long)errno;
}

/* ---------------------------------------------------------------- files --- */

long designer_file_open_exclusive(const char* path) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_TRUNC, 0644);
    return fd < 0 ? -(long)errno : (long)fd;
}

long designer_file_open_directory(const char* path) {
    int fd = open(path, O_RDONLY);
    return fd < 0 ? -(long)errno : (long)fd;
}

long designer_file_write(long fd, const void* bytes, size_t count) {
    const char* cursor = (const char*)bytes;
    size_t remaining = count;
    while (remaining > 0) {
        ssize_t written = write((int)fd, cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) continue;
            return -(long)errno;
        }
        cursor += written;
        remaining -= (size_t)written;
    }
    return (long)count;
}

long designer_file_sync(long fd) {
    return fsync((int)fd) == 0 ? 0 : -(long)errno;
}

/* A one-shot fault hook for the safe-save regression test. It is consumed only
 * by directory sync, after atomic rename has already published the new file. */
static int designer_fail_next_directory_sync_for_test = 0;
static long designer_directory_sync_failure_count_for_test = 0;

void designer_test_fail_next_directory_sync(void) {
    designer_fail_next_directory_sync_for_test = 1;
}

long designer_test_directory_sync_failure_count(void) {
    return designer_directory_sync_failure_count_for_test;
}

long designer_file_sync_directory(long fd) {
    if (designer_fail_next_directory_sync_for_test) {
        designer_fail_next_directory_sync_for_test = 0;
        designer_directory_sync_failure_count_for_test++;
        errno = EIO;
        return -(long)errno;
    }
    return fsync((int)fd) == 0 ? 0 : -(long)errno;
}

long designer_file_read_is_not_found(long status) {
    return status == -(long)ENOENT ? 1 : 0;
}

/* Test-only permission helper so the safe-save suite can exercise an
 * unreadable existing destination while keeping its parent directory writable. */
long designer_test_set_file_mode(const char* path, long mode) {
    return chmod(path, (mode_t)mode) == 0 ? 0 : -(long)errno;
}

long designer_file_close(long fd) {
    return close((int)fd) == 0 ? 0 : -(long)errno;
}

long designer_file_rename(const char* from, const char* to) {
    return rename(from, to) == 0 ? 0 : -(long)errno;
}

/* Publish a completely-written sibling temporary file only if the requested
 * destination does not already exist. `link` is atomic and fails with EEXIST
 * for regular files and symlink directory entries, avoiding a check/rename
 * race without exposing partially-written destination contents. The caller
 * removes the temporary name after a successful link. */
long designer_file_link_no_replace(const char* from, const char* to) {
    return link(from, to) == 0 ? 0 : -(long)errno;
}

long designer_file_status_is_exists(long status) {
    return status == -(long)EEXIST ? 1 : 0;
}

long designer_file_mkdir(const char* path) {
    if (mkdir(path, 0755) == 0) return 0;
    return errno == EEXIST ? 0 : -(long)errno;
}

/* Unlike designer_file_mkdir, this reports success only when this call
 * exclusively created the directory. EEXIST remains an error so callers can
 * track which directories they own and may attempt to remove on rollback. */
long designer_file_mkdir_exclusive(const char* path) {
    return mkdir(path, 0755) == 0 ? 0 : -(long)errno;
}

/* Remove an empty directory only. rmdir fails if it is nonempty, so rollback
 * cannot recursively erase files another process placed in the directory. */
long designer_file_rmdir_empty(const char* path) {
    return rmdir(path) == 0 ? 0 : -(long)errno;
}

long designer_file_unlink(const char* path) {
    return unlink(path) == 0 ? 0 : -(long)errno;
}

/* Bounded whole-file read: copies at most `capacity` bytes and returns the
 * number copied, or negative errno. The caller owns the buffer and decides the
 * bound; no allocation crosses the boundary. */
long designer_file_read(const char* path, void* out, size_t capacity) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -(long)errno;
    char* cursor = (char*)out;
    size_t total = 0;
    for (;;) {
        if (total >= capacity) break;
        ssize_t amount = read(fd, cursor + total, capacity - total);
        if (amount < 0) {
            if (errno == EINTR) continue;
            int saved_error = errno;
            close(fd);
            return -(long)saved_error;
        }
        if (amount == 0) break;
        total += (size_t)amount;
    }
    close(fd);
    return (long)total;
}

/* Decode the first frame of a PNG to caller-owned RGBA8 storage. Rows are
 * returned top-to-bottom (row 0 is the image's top row); each pixel is four
 * consecutive R, G, B, A bytes. Alpha is straight/unassociated: RGB is
 * un-premultiplied before returning, and transparent pixels have RGB=0.
 *
 * This helper deliberately does not allocate the output buffer. The caller
 * supplies both its capacity and the three result pointers, and the decoded
 * dimensions/stride are validated against that capacity before ImageIO is
 * asked to materialize the image. The return ABI is 1 on success and negative
 * errno on failure. */
long designer_png_decode_rgba32(const char* path, unsigned char* output,
                                size_t capacity, size_t* width,
                                size_t* height, size_t* row_stride) {
#ifdef __APPLE__
    if (width != NULL) *width = 0;
    if (height != NULL) *height = 0;
    if (row_stride != NULL) *row_stride = 0;
    if (path == NULL || path[0] == '\0' || output == NULL || width == NULL ||
        height == NULL || row_stride == NULL) return -(long)EINVAL;

    const size_t path_length = strlen(path);
    if (path_length == 0 || path_length > LONG_MAX) return -(long)EINVAL;

    CFURLRef url = CFURLCreateFromFileSystemRepresentation(
        kCFAllocatorDefault, (const UInt8*)path, (CFIndex)path_length, false);
    if (url == NULL) return -(long)EINVAL;

    CGImageSourceRef source = CGImageSourceCreateWithURL(url, NULL);
    CFRelease(url);
    if (source == NULL) return -(long)EIO;

    long result = -(long)EIO;
    CFStringRef source_type = CGImageSourceGetType(source);
    if (source_type == NULL ||
        !CFEqual(source_type, CFSTR("public.png")) ||
        CGImageSourceGetCount(source) == 0) {
        result = -(long)EINVAL;
        goto cleanup_source;
    }

    CFDictionaryRef properties = CGImageSourceCopyPropertiesAtIndex(source, 0, NULL);
    if (properties == NULL) goto cleanup_source;
    CFNumberRef width_number = (CFNumberRef)CFDictionaryGetValue(properties, kCGImagePropertyPixelWidth);
    CFNumberRef height_number = (CFNumberRef)CFDictionaryGetValue(properties, kCGImagePropertyPixelHeight);
    int64_t image_width_i64 = 0;
    int64_t image_height_i64 = 0;
    if (width_number == NULL || height_number == NULL ||
        !CFNumberGetValue(width_number, kCFNumberSInt64Type, &image_width_i64) ||
        !CFNumberGetValue(height_number, kCFNumberSInt64Type, &image_height_i64) ||
        image_width_i64 <= 0 || image_height_i64 <= 0 ||
        (uint64_t)image_width_i64 > (uint64_t)SIZE_MAX ||
        (uint64_t)image_height_i64 > (uint64_t)SIZE_MAX) {
        CFRelease(properties);
        result = -(long)EINVAL;
        goto cleanup_source;
    }

    const size_t image_width = (size_t)image_width_i64;
    const size_t image_height = (size_t)image_height_i64;
    if (image_width > SIZE_MAX / 4) {
        CFRelease(properties);
        result = -(long)EOVERFLOW;
        goto cleanup_source;
    }
    const size_t stride = image_width * 4;
    if (image_height > SIZE_MAX / stride) {
        CFRelease(properties);
        result = -(long)EOVERFLOW;
        goto cleanup_source;
    }
    const size_t byte_count = stride * image_height;
    CFRelease(properties);
    if (byte_count > capacity) {
        result = -(long)ENOSPC;
        goto cleanup_source;
    }

    CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
    if (image == NULL) goto cleanup_source;
    if (CGImageGetWidth(image) != image_width || CGImageGetHeight(image) != image_height) {
        CGImageRelease(image);
        result = -(long)EIO;
        goto cleanup_source;
    }

    CGColorSpaceRef color_space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    if (color_space == NULL) {
        CGImageRelease(image);
        result = -(long)ENOTSUP;
        goto cleanup_source;
    }
    CGBitmapInfo bitmap_info = (CGBitmapInfo)(kCGImageAlphaPremultipliedLast |
                                               kCGBitmapByteOrder32Big);
    CGContextRef context = CGBitmapContextCreate(output, image_width, image_height,
                                                  8, stride, color_space, bitmap_info);
    CGColorSpaceRelease(color_space);
    if (context == NULL) {
        CGImageRelease(image);
        result = -(long)ENOMEM;
        goto cleanup_source;
    }

    /* ImageIO's CGImage-to-bitmap drawing already places the PNG's first row
     * at row zero in this context's byte buffer. Preserve that top-down order;
     * applying another y-flip reverses the preview before Skia uploads it. */
    CGContextSetBlendMode(context, kCGBlendModeCopy);
    CGContextDrawImage(context, CGRectMake(0.0, 0.0, (CGFloat)image_width,
                                           (CGFloat)image_height), image);
    CGContextRelease(context);
    CGImageRelease(image);

    /* The bitmap context uses premultiplied alpha internally, which Core
     * Graphics requires. Convert in place to straight RGBA8 for renderer upload. */
    for (size_t offset = 0; offset < byte_count; offset += 4) {
        const unsigned int alpha = output[offset + 3];
        if (alpha == 0) {
            output[offset] = 0;
            output[offset + 1] = 0;
            output[offset + 2] = 0;
        } else if (alpha < 255) {
            for (size_t channel = 0; channel < 3; channel++) {
                unsigned int value = output[offset + channel];
                value = (value * 255u + alpha / 2u) / alpha;
                output[offset + channel] = (unsigned char)(value > 255u ? 255u : value);
            }
        }
    }

    *width = image_width;
    *height = image_height;
    *row_stride = stride;
    result = 1;

cleanup_source:
    CFRelease(source);
    return result;
#else
    (void)path;
    (void)output;
    (void)capacity;
    if (width != NULL) *width = 0;
    if (height != NULL) *height = 0;
    if (row_stride != NULL) *row_stride = 0;
    return -1;
#endif
}

/* ------------------------------------------------------- designer settings --- */

#define DESIGNER_CONFIG_PATH_CAP ((size_t)4096)
#define DESIGNER_PREFERENCES_CAP ((size_t)1048576)

typedef struct DesignerJsonSlice {
    const char* data;
    size_t length;
} DesignerJsonSlice;

static const char* designer_json_space(const char* cursor) {
    while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' || *cursor == '\n') cursor++;
    return cursor;
}

static int designer_json_hex(char byte) {
    return (byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f') || (byte >= 'A' && byte <= 'F');
}

static const char* designer_json_string_end(const char* cursor) {
    if (*cursor != '"') return NULL;
    cursor++;
    while (*cursor != '\0') {
        unsigned char byte = (unsigned char)*cursor;
        if (byte == '"') return cursor + 1;
        if (byte < 0x20) return NULL;
        if (byte == '\\') {
            cursor++;
            if (*cursor == '\0') return NULL;
            if (*cursor == 'u') {
                for (int index = 1; index <= 4; index++) {
                    if (cursor[index] == '\0' || !designer_json_hex(cursor[index])) return NULL;
                }
                cursor += 5;
                continue;
            }
            if (*cursor != '"' && *cursor != '\\' && *cursor != '/' && *cursor != 'b' && *cursor != 'f' &&
                *cursor != 'n' && *cursor != 'r' && *cursor != 't') return NULL;
        }
        cursor++;
    }
    return NULL;
}

static int designer_json_number_end(const char** cursor_ptr) {
    const char* cursor = *cursor_ptr;
    if (*cursor == '-') cursor++;
    if (*cursor == '0') {
        cursor++;
        if (*cursor >= '0' && *cursor <= '9') return 0;
    } else {
        if (*cursor < '1' || *cursor > '9') return 0;
        while (*cursor >= '0' && *cursor <= '9') cursor++;
    }
    if (*cursor == '.') {
        cursor++;
        if (*cursor < '0' || *cursor > '9') return 0;
        while (*cursor >= '0' && *cursor <= '9') cursor++;
    }
    if (*cursor == 'e' || *cursor == 'E') {
        cursor++;
        if (*cursor == '+' || *cursor == '-') cursor++;
        if (*cursor < '0' || *cursor > '9') return 0;
        while (*cursor >= '0' && *cursor <= '9') cursor++;
    }
    *cursor_ptr = cursor;
    return 1;
}

static const char* designer_json_value_end(const char* cursor, int depth) {
    cursor = designer_json_space(cursor);
    if (depth > 64) return NULL;
    if (*cursor == '"') return designer_json_string_end(cursor);
    if (*cursor == '{' || *cursor == '[') {
        char close = *cursor == '{' ? '}' : ']';
        char open = *cursor++;
        cursor = designer_json_space(cursor);
        if (*cursor == close) return cursor + 1;
        for (;;) {
            if (open == '{') {
                const char* key_end = designer_json_string_end(cursor);
                if (key_end == NULL) return NULL;
                cursor = designer_json_space(key_end);
                if (*cursor++ != ':') return NULL;
            }
            cursor = designer_json_value_end(cursor, depth + 1);
            if (cursor == NULL) return NULL;
            cursor = designer_json_space(cursor);
            if (*cursor == close) return cursor + 1;
            if (*cursor++ != ',') return NULL;
            cursor = designer_json_space(cursor);
        }
    }
    if (strncmp(cursor, "true", 4) == 0) return cursor + 4;
    if (strncmp(cursor, "false", 5) == 0) return cursor + 5;
    if (strncmp(cursor, "null", 4) == 0) return cursor + 4;
    return designer_json_number_end(&cursor) ? cursor : NULL;
}

static int designer_json_key_equals(const char* start, const char* end, const char* key) {
    size_t key_length = strlen(key);
    return (size_t)(end - start) == key_length && memcmp(start, key, key_length) == 0;
}

/* Returns 1 when found, 0 when absent, and -1 for malformed/duplicate keys. */
static int designer_json_member(const char* object, const char* key, DesignerJsonSlice* value) {
    const char* cursor = designer_json_space(object);
    if (*cursor++ != '{') return -1;
    cursor = designer_json_space(cursor);
    int found = 0;
    if (*cursor == '}') return 0;
    for (;;) {
        if (*cursor != '"') return -1;
        const char* key_start = cursor + 1;
        const char* key_after = designer_json_string_end(cursor);
        if (key_after == NULL) return -1;
        const char* key_end = key_after - 1;
        cursor = designer_json_space(key_after);
        if (*cursor++ != ':') return -1;
        const char* value_start = designer_json_space(cursor);
        const char* value_end = designer_json_value_end(value_start, 0);
        if (value_end == NULL) return -1;
        if (designer_json_key_equals(key_start, key_end, key)) {
            if (found) return -1;
            found = 1;
            value->data = value_start;
            value->length = (size_t)(value_end - value_start);
        }
        cursor = designer_json_space(value_end);
        if (*cursor == '}') return found;
        if (*cursor++ != ',') return -1;
        cursor = designer_json_space(cursor);
    }
}

static int designer_json_document_valid(const char* json) {
    const char* end = designer_json_value_end(json, 0);
    return end != NULL && *designer_json_space(end) == '\0' && *designer_json_space(json) == '{';
}

static int designer_json_slice_string_equals(DesignerJsonSlice value, const char* expected) {
    size_t expected_length = strlen(expected);
    return value.length == expected_length + 2 && value.data[0] == '"' && value.data[value.length - 1] == '"' &&
        memcmp(value.data + 1, expected, expected_length) == 0;
}

static int designer_preferences_root(const char* json, DesignerJsonSlice* recent, DesignerJsonSlice* theme,
                                     DesignerJsonSlice* panes) {
    if (!designer_json_document_valid(json)) return 0;
    DesignerJsonSlice format = {0};
    DesignerJsonSlice version = {0};
    int has_format = designer_json_member(json, "format", &format);
    int has_version = designer_json_member(json, "version", &version);
    int has_recent = designer_json_member(json, "recentFiles", recent);
    int has_theme = designer_json_member(json, "theme", theme);
    int has_panes = designer_json_member(json, "panes", panes);
    if (has_format != 1 || has_version != 1 || has_recent < 0 || has_theme < 0 || has_panes < 0) return 0;
    if (!designer_json_slice_string_equals(format, "elisa-ide-workspace")) return 0;
    if (version.length != 1 || version.data[0] != '1') return 0;
    return 1;
}

static double designer_preferences_ratio(DesignerJsonSlice panes, const char* key, double fallback) {
    DesignerJsonSlice value = {0};
    int found = designer_json_member(panes.data, key, &value);
    if (found != 1 || value.length == 0 || value.length > 64) return fallback;
    char number[65];
    memcpy(number, value.data, value.length);
    number[value.length] = '\0';
    char* end = NULL;
    double parsed = strtod(number, &end);
    if (end == number || *designer_json_space(end) != '\0') return fallback;
    if (!(parsed >= 0.08 && parsed <= 0.45)) return fallback;
    return parsed;
}

static int designer_preferences_path_valid(const char* value, int require_absolute) {
    if (value == NULL || value[0] == '\0') return 0;
    if (require_absolute && value[0] != '/') return 0;
    size_t length = 0;
    while (value[length] != '\0') {
        unsigned char byte = (unsigned char)value[length];
        if (byte < 0x20 || byte == 0x7f) return 0;
        if (++length >= DESIGNER_CONFIG_PATH_CAP) return 0;
    }
    return 1;
}

static int designer_preferences_path_join(char* out, size_t capacity, const char* base, const char* suffix) {
    size_t length = strlen(base);
    int separator = length > 0 && base[length - 1] != '/';
    int amount = snprintf(out, capacity, "%s%s%s", base, separator ? "/" : "", suffix);
    return amount >= 0 && (size_t)amount < capacity;
}

static int designer_preferences_config_path(char* out, size_t capacity) {
    if (out == NULL || capacity == 0) return 0;
    out[0] = '\0';
    const char* explicit_path = getenv("ELISA_IDE_CONFIG");
    if (!designer_preferences_path_valid(explicit_path, 0))
        explicit_path = getenv("ELISA_UI_DESIGNER_CONFIG");
    if (designer_preferences_path_valid(explicit_path, 0) && strlen(explicit_path) < capacity) {
        memcpy(out, explicit_path, strlen(explicit_path) + 1);
        return 1;
    }
    const char* xdg = getenv("XDG_CONFIG_HOME");
    if (designer_preferences_path_valid(xdg, 1) &&
        designer_preferences_path_join(out, capacity, xdg, "elisa-ide/preferences.json")) return 1;
    const char* home = getenv("HOME");
    if (designer_preferences_path_valid(home, 1) &&
        designer_preferences_path_join(out, capacity, home, ".config/elisa-ide/preferences.json")) return 1;
    return 0;
}

static int designer_preferences_make_directory(const char* path) {
    if (mkdir(path, 0700) == 0) return 1;
    if (errno != EEXIST) return 0;
    struct stat status;
    return stat(path, &status) == 0 && S_ISDIR(status.st_mode);
}

static int designer_preferences_ensure_parent(const char* file_path) {
    size_t length = strlen(file_path);
    if (length == 0 || length >= DESIGNER_CONFIG_PATH_CAP) return 0;
    char parent[DESIGNER_CONFIG_PATH_CAP];
    memcpy(parent, file_path, length + 1);
    char* slash = strrchr(parent, '/');
    if (slash == NULL) return 1;
    if (slash == parent) return 1;
    *slash = '\0';
    size_t start = parent[0] == '/' ? 1 : 0;
    for (size_t index = start; parent[index] != '\0'; index++) {
        if (parent[index] == '/') {
            parent[index] = '\0';
            if (parent[0] != '\0' && !designer_preferences_make_directory(parent)) return 0;
            parent[index] = '/';
        }
    }
    return designer_preferences_make_directory(parent);
}

static long designer_preferences_read_file(const char* path, char** contents_out) {
    *contents_out = NULL;
    int descriptor = open(path, O_RDONLY);
    if (descriptor < 0) return -(long)errno;
    char* contents = (char*)malloc(DESIGNER_PREFERENCES_CAP + 1);
    if (contents == NULL) {
        close(descriptor);
        return -ENOMEM;
    }
    size_t total = 0;
    while (total <= DESIGNER_PREFERENCES_CAP) {
        size_t available = DESIGNER_PREFERENCES_CAP + 1 - total;
        ssize_t amount = read(descriptor, contents + total, available);
        if (amount < 0) {
            if (errno == EINTR) continue;
            int saved_error = errno;
            close(descriptor);
            free(contents);
            return -(long)saved_error;
        }
        if (amount == 0) break;
        total += (size_t)amount;
    }
    close(descriptor);
    if (total > DESIGNER_PREFERENCES_CAP) {
        free(contents);
        return -EFBIG;
    }
    contents[total] = '\0';
    *contents_out = contents;
    return (long)total;
}

static int designer_preferences_write_atomic(const char* path, const char* bytes, size_t length) {
    char temporary[DESIGNER_CONFIG_PATH_CAP];
    int temp_length = snprintf(temporary, sizeof(temporary), "%s.tmp", path);
    if (temp_length < 0 || (size_t)temp_length >= sizeof(temporary)) return 0;
    int descriptor = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_TRUNC, 0600);
    if (descriptor < 0) return 0;
    size_t written = 0;
    while (written < length) {
        ssize_t amount = write(descriptor, bytes + written, length - written);
        if (amount < 0) {
            if (errno == EINTR) continue;
            close(descriptor);
            unlink(temporary);
            return 0;
        }
        written += (size_t)amount;
    }
    int sync_result = fsync(descriptor);
    int close_result = close(descriptor);
    if (sync_result != 0 || close_result != 0) {
        unlink(temporary);
        return 0;
    }
    if (rename(temporary, path) != 0) {
        unlink(temporary);
        return 0;
    }
    char parent[DESIGNER_CONFIG_PATH_CAP];
    memcpy(parent, path, strlen(path) + 1);
    char* slash = strrchr(parent, '/');
    if (slash != NULL) {
        if (slash == parent) slash[1] = '\0';
        else *slash = '\0';
        int directory = open(parent, O_RDONLY);
        if (directory >= 0) {
            (void)fsync(directory);
            close(directory);
        }
    }
    return 1;
}

void designer_workspace_preferences_load_ratios(double* left, double* right, double* bottom) {
    if (left == NULL || right == NULL || bottom == NULL) return;
    *left = 0.22;
    *right = 0.25;
    *bottom = 0.20;
    char path[DESIGNER_CONFIG_PATH_CAP];
    if (!designer_preferences_config_path(path, sizeof(path))) return;
    char* contents = NULL;
    long amount = designer_preferences_read_file(path, &contents);
    if (amount < 0 || contents == NULL) return;
    DesignerJsonSlice recent = {0};
    DesignerJsonSlice theme = {0};
    DesignerJsonSlice panes = {0};
    if (designer_preferences_root(contents, &recent, &theme, &panes) && panes.length > 0 && panes.data[0] == '{') {
        *left = designer_preferences_ratio(panes, "left", *left);
        *right = designer_preferences_ratio(panes, "right", *right);
        *bottom = designer_preferences_ratio(panes, "bottom", *bottom);
    }
    free(contents);
}

long designer_workspace_preferences_save_ratios(double left, double right, double bottom) {
    if (!(left >= 0.08 && left <= 0.45) || !(right >= 0.08 && right <= 0.45) || !(bottom >= 0.08 && bottom <= 0.45)) return 0;
    char path[DESIGNER_CONFIG_PATH_CAP];
    if (!designer_preferences_config_path(path, sizeof(path)) || !designer_preferences_ensure_parent(path)) return 0;

    const char* recent_json = "[]";
    const char* theme_json = "\"system\"";
    size_t recent_length = 2;
    size_t theme_length = strlen(theme_json);
    char* existing = NULL;
    long existing_length = designer_preferences_read_file(path, &existing);
    if (existing_length >= 0) {
        DesignerJsonSlice recent = {0};
        DesignerJsonSlice theme = {0};
        DesignerJsonSlice panes = {0};
        if (!designer_preferences_root(existing, &recent, &theme, &panes)) {
            free(existing);
            return 0;
        }
        if (recent.length > 0) {
            if (recent.data[0] != '[') {
                free(existing);
                return 0;
            }
            recent_json = recent.data;
            recent_length = recent.length;
        }
        if (theme.length > 0) {
            if (!designer_json_slice_string_equals(theme, "system") &&
                !designer_json_slice_string_equals(theme, "light") &&
                !designer_json_slice_string_equals(theme, "dark")) {
                free(existing);
                return 0;
            }
            theme_json = theme.data;
            theme_length = theme.length;
        }
    } else if (existing_length != -ENOENT) {
        return 0;
    }

    size_t output_capacity = DESIGNER_PREFERENCES_CAP + 256;
    char* output = (char*)malloc(output_capacity);
    if (output == NULL) {
        free(existing);
        return 0;
    }
    int output_length = snprintf(output, output_capacity,
        "{\"format\":\"elisa-ide-workspace\",\"version\":1,\"recentFiles\":%.*s,\"theme\":%.*s,\"panes\":{\"left\":%.17g,\"right\":%.17g,\"bottom\":%.17g}}",
        (int)recent_length, recent_json, (int)theme_length, theme_json, left, right, bottom);
    int saved = output_length > 0 && (size_t)output_length < output_capacity &&
        designer_preferences_write_atomic(path, output, (size_t)output_length);
    free(output);
    free(existing);
    return saved ? 1 : 0;
}

/* -------------------------------------------------------------- process --- */

#define DESIGNER_STREAM_CAP ((size_t)4 * 1024 * 1024)

typedef struct DesignerProcess {
    pid_t pid;
    int in_fd;
    int out_fd;
    int err_fd;
    char* out;
    size_t out_len;
    size_t out_read;
    char* err;
    size_t err_len;
    size_t err_read;
    int truncated;
    int finished;
    int cancelled;
    int status;
    int spawn_failed;
} DesignerProcess;

/* Keep the service's pipe endpoints out of unrelated descendants. The child
 * receives only stdin/stdout/stderr through posix_spawn's dup2 actions; those
 * standard descriptors intentionally survive exec. */
static int designer_pipe_cloexec(int fds[2]) {
    if (pipe(fds) != 0) return -1;
    for (size_t index = 0; index < 2; index++) {
        int flags = fcntl(fds[index], F_GETFD);
        if (flags < 0 || fcntl(fds[index], F_SETFD, flags | FD_CLOEXEC) < 0) {
            int saved_error = errno;
            close(fds[0]);
            close(fds[1]);
            fds[0] = -1;
            fds[1] = -1;
            errno = saved_error;
            return -1;
        }
    }
    return 0;
}

static void designer_process_drain(DesignerProcess* p) {
    char scratch[4096];
    int fds[2];
    int count = 0;
    if (p->out_fd >= 0) fds[count++] = p->out_fd;
    if (p->err_fd >= 0) fds[count++] = p->err_fd;
    for (int i = 0; i < count; i++) {
        int fd = fds[i];
        for (;;) {
            ssize_t amount = read(fd, scratch, sizeof(scratch));
            if (amount > 0) {
                char** buffer = (fd == p->out_fd) ? &p->out : &p->err;
                size_t* length = (fd == p->out_fd) ? &p->out_len : &p->err_len;
                if (*length + (size_t)amount > DESIGNER_STREAM_CAP) {
                    size_t room = DESIGNER_STREAM_CAP - *length;
                    if (room > 0) {
                        memcpy(*buffer + *length, scratch, room);
                        *length += room;
                    }
                    p->truncated = 1;
                } else {
                    memcpy(*buffer + *length, scratch, (size_t)amount);
                    *length += (size_t)amount;
                }
                continue;
            }
            if (amount == 0) {
                close(fd);
                if (fd == p->out_fd) p->out_fd = -1;
                if (fd == p->err_fd) p->err_fd = -1;
            }
            /* amount < 0: EAGAIN or EINTR mean come back later. */
            break;
        }
    }
}

static void designer_process_reap(DesignerProcess* p, int block) {
    if (p->finished) return;
    int status = 0;
    pid_t result = waitpid(p->pid, &status, block ? 0 : WNOHANG);
    if (result == p->pid) {
        p->status = status;
        p->finished = 1;
    }
}

void* designer_process_start(const char* const* argv, size_t argc) {
    if (argc == 0 || argv == NULL) return NULL;
    char** spawn_argv = (char**)calloc(argc + 1, sizeof(char*));
    if (spawn_argv == NULL) return NULL;
    for (size_t i = 0; i < argc; i++) spawn_argv[i] = (char*)argv[i];

    int in_pipe[2] = { -1, -1 };
    int out_pipe[2] = { -1, -1 };
    int err_pipe[2] = { -1, -1 };
    if (designer_pipe_cloexec(in_pipe) != 0 ||
        designer_pipe_cloexec(out_pipe) != 0 ||
        designer_pipe_cloexec(err_pipe) != 0) {
        free(spawn_argv);
        if (in_pipe[0] >= 0) { close(in_pipe[0]); close(in_pipe[1]); }
        if (out_pipe[0] >= 0) { close(out_pipe[0]); close(out_pipe[1]); }
        if (err_pipe[0] >= 0) { close(err_pipe[0]); close(err_pipe[1]); }
        return NULL;
    }

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, in_pipe[0], STDIN_FILENO);
    posix_spawn_file_actions_adddup2(&actions, out_pipe[1], STDOUT_FILENO);
    posix_spawn_file_actions_adddup2(&actions, err_pipe[1], STDERR_FILENO);
    posix_spawn_file_actions_addclose(&actions, in_pipe[0]);
    posix_spawn_file_actions_addclose(&actions, in_pipe[1]);
    posix_spawn_file_actions_addclose(&actions, out_pipe[0]);
    posix_spawn_file_actions_addclose(&actions, err_pipe[0]);
    posix_spawn_file_actions_addclose(&actions, out_pipe[1]);
    posix_spawn_file_actions_addclose(&actions, err_pipe[1]);

    pid_t pid = -1;
    int spawn_rc = posix_spawnp(&pid, spawn_argv[0], &actions, NULL, spawn_argv, environ);
    posix_spawn_file_actions_destroy(&actions);
    close(in_pipe[0]);
    close(out_pipe[1]);
    close(err_pipe[1]);
    free(spawn_argv);
    if (spawn_rc != 0) {
        close(in_pipe[1]);
        close(out_pipe[0]);
        close(err_pipe[0]);
        return NULL;
    }

    DesignerProcess* p = (DesignerProcess*)calloc(1, sizeof(DesignerProcess));
    if (p == NULL) {
        kill(pid, SIGKILL);
        waitpid(pid, NULL, 0);
        close(in_pipe[1]);
        close(out_pipe[0]);
        close(err_pipe[0]);
        return NULL;
    }
    p->pid = pid;
    p->in_fd = in_pipe[1];
    p->out_fd = out_pipe[0];
    p->err_fd = err_pipe[0];
    p->out = (char*)malloc(DESIGNER_STREAM_CAP);
    p->err = (char*)malloc(DESIGNER_STREAM_CAP);
    if (p->out == NULL || p->err == NULL) {
        kill(pid, SIGKILL);
        waitpid(pid, NULL, 0);
        close(p->in_fd);
        close(p->out_fd);
        close(p->err_fd);
        free(p->out);
        free(p->err);
        free(p);
        return NULL;
    }
    fcntl(p->out_fd, F_SETFL, O_NONBLOCK);
    fcntl(p->err_fd, F_SETFL, O_NONBLOCK);
    return p;
}

typedef struct DesignerEditorReaper {
    pid_t pid;
} DesignerEditorReaper;

static void* designer_editor_reap(void* opaque) {
    DesignerEditorReaper* child = (DesignerEditorReaper*)opaque;
    int status = 0;
    pid_t result;
    do {
        result = waitpid(child->pid, &status, 0);
    } while (result < 0 && errno == EINTR);
    free(child);
    return NULL;
}

static long designer_launch_detached(const char* executable, char* const* argv) {
    pid_t pid = -1;
    int spawn_rc = posix_spawnp(&pid, executable, NULL, NULL, (char* const*)argv, environ);
    if (spawn_rc != 0) return -(long)spawn_rc;

    DesignerEditorReaper* child = (DesignerEditorReaper*)malloc(sizeof(*child));
    if (child == NULL) {
        kill(pid, SIGKILL);
        while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) { }
        return -ENOMEM;
    }
    child->pid = pid;

    pthread_attr_t attributes;
    int thread_rc = pthread_attr_init(&attributes);
    if (thread_rc == 0) {
        thread_rc = pthread_attr_setdetachstate(&attributes, PTHREAD_CREATE_DETACHED);
        if (thread_rc == 0) {
            pthread_t thread;
            thread_rc = pthread_create(&thread, &attributes, designer_editor_reap, child);
        }
        pthread_attr_destroy(&attributes);
    }
    if (thread_rc != 0) {
        kill(pid, SIGKILL);
        while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) { }
        free(child);
        return -(long)thread_rc;
    }
    return 0;
}

/* Launch a user-configured editor without a shell. The path and one-based
 * line are distinct arguments, so spaces and shell metacharacters in either
 * value remain ordinary filename text. A detached waiter reaps editors that
 * stay open after the IDE returns to its event loop. */
long designer_editor_launch(const char* editor, const char* path, long line) {
    if (editor == NULL || path == NULL || editor[0] == '\0' || path[0] == '\0' || line <= 0)
        return -EINVAL;
    size_t editor_length = strnlen(editor, PATH_MAX);
    size_t path_length = strnlen(path, PATH_MAX);
    if (editor_length == 0 || editor_length >= PATH_MAX ||
        path_length == 0 || path_length >= PATH_MAX)
        return -ENAMETOOLONG;
    for (size_t index = 0; index < editor_length; index++) {
        unsigned char byte = (unsigned char)editor[index];
        if (byte < 32 || byte == 127) return -EINVAL;
    }
    for (size_t index = 0; index < path_length; index++) {
        unsigned char byte = (unsigned char)path[index];
        if (byte < 32 || byte == 127) return -EINVAL;
    }

    char line_argument[32];
    int line_length = snprintf(line_argument, sizeof(line_argument), "%ld", line);
    if (line_length <= 0 || (size_t)line_length >= sizeof(line_argument)) return -ERANGE;
    char* argv[] = {(char*)editor, (char*)path, line_argument, NULL};
    return designer_launch_detached(editor, argv);
}

/* Ask Finder to reveal the opened file. The source path is a single argv item;
 * paths with spaces cannot be reinterpreted as shell syntax. */
long designer_reveal_file(const char* path) {
    if (path == NULL || path[0] == '\0') return -EINVAL;
    size_t path_length = strnlen(path, PATH_MAX);
    if (path_length == 0 || path_length >= PATH_MAX) return -ENAMETOOLONG;
    for (size_t index = 0; index < path_length; index++) {
        unsigned char byte = (unsigned char)path[index];
        if (byte < 32 || byte == 127) return -EINVAL;
    }
#ifdef __APPLE__
    char* argv[] = {(char*)"open", (char*)"-R", (char*)path, NULL};
    return designer_launch_detached("open", argv);
#else
    return -ENOTSUP;
#endif
}

/* Write `count` bytes to the child's stdin. Returns bytes written or negative
 * errno. The pipe is not non-blocking: a bounded message is small and the host
 * reads continuously, so a short blocking write is acceptable and simpler than
 * a partial-write state machine. */
long designer_process_write(void* handle, const void* bytes, size_t count) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL || p->in_fd < 0) return -1;
    const char* cursor = (const char*)bytes;
    size_t remaining = count;
    while (remaining > 0) {
        ssize_t written = write(p->in_fd, cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) continue;
            return -(long)errno;
        }
        cursor += written;
        remaining -= (size_t)written;
    }
    return (long)count;
}

/* Close the child's stdin, signalling end of requests. */
long designer_process_close_stdin(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL || p->in_fd < 0) return -1;
    int result = close(p->in_fd);
    p->in_fd = -1;
    return result == 0 ? 0 : -(long)errno;
}

long designer_process_poll(void* handle, long timeout_ms) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return -1;
    if (p->finished) return 1;

    /* Child lifetime and output-pipe lifetime are independent. A descendant
     * can inherit standard output or stderr and keep that pipe open after the
     * managed child exits, so always check waitpid before possibly blocking on
     * pipe readiness. */
    designer_process_reap(p, 0);
    if (p->finished) {
        designer_process_drain(p);
        return 1;
    }

    struct pollfd fds[2];
    int count = 0;
    if (p->out_fd >= 0) { fds[count].fd = p->out_fd; fds[count].events = POLLIN; count++; }
    if (p->err_fd >= 0) { fds[count].fd = p->err_fd; fds[count].events = POLLIN; count++; }
    if (count > 0) {
        int wait_ms = timeout_ms < 0 ? -1 : (timeout_ms > 1000 ? 1000 : (int)timeout_ms);
        poll(fds, (nfds_t)count, wait_ms);
    }
    designer_process_drain(p);
    designer_process_reap(p, 0);
    return p->finished ? 1 : 0;
}

unsigned long designer_process_stdout(void* handle, void* out, size_t capacity) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return 0;
    size_t unread = p->out_len - p->out_read;
    size_t amount = unread < capacity ? unread : capacity;
    if (amount > 0 && out != NULL) memcpy(out, p->out + p->out_read, amount);
    p->out_read += amount;
    return (unsigned long)amount;
}

unsigned long designer_process_stderr(void* handle, void* out, size_t capacity) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return 0;
    size_t unread = p->err_len - p->err_read;
    size_t amount = unread < capacity ? unread : capacity;
    if (amount > 0 && out != NULL) memcpy(out, p->err + p->err_read, amount);
    p->err_read += amount;
    return (unsigned long)amount;
}

long designer_process_cancel(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return -1;
    if (!p->finished) {
        p->cancelled = 1;
        kill(p->pid, SIGTERM);
        for (int tick = 0; tick < 50 && !p->finished; tick++) designer_process_poll(p, 10);
        if (!p->finished) {
            kill(p->pid, SIGKILL);
            for (int tick = 0; tick < 100 && !p->finished; tick++) designer_process_poll(p, 10);
        }
        designer_process_reap(p, 1);
    }
    return p->status;
}

long designer_process_finished(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return 0;
    designer_process_poll(p, 0);
    return p->finished;
}

long designer_process_exit_code(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return -1;
    if (!p->finished) return -1;
    if (WIFEXITED(p->status)) return (long)WEXITSTATUS(p->status);
    return -1;
}

long designer_process_term_signal(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return 0;
    if (!p->finished) return 0;
    return WIFSIGNALED(p->status) ? (long)WTERMSIG(p->status) : 0;
}

long designer_process_was_cancelled(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    return p == NULL ? 0 : p->cancelled;
}

long designer_process_truncated(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    return p == NULL ? 0 : p->truncated;
}

void designer_process_free(void* handle) {
    DesignerProcess* p = (DesignerProcess*)handle;
    if (p == NULL) return;
    if (!p->finished) {
        kill(p->pid, SIGKILL);
        waitpid(p->pid, NULL, 0);
    }
    if (p->in_fd >= 0) close(p->in_fd);
    if (p->out_fd >= 0) close(p->out_fd);
    if (p->err_fd >= 0) close(p->err_fd);
    free(p->out);
    free(p->err);
    free(p);
}
