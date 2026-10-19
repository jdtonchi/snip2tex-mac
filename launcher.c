/* Executable of Snip2TeX Engine.app: runs app.py with an embedded Python.
 *
 * The bundle used to start a shell script that exec'd .venv/bin/python, so
 * the running process was a binary outside the bundle with its own (or no)
 * signature. macOS 26 then refused to show the app's menu bar item. Embedding
 * the interpreter keeps the process the bundle's own signed executable.
 *
 * make_app.sh compiles this with PROJECT_DIR, VENV_PYTHON and LOG_FILE set. */
#include <Python.h>
#include <stdio.h>
#include <unistd.h>

#ifndef PROJECT_DIR
#error "build with -DPROJECT_DIR=..."
#endif

int main(void)
{
    if (chdir(PROJECT_DIR) != 0) {
        perror("chdir " PROJECT_DIR);
        return 1;
    }
    freopen(LOG_FILE, "a", stdout);
    freopen(LOG_FILE, "a", stderr);
    setvbuf(stdout, NULL, _IOLBF, 0);

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    /* pointing executable at the venv's python makes Python read its
       pyvenv.cfg, so sys.prefix and site-packages are the venv's */
    PyStatus status = PyConfig_SetBytesString(&config, &config.executable, VENV_PYTHON);
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesString(&config, &config.program_name, VENV_PYTHON);
    char *argv[] = {VENV_PYTHON, "-W", "ignore", PROJECT_DIR "/app.py"};
    if (!PyStatus_Exception(status))
        status = PyConfig_SetBytesArgv(&config, 4, argv);
    if (!PyStatus_Exception(status))
        status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status))
        Py_ExitStatusException(status);
    return Py_RunMain();
}
