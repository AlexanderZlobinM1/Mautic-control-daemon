"""Path-checked tar extraction shared by generic artifact delivery."""
from pathlib import Path
import tarfile


def safe_extract_tar(archive: tarfile.TarFile, destination: Path, *, allow_links: bool = False, skip_prefixes: tuple[str, ...] = ()) -> None:
    root = destination.resolve()
    members = []
    for member in archive.getmembers():
        normalized = member.name.replace('\\', '/')
        while normalized.startswith('./'):
            normalized = normalized[2:]
        if any(normalized == prefix or normalized.startswith(prefix + '/') for prefix in skip_prefixes):
            continue
        target = (destination / member.name).resolve()
        if root != target and root not in target.parents:
            raise RuntimeError(f'unsafe path in archive: {member.name}')
        if member.issym() or member.islnk():
            if not allow_links:
                raise RuntimeError(f'links are not allowed in artifact bundle: {member.name}')
            base = target.parent if member.issym() else root
            link_target = (base / member.linkname).resolve()
            if root != link_target and root not in link_target.parents:
                raise RuntimeError(f'unsafe link in archive: {member.name}')
        elif not (member.isfile() or member.isdir()):
            raise RuntimeError(f'unsupported archive member type: {member.name}')
        members.append(member)
    archive.extractall(destination, members=members)
