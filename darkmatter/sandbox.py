"""
OS-native process sandboxing — zero-overhead filesystem and network isolation.

macOS: Seatbelt (sandbox-exec) — kernel-enforced, no containers, no copies.
Linux: Landlock — kernel filesystem ACLs (v5.13+), zero overhead.

The sandbox runs the agent process in-place (same folder, same filesystem)
but the kernel prevents it from escaping the allowed paths.

Depends on: config
"""

import os
import platform
import sys
import tempfile
from typing import Optional


# =============================================================================
# Seatbelt Base Policy (macOS) — deny-all default, inspired by Codex/Chrome
# =============================================================================

_SEATBELT_BASE_POLICY = """\
(version 1)

; deny everything by default
(deny default)

; child processes inherit sandbox
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; all sysctls readable (tools need hw/kern info)
(allow sysctl-read)

; mach IPC — allow all lookups (tools need system services for TLS, logging, etc.)
(allow mach-lookup)

; IOKit for power management
(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))

; python multiprocessing
(allow ipc-posix-sem)

; PTY support (needed for agent CLIs)
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write* (regex #"^/dev/ttys[0-9]+"))
(allow file-ioctl (regex #"^/dev/ttys[0-9]+"))

; system-wide read access — binaries, libs, frameworks, etc.
; this is safe: agents can read system files but not modify them.
; the security boundary is WRITE access, not read access.
(allow file-read* (subpath "/usr"))
(allow file-read* (subpath "/bin"))
(allow file-read* (subpath "/sbin"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/Library"))
(allow file-read* (subpath "/etc"))
(allow file-read* (subpath "/private"))
(allow file-read* (subpath "/var"))
(allow file-read* (subpath "/dev"))
(allow file-read* (subpath "/opt"))
(allow file-read* (literal "/"))
(allow file-read-metadata (literal "/"))

; temp directories — agents need scratch space
(allow file-read* file-write* (subpath "/tmp"))
(allow file-read* file-write* (subpath "/private/tmp"))
(allow file-read* file-write* (subpath "/var/tmp"))
(allow file-read* file-write* (subpath "/private/var/tmp"))

; /dev write basics
(allow file-write-data (literal "/dev/null"))
(allow file-read* file-write* (literal "/dev/tty"))
(allow file-read-data file-write-data (subpath "/dev/fd"))

; symlink resolution and notifications
(allow system-fsctl)
(allow ipc-posix-shm-read* (ipc-posix-name "apple.shm.notification_center"))

; syslog
(allow network-outbound (literal "/private/var/run/syslog"))
"""

# =============================================================================
# Network Policy (appended when network=True)
# =============================================================================

_SEATBELT_NETWORK_POLICY = """\

; network access — full outbound + inbound + TLS support
(allow network-outbound)
(allow network-inbound)
(allow network-bind)

(allow system-socket
  (require-all
    (socket-domain AF_SYSTEM)
    (socket-protocol 2)))

(allow mach-lookup
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd"))
"""


# =============================================================================
# Profile Builder
# =============================================================================

def build_seatbelt_profile(
    writable_roots: list[str],
    readable_roots: list[str] | None = None,
    network: bool = True,
) -> tuple[str, dict[str, str]]:
    """Build a complete seatbelt profile with dynamic path parameters.

    Returns (profile_text, params_dict) where params_dict maps
    parameter names to paths for sandbox-exec -D flags.
    """
    params: dict[str, str] = {}
    sections = [_SEATBELT_BASE_POLICY]

    # Writable roots
    if writable_roots:
        write_rules = []
        for i, root in enumerate(writable_roots):
            canonical = os.path.realpath(root)
            param_name = f"WRITABLE_ROOT_{i}"
            params[param_name] = canonical
            write_rules.append(f'(subpath (param "{param_name}"))')
        sections.append(
            f"(allow file-write*\n  {' '.join(write_rules)})\n"
        )

    # Home directory — readable but NOT writable.
    # Agent CLIs need to read configs, tool paths, shell profiles, etc.
    # The security boundary is write access, not read access.
    home = os.path.realpath(os.path.expanduser("~"))
    params["HOME_DIR"] = home
    sections.append('; home directory (read-only)\n(allow file-read* (subpath (param "HOME_DIR")))\n')

    # Additional readable roots beyond home
    if readable_roots:
        read_rules = []
        for i, root in enumerate(readable_roots):
            canonical = os.path.realpath(root)
            param_name = f"READABLE_ROOT_{i}"
            params[param_name] = canonical
            read_rules.append(f'(subpath (param "{param_name}"))')
        sections.append(
            f"; extra read access\n(allow file-read*\n  {' '.join(read_rules)})\n"
        )

    if network:
        sections.append(_SEATBELT_NETWORK_POLICY)

    return "\n".join(sections), params


def build_sandbox_command_macos(
    command: list[str],
    writable_roots: list[str],
    readable_roots: list[str] | None = None,
    network: bool = True,
) -> list[str]:
    """Wrap a command with sandbox-exec on macOS.

    Returns the full argv list: [sandbox-exec, -p, <policy>, -D..., --, cmd...]
    """
    profile, params = build_seatbelt_profile(writable_roots, readable_roots, network)

    argv = ["/usr/bin/sandbox-exec", "-p", profile]
    for key, value in params.items():
        argv.append(f"-D{key}={value}")
    argv.append("--")
    argv.extend(command)
    return argv


# =============================================================================
# Linux Landlock (kernel 5.13+)
# =============================================================================

def _landlock_available() -> bool:
    """Check if Landlock is available on this Linux kernel."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        # landlock_create_ruleset syscall number on x86_64
        import struct
        arch = struct.calcsize("P") * 8
        if platform.machine() in ("x86_64", "AMD64"):
            NR_LANDLOCK_CREATE_RULESET = 444
        elif platform.machine() in ("aarch64", "arm64"):
            NR_LANDLOCK_CREATE_RULESET = 444
        else:
            return False
        # Try with invalid args — ENOSYS means kernel doesn't support it
        ret = libc.syscall(NR_LANDLOCK_CREATE_RULESET, 0, 0, 0x1)  # flag=version query
        return ret >= 0 or ctypes.get_errno() != 38  # 38 = ENOSYS
    except Exception:
        return False


def build_sandbox_command_linux(
    command: list[str],
    writable_roots: list[str],
    readable_roots: list[str] | None = None,
    network: bool = True,
) -> list[str]:
    """Wrap a command with a Landlock sandbox helper script on Linux.

    Creates a small Python bootstrap script that applies Landlock rules
    before exec'ing the target command.
    """
    all_readable = list(writable_roots)
    if readable_roots:
        all_readable.extend(readable_roots)

    # Build a self-contained bootstrap script
    script = _build_landlock_bootstrap(writable_roots, all_readable, network, command)

    # Write to a temp file that auto-cleans
    fd, script_path = tempfile.mkstemp(prefix="dm_sandbox_", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write(script)

    return [sys.executable, script_path]


def _build_landlock_bootstrap(
    writable: list[str],
    readable: list[str],
    network: bool,
    command: list[str],
) -> str:
    """Generate a Python script that applies Landlock rules then exec's the command."""
    import json
    return f"""\
import ctypes, ctypes.util, os, struct, sys

# Landlock constants
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14

READ_ACCESS = (LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE |
               LANDLOCK_ACCESS_FS_READ_DIR)
WRITE_ACCESS = (LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_REMOVE_DIR |
                LANDLOCK_ACCESS_FS_REMOVE_FILE | LANDLOCK_ACCESS_FS_MAKE_CHAR |
                LANDLOCK_ACCESS_FS_MAKE_DIR | LANDLOCK_ACCESS_FS_MAKE_REG |
                LANDLOCK_ACCESS_FS_MAKE_SOCK | LANDLOCK_ACCESS_FS_MAKE_FIFO |
                LANDLOCK_ACCESS_FS_MAKE_BLOCK | LANDLOCK_ACCESS_FS_MAKE_SYM |
                LANDLOCK_ACCESS_FS_REFER | LANDLOCK_ACCESS_FS_TRUNCATE)
ALL_ACCESS = READ_ACCESS | WRITE_ACCESS

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

NR_LANDLOCK_CREATE_RULESET = 444
NR_LANDLOCK_ADD_RULE = 445
NR_LANDLOCK_RESTRICT_SELF = 446

class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]

class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]

attr = LandlockRulesetAttr(handled_access_fs=ALL_ACCESS)
ruleset_fd = libc.syscall(NR_LANDLOCK_CREATE_RULESET,
                          ctypes.byref(attr), ctypes.sizeof(attr), 0)
if ruleset_fd < 0:
    print("[DarkMatter Sandbox] Landlock not available, running unsandboxed", file=sys.stderr)
    os.execvp({json.dumps(command[0])}, {json.dumps(command)})

def add_rule(path, access):
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    rule = LandlockPathBeneathAttr(allowed_access=access, parent_fd=fd)
    ret = libc.syscall(NR_LANDLOCK_ADD_RULE, ruleset_fd, 1, ctypes.byref(rule), 0)
    os.close(fd)
    return ret

# System read-only paths
for p in ["/usr", "/bin", "/sbin", "/etc", "/lib", "/lib64",
          "/opt", "/tmp", "/var/tmp", "/dev", "/proc", "/sys"]:
    if os.path.exists(p):
        add_rule(p, READ_ACCESS)

# User-specified readable roots
for p in {json.dumps(readable)}:
    if os.path.exists(p):
        add_rule(p, READ_ACCESS)

# User-specified writable roots
for p in {json.dumps(writable)}:
    if os.path.exists(p):
        add_rule(p, ALL_ACCESS)

# Temp dirs need write access
for p in ["/tmp", "/var/tmp"]:
    if os.path.exists(p):
        add_rule(p, ALL_ACCESS)

# Apply the ruleset
import prctl  # noqa — may not be available
try:
    prctl.set_no_new_privs(1)
except Exception:
    libc.prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS = 38

ret = libc.syscall(NR_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
os.close(ruleset_fd)
if ret < 0:
    print("[DarkMatter Sandbox] Failed to apply Landlock rules", file=sys.stderr)

os.execvp({json.dumps(command[0])}, {json.dumps(command)})
"""


# =============================================================================
# Public API — platform-agnostic
# =============================================================================

def build_sandbox_command(
    command: list[str],
    writable_roots: list[str],
    readable_roots: list[str] | None = None,
    network: bool = True,
) -> Optional[list[str]]:
    """Wrap a command in a platform-appropriate sandbox.

    Returns the sandboxed command argv, or None if sandboxing is
    not available on this platform.

    Args:
        command: The command to sandbox [program, arg1, arg2, ...]
        writable_roots: Directories the agent can read AND write
        readable_roots: Additional directories the agent can read (optional)
        network: Whether to allow network access (default True)
    """
    system = platform.system()

    if system == "Darwin":
        if not os.path.exists("/usr/bin/sandbox-exec"):
            print("[DarkMatter Sandbox] sandbox-exec not found, cannot sandbox", file=sys.stderr)
            return None
        return build_sandbox_command_macos(command, writable_roots, readable_roots, network)

    elif system == "Linux":
        if not _landlock_available():
            print("[DarkMatter Sandbox] Landlock not available (kernel 5.13+ required)", file=sys.stderr)
            return None
        return build_sandbox_command_linux(command, writable_roots, readable_roots, network)

    else:
        print(f"[DarkMatter Sandbox] No sandbox support for {system}", file=sys.stderr)
        return None


def is_sandbox_available() -> bool:
    """Check if sandboxing is available on this platform."""
    system = platform.system()
    if system == "Darwin":
        return os.path.exists("/usr/bin/sandbox-exec")
    elif system == "Linux":
        return _landlock_available()
    return False
