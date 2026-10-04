/* Establish the IDE's runtime working directory for development app bundles.
 *
 * Host protocol files, workspace recovery, project templates and build
 * scripts are relative to this directory. LaunchServices starts applications
 * with an unrelated working directory, so the product build supplies the
 * checked-out source root as a quoted compile-time default. ELISA_IDE_RUNTIME_ROOT
 * can select a local scratch workspace for isolated live runs and tests. */

#include <errno.h>
#include <stdlib.h>
#include <unistd.h>

#ifndef ELISA_IDE_SOURCE_ROOT
#error "ELISA_IDE_SOURCE_ROOT must be set by scripts/build_ide.sh"
#endif

long designer_ide_prepare_working_directory(void) {
    const char* runtime_root = getenv("ELISA_IDE_RUNTIME_ROOT");
    if (runtime_root == NULL || runtime_root[0] == '\0') runtime_root = ELISA_IDE_SOURCE_ROOT;
    if (chdir(runtime_root) == 0) return 0;
    return -(long)errno;
}
