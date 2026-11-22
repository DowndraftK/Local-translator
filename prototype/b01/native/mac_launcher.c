#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <Python.h>

#ifndef B01_PYTHON_PATH
#error "Build with the explicit isolated B01 interpreter path."
#endif

int main(int argc, char **argv) {
    char executable[4096], script[8192], python[8192];
    uint32_t length = sizeof(executable);
    if (_NSGetExecutablePath(executable, &length) != 0) return 2;
    char *slash = strrchr(executable, '/');
    if (!slash) return 2;
    *slash = '\0';
    if (snprintf(script, sizeof(script), "%s/../Resources/B01/launch.py", executable) >= sizeof(script)) return 2;
    char **arguments = calloc((size_t)argc + 4, sizeof(char *));
    if (!arguments) return 2;
#ifdef B01_BUNDLED_RUNTIME
    if (snprintf(python, sizeof(python), "%s/../Resources/Runtime/gui/Python/bin/python3.12", executable) >= sizeof(python)) {
        free(arguments);
        return 2;
    }
    arguments[0] = python;
#else
    arguments[0] = B01_PYTHON_PATH;
#endif
    arguments[1] = "-I"; arguments[2] = "-B"; arguments[3] = script;
    for (int i = 1; i < argc; ++i) arguments[i + 3] = argv[i];
    // Keep the signed .app executable as the process's native executable so
    // NSBundle/LaunchServices/AX retain the preview's own desktop identity.
    // Python still uses the explicit isolated development venv as argv[0].
    int result = Py_BytesMain(argc + 3, arguments);
    free(arguments);
    return result;
}
