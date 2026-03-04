"""
Genome — peer-to-peer evolutionary code distribution.

Each agent's darkmatter/ package directory is its genome. Agents can serve
their genome as a signed zip and install a peer's genome over their own.

Depends on: security
"""

import hashlib
import io
import os
import pathlib
import re
import zipfile

import darkmatter
from darkmatter.security import sign_genome, verify_genome_signature


def get_package_dir() -> pathlib.Path:
    """Return path to the running darkmatter/ package directory."""
    return pathlib.Path(darkmatter.__file__).parent


def get_genome_version() -> str:
    """Return the genome version string.

    If __genome_version__ is set (installed from peer), returns it.
    Otherwise returns 'stock:{__version__}'.
    """
    if darkmatter.__genome_version__:
        return darkmatter.__genome_version__
    return f"stock:{darkmatter.__version__}"


def build_genome_zip() -> bytes:
    """Zip the darkmatter/ directory, excluding __pycache__ and .pyc files.

    Returns the zip bytes.
    """
    pkg_dir = get_package_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(pkg_dir):
            # Skip __pycache__ directories
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if fname.endswith(".pyc"):
                    continue
                fpath = pathlib.Path(root) / fname
                arcname = str(fpath.relative_to(pkg_dir.parent))
                zf.writestr(arcname, fpath.read_bytes())
    return buf.getvalue()


def hash_bytes(data: bytes) -> str:
    """Return SHA-256 hex digest of data."""
    return hashlib.sha256(data).hexdigest()


def sign_genome_zip(zip_bytes: bytes, private_key_hex: str, genome_version: str) -> str:
    """Sign a genome zip. Returns signature hex.

    Signs (genome_version, sha256_hash) with DOMAIN_GENOME.
    """
    h = hash_bytes(zip_bytes)
    return sign_genome(private_key_hex, genome_version, h)


def verify_and_extract(zip_bytes: bytes, signature_hex: str, public_key_hex: str,
                       genome_version: str, target_dir: pathlib.Path) -> None:
    """Verify signature, check for path traversal, and extract zip.

    Raises ValueError on verification failure or unsafe paths.
    """
    h = hash_bytes(zip_bytes)
    if not verify_genome_signature(public_key_hex, signature_hex, genome_version, h):
        raise ValueError("Genome signature verification failed")

    # Validate all paths before extracting anything
    target_resolved = target_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            dest = (target_dir / info.filename).resolve()
            if not str(dest).startswith(str(target_resolved)):
                raise ValueError(f"Path traversal detected: {info.filename}")

        # Safe to extract
        zf.extractall(target_dir)


def update_init_metadata(init_path: pathlib.Path, genome_version: str,
                         author_agent_id: str, parent_version: str) -> None:
    """Update __genome_version__, __genome_author__, __genome_parent__ in __init__.py."""
    content = init_path.read_text()

    replacements = {
        "__genome_version__": repr(genome_version),
        "__genome_author__": repr(author_agent_id),
        "__genome_parent__": repr(parent_version),
    }
    for var, value in replacements.items():
        pattern = rf"^{var}\s*=\s*.*$"
        replacement = f"{var} = {value}"
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)

    init_path.write_text(content)
