#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

extern long designer_editor_launch(const char* editor, const char* path, long line);
extern long designer_reveal_file(const char* path);

static int wait_for_record(const char* path, const char* expected) {
    char actual[8192] = {0};
    for (int attempt = 0; attempt < 200; attempt++) {
        FILE* input = fopen(path, "r");
        if (input != NULL) {
            size_t amount = fread(actual, 1, sizeof(actual) - 1, input);
            fclose(input);
            if (amount > 0) return strcmp(actual, expected) == 0;
        }
        struct timespec pause = {0, 10000000};
        nanosleep(&pause, NULL);
    }
    return 0;
}

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    char output[] = "/tmp/elisa-ide-editor-argv-XXXXXX";
    int descriptor = mkstemp(output);
    if (descriptor < 0) return 3;
    close(descriptor);
    unlink(output);
    if (setenv("ELISA_IDE_EDITOR_TEST_OUTPUT", output, 1) != 0) return 4;

    char reveal_directory[1024];
    if (snprintf(reveal_directory, sizeof(reveal_directory), "/tmp/elisa-ide-reveal-test-%ld", (long)getpid()) >= (int)sizeof(reveal_directory)) return 7;
    if (mkdir(reveal_directory, 0700) != 0) return 7;
    char open_link[1024];
    if (snprintf(open_link, sizeof(open_link), "%s/open", reveal_directory) >= (int)sizeof(open_link)) return 8;
    if (symlink(argv[1], open_link) != 0) return 9;
    const char* existing_path = getenv("PATH");
    char search_path[8192];
    if (snprintf(search_path, sizeof(search_path), "%s:%s", reveal_directory,
                 existing_path == NULL ? "" : existing_path) >= (int)sizeof(search_path)) return 10;
    if (setenv("PATH", search_path, 1) != 0) return 11;

    const char* source_path = "/tmp/Elisa IDE test/src/main handlers.elisa";
    long launch = designer_editor_launch(argv[1], source_path, 37);
    if (launch != 0) {
        fprintf(stderr, "editor launch failed: %ld\n", launch);
        unlink(output);
        return 5;
    }
    const char expected_editor[] = "[\"/tmp/Elisa IDE test/src/main handlers.elisa\", \"37\"]\n";
    int editor_args_ok = wait_for_record(output, expected_editor);
    unlink(output);

    long reveal = designer_reveal_file(source_path);
    if (reveal != 0) {
        fprintf(stderr, "file reveal launch failed: %ld\n", reveal);
        unlink(output);
        unlink(open_link);
        rmdir(reveal_directory);
        return 12;
    }
    const char expected_reveal[] = "[\"-R\", \"/tmp/Elisa IDE test/src/main handlers.elisa\"]\n";
    int reveal_args_ok = wait_for_record(output, expected_reveal);
    unlink(output);
    unlink(open_link);
    rmdir(reveal_directory);
    if (!editor_args_ok) {
        fprintf(stderr, "editor did not receive file and line as separate argv entries\n");
        return 6;
    }
    if (!reveal_args_ok) {
        fprintf(stderr, "filesystem reveal did not receive the file path as one argv entry\n");
        return 6;
    }
    puts("test external_editor_launch_and_reveal: ok");
    return 0;
}
