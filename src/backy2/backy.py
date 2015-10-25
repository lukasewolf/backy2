# -*- encoding: utf-8 -*-

from prettytable import PrettyTable
import argparse
#import configparser
import glob
import datetime
import fileinput
import math
import hashlib
import logging
import json
#import random
import sqlite3
import uuid
import os
import sys


logger = logging.getLogger(__name__)

BLOCK_SIZE = 1024*4096  # 4MB
HASH_FUNCTION = hashlib.sha512

def init_logging(logdir, console_level):  # pragma: no cover
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(levelname)8s: %(message)s')),
    console.setLevel(console_level)
    #logger.addHandler(console)

    logfile = logging.FileHandler(os.path.join(logdir, 'backy.log'))
    logfile.setLevel(logging.INFO)
    logfile.setFormatter(logging.Formatter('%(asctime)s [%(process)d] %(message)s')),
    #logger.addHandler(logfile)

    logging.basicConfig(handlers = [console, logfile], level=logging.DEBUG)

    logger.info('$ ' + ' '.join(sys.argv))



def hints_from_rbd_diff(rbd_diff):
    """ Return the required offset:length tuples from a rbd json diff
    """
    data = json.loads(rbd_diff)
    return [(l['offset'], l['length'], True if l['exists']=='true' else False) for l in data]


def blocks_from_hints(hints, block_size):
    """ Helper method """
    blocks = set()
    for offset, length, exists in hints:
        start_block = offset // block_size  # integer division
        end_block = start_block + (length-1) // block_size
        for i in range(start_block, end_block+1):
            blocks.add(i)
    return blocks


def makedirs(path):
    try:
        os.makedirs(path)
    except FileExistsError:
        pass


class MetaBackend():
    """ Holds meta data """

    def __init__(self, path):
        self.path = path


    def create_version(self, version_name, size, size_bytes):
        """ Creates a new version with a given name.
        size is the number of blocks this version will contain.
        Returns a uid for this version.
        """
        raise NotImplementedError()


    def get_version(self, uid):
        """ Returns a version as a dict """
        raise NotImplementedError()


    def get_versions(self):
        """ Returns a list of all versions """
        raise NotImplementedError()


    def set_block(self, id, version_uid, block_uid, checksum, size, _commit=True):
        """ Set a block to <id> for a version's uid (which must exist) and
        store it's uid (which points to the data BLOB).
        checksum is the block's checksum
        size is the block's size
        _commit is a hint if the transaction should be committed immediately.
        """
        raise NotImplementedError()


    def get_block(self, block_uid):
        """ Get a dict of a single block """
        raise NotImplementedError()


    def get_blocks_by_version(self, version_uid):
        """ Returns an ordered (by id asc) list of blocks for a version uid """
        raise NotImplementedError()


    def close(self):
        pass


class DataBackend():
    """ Holds BLOBs, never overwrites
    """

    def __init__(self, path):
        self.path = path


    def save(self, data):
        """ Saves data, returns unique ID """
        raise NotImplementedError()


    def read(self, uid):
        """ Returns b'<data>' or raises FileNotFoundError """
        raise NotImplementedError()


    def rm(self, uid):
        """ Deletes a block """
        raise NotImplementedError()


    def close(self):
        pass


class SQLiteBackend(MetaBackend):
    """ Stores meta data in a sqlite database """

    DBFILENAME = 'backy.sqlite'

    def __init__(self, path):
        MetaBackend.__init__(self, path)
        dbpath = os.path.join(self.path, self.DBFILENAME)

        def dict_factory(cursor, row):
            """ A row factory for sqlite3 which emulates a dict cursor. """
            d = {}
            for idx, col in enumerate(cursor.description):
                d[col[0]] = row[idx]
            return d

        self.conn = sqlite3.connect(dbpath)
        self.conn.row_factory = dict_factory
        self.cursor = self.conn.cursor()
        self._create()


    def _uid(self):
        return str(uuid.uuid1())


    def _now(self):
        """ Returns datetime as isoformat (ex. 2015-10-25T10:43:03.823777+00:00) """
        return datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()


    def _create(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS versions
             (uid text, date text, name text, size integer, size_bytes integer)''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS blocks (
             uid text,
             version_uid text,
             id integer,
             date text,
             checksum text,
             size integer,
             FOREIGN KEY(version_uid) REFERENCES versions(uid),
             PRIMARY KEY(version_uid, id)
             )''')
        self.cursor.execute('''
            CREATE INDEX IF NOT EXISTS block_uid on blocks(uid)
            ''')
        self.conn.commit()


    def create_version(self, version_name, size, size_bytes):
        uid = self._uid()
        now = self._now()
        self.cursor.execute('''
            INSERT INTO versions (uid, date, name, size, size_bytes) VALUES (?, ?, ?, ?, ?)
            ''', (uid, now, version_name, size, size_bytes))
        self.conn.commit()
        return uid


    def get_version(self, uid):
        self.cursor.execute('''
            SELECT uid, date, name, size, size_bytes FROM versions WHERE uid=?
            ''', (uid,))
        version = self.cursor.fetchone()
        if version is None:
            # not found
            raise KeyError('Version {} not found.'.format(uid))
        return version


    def get_versions(self):
        self.cursor.execute('''
            SELECT uid, date, name, size, size_bytes FROM versions ORDER BY name asc, date asc
            ''')
        versions = self.cursor.fetchall()
        return versions


    def set_block(self, id, version_uid, block_uid, checksum, size, _commit=True):
        now = self._now()
        self.cursor.execute('''
            INSERT OR REPLACE INTO blocks (uid, version_uid, id, date, checksum, size) VALUES (?, ?, ?, ?, ?, ?)
            ''', (block_uid, version_uid, id, now, checksum, size))
        if _commit:
            self.conn.commit()


    def _commit(self):
        self.conn.commit()


    def get_block(self, block_uid):
        self.cursor.execute('''
            SELECT uid, version_uid, id, date, checksum, size FROM blocks WHERE uid=?
            ''', (block_uid,))
        block = self.cursor.fetchone()
        if block is None:
            # not found
            raise KeyError('Block {} not found.'.format(block_uid))
        return block


    def get_block_by_checksum(self, checksum):
        self.cursor.execute('''
            SELECT uid, version_uid, id, date, checksum, size FROM blocks WHERE checksum=?
            ''', (checksum,))
        block = self.cursor.fetchone()
        return block  # None if nothing found


    def get_blocks_by_version(self, version_uid):
        self.cursor.execute('''
            SELECT uid, version_uid, id, date, checksum, size FROM blocks
            WHERE version_uid=? ORDER BY id ASC
            ''', (version_uid,))
        blocks = self.cursor.fetchall()
        #if block is None:
            ## not found
            #raise KeyError('Block {} not found.'.format(uid))
        return blocks


    def close(self):
        self.conn.close()


class FileBackend(DataBackend):
    """ A DataBackend which stores in files. The files are stored in directories
    starting with the bytes of the generated uid. The depth of this structure
    is configurable via the DEPTH parameter, which defaults to 2. """

    DEPTH = 2
    SPLIT = 2

    def _uid(self):
        return uuid.uuid1().hex


    def _path(self, uid):
        """ Returns a generated path (depth = self.DEPTH) from a uid.
        Example uid=831bde887afc11e5b45aa44e314f9270 and depth=2, then
        it returns "83/1b".
        If depth is larger than available bytes, then available bytes
        are returned only as path."""

        parts = [uid[i:i+self.SPLIT] for i in range(0, len(uid), self.SPLIT)]
        return os.path.join(*parts[:self.DEPTH])


    def save(self, data):
        uid = self._uid()
        path = os.path.join(self.path, self._path(uid))
        makedirs(path)
        filename = os.path.join(path, uid)
        if os.path.exists(filename):
            raise ValueError('Found a file {} where this is impossible.'.format(filename))
        with open(filename, 'wb') as f:
            r = f.write(data)
            assert r == len(data)
        return uid


    def rm(self, uid):
        path = os.path.join(self.path, self._path(uid))
        filename = os.path.join(path, uid)
        if not os.path.exists(filename):
            raise FileNotFoundError('File {} not found.'.format(filename))
        os.unlink(filename)


    def read(self, uid):
        path = os.path.join(self.path, self._path(uid))
        filename = os.path.join(path, uid)
        if not os.path.exists(filename):
            raise FileNotFoundError('File {} not found.'.format(filename))
        return open(filename, 'rb').read()



class Backy():
    """
    """

    def __init__(self, path, block_size=BLOCK_SIZE):
        self.path = path
        self.datapath = os.path.join(self.path, 'data')
        makedirs(self.datapath)
        self.meta_backend = SQLiteBackend(self.datapath)
        self.data_backend = FileBackend(self.datapath)
        self.block_size = block_size


    def _prepare_version(self, name, size_bytes, from_version_uid=None):
        """ Prepares the metadata for a new version.
        If from_version_uid is given, this is taken as the base, otherwise
        a pure sparse version is created.
        """
        if from_version_uid:
            self.meta_backend.get_version(from_version_uid)  # raise if not exists
            old_blocks = self.meta_backend.get_blocks_by_version(from_version_uid)
        else:
            old_blocks = None
        size = math.ceil(size_bytes / self.block_size)
        version_uid = self.meta_backend.create_version(name, size, size_bytes)
        for id in range(size):
            if old_blocks:
                try:
                    old_block = old_blocks[id]
                except IndexError:
                    uid = None
                    checksum = None
                    block_size = self.block_size
                else:
                    assert old_block['id'] == id
                    uid = old_block['uid']
                    checksum = old_block['checksum']
                    block_size = old_block['size']
            else:
                uid = None
                checksum = None
                block_size = self.block_size

            # the last block can differ in size, so let's check
            _offset = id * self.block_size
            block_size = min(block_size, size_bytes - _offset)

            self.meta_backend.set_block(
                id,
                version_uid,
                uid,
                checksum,
                block_size,
                _commit=False)
        self.meta_backend._commit()
        #logger.info('New version: {}'.format(version_uid))
        return version_uid


    def ls(self):
        versions = self.meta_backend.get_versions()
        tbl = PrettyTable()
        tbl.field_names = ['date', 'name', 'size', 'size_bytes', 'uid']
        for version in versions:
            tbl.add_row([
                version['date'],
                version['name'],
                version['size'],
                version['size_bytes'],
                version['uid'],
                ])
        print(tbl)


    def restore(self, version_uid, target, sparse=True):
        version = self.meta_backend.get_version(version_uid)  # raise if version not exists
        blocks = self.meta_backend.get_blocks_by_version(version_uid)
        with open(target, 'wb') as f:
            for block in blocks:
                f.seek(block['id'] * self.block_size)
                if block['uid']:
                    data = self.data_backend.read(block['uid'])
                    assert len(data) == block['size']
                    data_checksum = HASH_FUNCTION(data).hexdigest()
                    written = f.write(data)
                    assert written == len(data)
                    if data_checksum != block['checksum']:
                        logger.error('Checksum mismatch during restore for block '
                            '{} (is: {} should-be: {}). Block restored is '
                            'invalid. Continuing.'.format(
                                block['id'],
                                data_checksum,
                                block['checksum'],
                                ))
                    else:
                        logger.debug('Restored block {} successfully ({} bytes).'.format(
                            block['id'],
                            block['size'],
                            ))
                elif not sparse:
                    f.write(b'\0'*block['size'])
                    logger.debug('Restored sparse block {} successfully ({} bytes).'.format(
                        block['id'],
                        block['size'],
                        ))
                else:
                    logger.debug('Ignored sparse block {}.'.format(
                        block['id'],
                        ))
            if f.tell() != version['size_bytes']:
                # write last byte with \0, because this can only happen when
                # the last block was left over in sparse mode.
                last_block = blocks[-1]
                f.seek(last_block['id'] * self.block_size + last_block['size'] - 1)
                f.write(b'\0')


    def backup(self, name, source, hints, from_version):
        """ Create a backup from source.
        If hints are given, they must be tuples of (offset, length, exists)
        where offset and length are integers and exists is a boolean. Then, only
        data within hints will be backed up.
        Otherwise, the backup reads source and looks if checksums match with
        the target.
        """
        with open(source, 'rb') as source_file:
            # determine source size
            source_file.seek(0, 2)  # to the end
            source_size = source_file.tell()
            source_file.seek(0)
            size = math.ceil(source_size / self.block_size)

            # Sanity check: check hints for validity, i.e. too high offsets, ...
            if hints:
                max_offset = max([h[0]+h[1] for h in hints])
                if max_offset > source_size:
                    raise ValueError('Hints have higher offsets than source file.')

            if hints:
                sparse_blocks = blocks_from_hints([hint for hint in hints if not hint[2]], self.block_size)
                read_blocks = blocks_from_hints([hint for hint in hints if hint[2]], self.block_size)
                #destroyed_blocks = base_level.get_invalid_chunk_ids()  # always re-read destroyed blocks
                #read_blocks = hinted_blocks.union(destroyed_blocks)
            else:
                sparse_blocks = []
                read_blocks = range(size)
            sparse_blocks = set(sparse_blocks)
            read_blocks = set(read_blocks)

            version_uid = self._prepare_version(name, source_size, from_version)
            blocks = self.meta_backend.get_blocks_by_version(version_uid)

            for block in blocks:
                if block['id'] in read_blocks:
                    source_file.seek(block['id'] * self.block_size)  # TODO: check if seek costs when it's == tell.
                    data = source_file.read(self.block_size)
                    if not data:
                        raise RuntimeError('EOF reached on source when there should be data.')

                    data_checksum = HASH_FUNCTION(data).hexdigest()
                    logger.debug('Read block {} (checksum {})'.format(block['id'], data_checksum))

                    # dedup
                    existing_block = self.meta_backend.get_block_by_checksum(data_checksum)
                    if existing_block and existing_block['size'] == len(data):
                        self.meta_backend.set_block(block['id'], version_uid, existing_block['uid'], data_checksum, len(data))
                        logger.debug('Found existing block for id {} with uid {})'.format
                                (block['id'], existing_block['uid']))
                    else:
                        block_uid = self.data_backend.save(data)
                        self.meta_backend.set_block(block['id'], version_uid, block_uid, data_checksum, len(data))
                        logger.debug('Wrote block {} (checksum {})'.format(block['id'], data_checksum))
                elif block['id'] in sparse_blocks:
                    self.meta_backend.set_block(block['id'], version_uid, None, None, block['size'])
                    logger.debug('Skipping block (sparse) {}'.format(block['id']))
                else:
                    logger.debug('Keeping block {}'.format(block['id']))
        logger.info('New version: {}'.format(version_uid))


    def close(self):
        self.meta_backend.close()
        self.data_backend.close()



class Commands():
    """Proxy between CLI calls and actual backup code."""

    def __init__(self, path):
        self.path = path


    def backup(self, name, source, rbd, from_version):
        backy = Backy(self.path)
        hints = None
        if rbd:
            data = ''.join([line for line in fileinput.input(rbd).readline()])
            hints = hints_from_rbd_diff(data)
        backy.backup(name, source, hints, from_version)


    def restore(self, version_uid, target, sparse):
        backy = Backy(self.path)
        backy.restore(version_uid, target, sparse)


    def scrub(self, backupname, level, source, percentile):
        if level == '':
            level = None  # restore latest
        else:
            level = int(level)
        if percentile:
            percentile = int(percentile)
        backy = Backy(self.path, backupname, block_size=BLOCK_SIZE)
        if source:
            backy.deep_scrub(source, level, percentile)
        else:
            backy.scrub(level)


    def ls(self):
        Backy(self.path).ls()


    def cleanup(self, backupname, keeplevels):
        keeplevels = int(keeplevels)
        if not backupname:
            where = os.path.join(self.path)
            files = glob.glob(where + '/' + '*..index')
            backupnames = [f.split('..')[0].split('/')[-1] for f in files]
        else:
            backupnames = [backupname]
        for backupname in backupnames:
            Backy(self.path, backupname, block_size=BLOCK_SIZE).cleanup(keeplevels)


def main():
    parser = argparse.ArgumentParser(
        description='Backup and restore for block devices.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        '-v', '--verbose', action='store_true', help='verbose output')
    parser.add_argument(
        '-b', '--backupdir', default='.')

    subparsers = parser.add_subparsers()

    # BACKUP
    p = subparsers.add_parser(
        'backup',
        help="Perform a backup.")
    p.add_argument(
        'source',
        help='Source file')
    p.add_argument(
        'name',
        help='Backup name')
    p.add_argument('-r', '--rbd', default=None, help='Hints as rbd json format')
    p.add_argument('-f', '--from-version', default=None, help='Use this version-uid as base')
    p.set_defaults(func='backup')

    # RESTORE
    p = subparsers.add_parser(
        'restore',
        help="Restore a given backup with level to a given target.")
    p.add_argument('-s', '--sparse', action='store_true', help='Write restore file sparse (does not work with legacy devices)')
    p.add_argument('version_uid')
    p.add_argument('target')
    p.set_defaults(func='restore')

    # SCRUB
    p = subparsers.add_parser(
        'scrub',
        help="Scrub a given backup and check for consistency.")
    p.add_argument('-l', '--level', default='')
    p.add_argument('-s', '--source', default=None,
        help="Source, optional. If given, check if source matches backup in addition to checksum tests.")
    p.add_argument('-p', '--percentile', default=100,
        help="Only check PERCENTILE percent of the blocks (value 0..100). Default: 100")
    p.add_argument('backupname')
    p.set_defaults(func='scrub')

    # CLEANUP
    p = subparsers.add_parser(
        'cleanup',
        help="Clean backup levels, only keep given number of newest levels.")
    p.add_argument('-l', '--keeplevels', default='7')
    p.add_argument('backupname', nargs='?', default="")
    p.set_defaults(func='cleanup')

    # LS
    p = subparsers.add_parser(
        'ls',
        help="List existing backups.")
    p.set_defaults(func='ls')

    args = parser.parse_args()

    if not hasattr(args, 'func'):
        parser.print_usage()
        sys.exit(0)

    if args.verbose:
        console_level = logging.DEBUG
    #elif args.func == 'scheduler':
        #console_level = logging.INFO
    else:
        console_level = logging.INFO
    init_logging(args.backupdir, console_level)

    commands = Commands(args.backupdir)
    func = getattr(commands, args.func)

    # Pass over to function
    func_args = dict(args._get_kwargs())
    del func_args['func']
    del func_args['verbose']
    del func_args['backupdir']

    try:
        logger.debug('backup.{0}(**{1!r})'.format(args.func, func_args))
        func(**func_args)
        logger.info('Backy complete.\n')
        sys.exit(0)
    except Exception as e:
        logger.error('Unexpected exception')
        logger.exception(e)
        logger.info('Backy failed.\n')
        sys.exit(1)


if __name__ == '__main__':
    main()
