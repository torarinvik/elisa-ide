#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

extern long designer_host_ipc_paths_create(char* request_path, size_t request_capacity,
                                           char* reply_path, size_t reply_capacity);
extern long designer_host_ipc_paths_cleanup(const char* request_path, const char* reply_path);

static int directory_for(const char* path, char* output, size_t capacity) {
    const size_t length = strnlen(path, capacity);
    if (length == 0 || length >= capacity) return 0;
    memcpy(output, path, length + 1);
    char* slash = strrchr(output, '/');
    if (slash == NULL) return 0;
    *slash = '\0';
    return 1;
}

static int create_empty_file(const char* path) {
    const int descriptor = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (descriptor < 0) return 0;
    return close(descriptor) == 0;
}

static int exists(const char* path) {
    struct stat status;
    return lstat(path, &status) == 0;
}

int main(void) {
    char first_request[4096];
    char first_reply[4096];
    char second_request[4096];
    char second_reply[4096];
    char first_directory[4096];
    char second_directory[4096];
    struct stat directory_status;

    if (designer_host_ipc_paths_create(first_request, sizeof(first_request),
                                       first_reply, sizeof(first_reply)) != 0 ||
        designer_host_ipc_paths_create(second_request, sizeof(second_request),
                                       second_reply, sizeof(second_reply)) != 0) {
        fputs("could not create private host IPC paths\n", stderr);
        return 1;
    }
    if (strcmp(first_request, second_request) == 0 ||
        !directory_for(first_request, first_directory, sizeof(first_directory)) ||
        !directory_for(second_request, second_directory, sizeof(second_directory)) ||
        strcmp(first_directory, second_directory) == 0) {
        fputs("host sessions did not receive distinct IPC directories\n", stderr);
        return 2;
    }
    if (stat(first_directory, &directory_status) != 0 || !S_ISDIR(directory_status.st_mode) ||
        (directory_status.st_mode & 077) != 0 || directory_status.st_uid != geteuid()) {
        fputs("host IPC directory is not private to the current user\n", stderr);
        return 3;
    }
    if (!create_empty_file(first_request) || !create_empty_file(first_reply) ||
        !create_empty_file(second_request) || !create_empty_file(second_reply)) {
        fputs("could not create IPC fixture files\n", stderr);
        return 4;
    }

    if (designer_host_ipc_paths_cleanup(first_request, second_reply) >= 0 ||
        !exists(first_request) || !exists(first_reply) ||
        !exists(second_request) || !exists(second_reply)) {
        fputs("mismatched IPC paths were accepted or changed\n", stderr);
        return 5;
    }
    if (designer_host_ipc_paths_cleanup(first_request, first_reply) != 0 ||
        exists(first_directory) || exists(first_request) || exists(first_reply) ||
        designer_host_ipc_paths_cleanup(first_request, first_reply) != 0) {
        fputs("owned IPC paths were not removed safely and idempotently\n", stderr);
        return 6;
    }
    if (designer_host_ipc_paths_cleanup(second_request, second_reply) != 0 ||
        exists(second_directory) || exists(second_request) || exists(second_reply)) {
        fputs("second session IPC paths were not cleaned up\n", stderr);
        return 7;
    }

    puts("test host_ipc_paths_private_and_scoped: ok");
    return 0;
}
