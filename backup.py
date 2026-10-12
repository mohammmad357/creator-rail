"""Local snapshot backup/restore; refuses overwriting an existing destination."""
import argparse
import shutil
import sqlite3
from pathlib import Path
from app import Database

p=argparse.ArgumentParser()
p.add_argument('operation',choices=['backup','restore'])
p.add_argument('source',type=Path)
p.add_argument('destination',type=Path)
a=p.parse_args()
if not a.source.is_file() or a.destination.exists():
    p.error('Source must exist; destination must not exist')
with sqlite3.connect(a.source) as source:
    if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
        p.error('Source integrity check failed')
if a.operation=='backup':
    Database(str(a.source)).backup(str(a.destination))
else:
    shutil.copyfile(a.source,a.destination)
print('Snapshot '+a.operation+' completed. No wallet keys are stored in this database.')
