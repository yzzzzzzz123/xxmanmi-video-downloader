"""Read bound cover evidence from live work or a verified cleanup archive."""
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path


def cover_receipt_bytes(project, batch_id, batch):
    project = Path(project).resolve()
    verification = batch.get('verification') or {}
    digest = verification.get('cover_receipt_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('Missing bound cover receipt hash')
    supplied = verification.get('cover_receipt')
    recorded_work = batch.get('work')
    if not isinstance(supplied, str) or not isinstance(recorded_work, str):
        raise ValueError('Missing bound cover receipt path')
    path, work = Path(supplied), Path(recorded_work).resolve()
    if (path.is_symlink() or not work.is_relative_to(project)
            or work == project or path.resolve() != work / f'{batch_id}-covers.json'
            or (verification.get('work') is not None
                and Path(verification['work']).resolve() != work)):
        raise ValueError('Cover receipt outside its bound collection workspace')
    if path.is_file():
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Oversized cover receipt')
        data = path.read_bytes()
    else:
        owner = batch.get('thread')
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,119}', owner):
            raise ValueError('Invalid archived receipt owner')
        cleanup = project / 'Datebase/xxmanmi-resource-db/.runtime/cleanup'
        index = cleanup / f'work-{owner}.json'
        if index.is_symlink() or not index.is_file() or index.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Missing verified cleanup archive index')
        descriptor = json.loads(index.read_bytes())
        if (descriptor.get('owner') != owner or descriptor.get('verified') is not True
                or descriptor.get('removed') is not True
                or Path(descriptor.get('original_work', '')).resolve() != work):
            raise ValueError('Cleanup archive does not match the bound owner/work')
        archive = Path(descriptor.get('archive', ''))
        archive_root = (cleanup / 'archives').resolve()
        if (archive.is_symlink() or archive.resolve().parent != archive_root
                or not archive.name.startswith(owner + '-') or archive.suffix != '.zip'
                or not archive.is_file()):
            raise ValueError('Cleanup archive outside its approved directory')
        expected_name = f'{batch_id}-covers.json'
        records = [item for item in descriptor.get('files', []) if item.get('name') == expected_name]
        if (len(records) != 1 or records[0].get('sha256') != digest.lower()
                or type(records[0].get('size')) is not int
                or not 0 < records[0]['size'] <= 4 * 1024 * 1024):
            raise ValueError('Cleanup archive receipt index differs from verification')
        with zipfile.ZipFile(archive) as bundle:
            entries = [item for item in bundle.infolist() if item.filename == expected_name]
            if (len(entries) != 1 or entries[0].file_size != records[0]['size']
                    or entries[0].is_dir()
                    or stat.S_ISLNK(entries[0].external_attr >> 16)):
                raise ValueError('Cleanup archive receipt member is invalid')
            data = bundle.read(entries[0])
    if hashlib.sha256(data).hexdigest() != digest.lower():
        raise ValueError('Cover receipt bytes differ from the bound verification hash')
    return data
