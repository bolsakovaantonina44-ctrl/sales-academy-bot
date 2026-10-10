"""Online SQLite snapshot and offline restore to a NEW path, never overwrite data.

python -m ops.backup snapshot /data/academy.sqlite3 /data/backups/DATE.sqlite3
python -m ops.backup verify /data/backups/DATE.sqlite3
python -m ops.backup restore BACKUP NEW_PATH
Copy snapshots off the volume separately; a same-volume copy is not disaster recovery.
"""
import argparse
import os
import sqlite3
from pathlib import Path


def verify(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup integrity check failed')
        db.execute('SELECT COUNT(*) FROM sessions').fetchone()


def snapshot(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise ValueError('Source database is missing')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation makes accidental overwrite impossible.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
        verify(destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('action', choices=['snapshot', 'verify', 'restore'])
    parser.add_argument('source')
    parser.add_argument('destination', nargs='?')
    args = parser.parse_args()
    if args.action == 'verify':
        verify(args.source)
    else:
        if not args.destination:
            parser.error('destination is required')
        if args.action == 'restore':
            verify(args.source)
        snapshot(args.source, args.destination)
    print('OK: ' + args.action)


if __name__ == '__main__':
    main()
