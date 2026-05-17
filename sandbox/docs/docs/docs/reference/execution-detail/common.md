# General Specifications

Unless otherwise specified, all sandbox code is executed in a temporary directory created under `/tmp`. This directory will be destroyed after code execution.

Unless otherwise specified, the code parameter passed to the sandbox will be written to a temporary file in the temporary directory. For Python as an example, the launch command would look like `python /tmp/tmpha4dcl5b/tmpx8k1pnfh.py`.

Files passed through `files` support both absolute and relative paths. Relative paths are based on the aforementioned temporary directory, such as `/tmp/tmpha4dcl5b`.

The file path rules for the `fetch_files` parameter are the same as `files`. For specific usage, refer to the [API Documentation](/docs/api/run-code-run-code-post).

When executing code, the sandbox provides two different isolation modes. Configuration methods can be found [here](/docs/docs/reference/config).

A general code execution flow:

1. Create temporary directory
2. Write files passed through `files`
3. Write code passed through `code` to temporary file
4. Set up environment according to isolation mode
5. Execute compilation commands (if needed)
6. Execute run commands
7. Retrieve files specified by `fetch_files`
8. Clean up environment

## Isolation Modes

### No Isolation

This mode does not impose any restrictions on the executing code process, though for some languages the process uid will be set to non-root. This mode provides the best performance.

### Light Isolation

Light isolation has relatively weaker performance and requires privileged containers to use. It is suitable for situations where the executed code is potentially dangerous or needs to modify system files. The isolation includes:

- cgroups resource limits (CPU, memory)
- Network namespace isolation
- overlayfs + chroot filesystem isolation
- PID namespace isolation
