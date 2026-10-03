/* Establish the checked-out IDE root for development app bundles.
 *
 * Host protocol files, workspace recovery, project templates and build
 * scripts are rooted in the current source checkout. LaunchServices starts
 * applications with an unrelated working directory, so the product build
 * supplies this root as a quoted compile-time string and we change directory
 * before initializing the shell. */

#include <errno.h>
#include <unistd.h>

#ifndef ELISA_IDE_SOURCE_ROOT
#error "ELISA_IDE_SOURCE_ROOT must be set by scripts/build_ide.sh"
#endif

long designer_ide_prepare_working_directory(void) {
    if (chdir(ELISA_IDE_SOURCE_ROOT) == 0) return 0;
    return -(long)errno;
}
