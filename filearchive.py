# Copyright (c) 2010 ActiveState Software Inc. All rights reserved.

import sys
import os
from os import path
import tarfile
import zipfile
from contextlib import closing, contextmanager


#
# Compression routines
#

class PackError(Exception):
    """Error during pack or unpack"""


def unpack_archive(filename, pth='.'):
    """Unpack the archive under ``path``

    Return (unpacked directory path, filetype)
    """
    assert path.isfile(filename), 'not a file: %s' % filename
    assert path.isdir(pth)

    for filetype, implementor in implementors.items():
        if implementor.is_valid(filename):
            with cd(pth):
                return (implementor(filename).extract(), filetype)
    else:
        raise PackError('unknown compression format: ' + filename)


def pack_archive(filename, files, pwd, filetype="tgz"):
    """Pack the given `files` from directory `pwd`

    `filetype` must be one of ["tgz", "tbz2", "zip"]
    """
    assert path.isdir(pwd)
    assert filetype in implementors, 'invalid filetype: %s' % filetype

    if path.exists(filename):
        os.remove(filename)

    with cd(pwd):
        relnames = [path.relpath(file, pwd) for file in files]
        implementors[filetype].pack(relnames, filename)

    return filename



class CompressedFile:

    def __init__(self, filename):
        self.filename = filename

    def extractall_with_single_toplevel(self, f, names):
        """Same as ``extractall`` but ensures a single toplevel directory

        Some compressed archives do not stick to the convension of having a
        single top-level directory. For eg.,
        http://code.google.com/p/grapefruit/issues/detail?id=3

        In such cases, a new toplevel directory corresponding to the name of the
        compressed file (eg: 'grapefruit-0.1a3' if compressed file is named
        'grapefruit-0.1a3.tar.gz') is created and then extraction happens
        *inside* that directory.

        - f:     tarfile/zipefile file object
        - names: List of filenames in the archive

        Return the absolute path to the toplevel directory.
        """
        toplevels = _find_top_level_directories(names, sep='/')

        if len(toplevels) == 0:
            raise PackError('archive is empty')
        elif len(toplevels) > 1:
            toplevel = _archive_basename(self.filename)
            os.mkdir(toplevel)
            with cd(toplevel):
                self._extractall(f)
            return path.abspath(toplevel)
        else:
            self._extractall(f)
            toplevel = path.abspath(toplevels[0])
            assert path.exists(toplevel)
            if not path.isdir(toplevel):
                # eg: http://pypi.python.org/pypi/DeferArgs/0.4
                raise SingleFile('archive has a single file: %s', toplevel)
            return toplevel

    def _extractall(self, f):
        try:
            return f.extractall()
        except (IOError, OSError) as e:
            raise PackError(e)



class ZippedFile(CompressedFile):
    """A zip file"""

    @staticmethod
    def is_valid(filename):
        return zipfile.is_zipfile(filename)

    def extract(self):
        try:
            f = zipfile.ZipFile(self.filename, 'r')
            try:
                return self.extractall_with_single_toplevel(
                    f, f.namelist())
            except OSError as e:
                if e.errno == 17:
                    # http://bugs.python.org/issue6510
                    raise PackError(e)
                # http://bugs.python.org/issue6609
                if sys.platform.startswith('win'):
                    if isinstance(e, WindowsError) and e.winerror == 267:
                        raise PackError('uses Windows special name (%s)' % e)
                raise
            except IOError as e:
                # http://bugs.python.org/issue10447
                if sys.platform == 'win32' and e.errno == 2:
                    raise PackError('reached max path-length: %s' % e)
                raise
            finally:
                f.close()
        except (zipfile.BadZipfile, zipfile.LargeZipFile) as e:
            raise PackError(e)

    @classmethod
    def pack(cls, paths, file):
        raise NotImplementedError('pack: zip files not supported yet')


class TarredFile(CompressedFile):
    """A tar.gz/bz2 file"""

    @classmethod
    def is_valid(cls, filename):
        try:
            with closing(tarfile.open(filename, cls._get_mode())) as f:
                return True
        except tarfile.TarError:
            return False

    def extract(self):
        try:
            f = tarfile.open(self.filename, self._get_mode())
            try:
                _ensure_read_write_access(f)
                return self.extractall_with_single_toplevel(
                    f, f.getnames())
            finally:
                f.close()
        except tarfile.TarError as e:
            raise PackError(e)
        except IOError as e:
            # see http://bugs.python.org/issue6584
            if 'CRC check failed' in str(e):
                raise PackError(e)
            # See github issue #10
            elif e.errno == 22 and "invalid mode ('wb')" in str(e):
                raise PackError(e)
            else:
                raise
        except OSError as e:
            # http://bugs.activestate.com/show_bug.cgi?id=89657
            if sys.platform == 'win32':
                if isinstance(e, WindowsError) and e.winerror == 123:
                    raise PackError(e)
            raise

    @classmethod
    def pack(cls, paths, file):
        f = tarfile.open(file, cls._get_mode('w'))
        try:
            for pth in paths:
                assert path.exists(pth), '"%s" does not exist' % path
                f.add(pth)
        finally:
            f.close()

    def _get_mode(self):
        """Return the mode for this tarfile"""
        raise NotImplementedError()


class GzipTarredFile(TarredFile):
    """A tar.gz2 file"""

    @staticmethod
    def _get_mode(mode='r'):
        assert mode in ['r', 'w']
        return mode + ':gz'


class Bzip2TarredFile(TarredFile):
    """A tar.gz2 file"""

    @staticmethod
    def _get_mode(mode='r'):
        assert mode in ['r', 'w']
        return mode + ':bz2'


implementors = dict(
    zip = ZippedFile,
    tgz = GzipTarredFile,
    bz2 = Bzip2TarredFile)


class MultipleTopLevels(PackError):
    """Can be extracted, but contains multiple top-level dirs"""
class SingleFile(PackError):
    """Contains nothing but a single file. Compressed archived is expected to
    contain one directory
    """

def _ensure_read_write_access(tarfileobj):
    """Ensure that the given tarfile will be readable and writable by the
    user (the client program using this API) after extraction.

    Some tarballs have u-x set on directories or u-w on files. We reset such
    perms here.. so that the extracted files remain accessible for reading
    and deletion as per the user's wish.

    See also: http://bugs.python.org/issue6196
    """
    dir_perm = tarfile.TUREAD | tarfile.TUWRITE | tarfile.TUEXEC
    file_perm = tarfile.TUREAD | tarfile.TUWRITE

    for tarinfo in tarfileobj.getmembers():
        tarinfo.mode |= (dir_perm if tarinfo.isdir() else file_perm)


def _find_top_level_directories(fileslist, sep):
    """Find the distinct first components in the fileslist"""
    toplevels = set()
    for pth in fileslist:
        firstcomponent = pth.split(sep, 1)[0]
        toplevels.add(firstcomponent)
    return list(toplevels)


def _archive_basename(filename):
    """Return a suitable base directory name for the given archive"""
    exts = (
        '.tar.gz',
        '.tgz',
        '.tar.bz2',
        '.bz2',
        '.zip')

    filename = path.basename(filename)

    for ext in exts:
        if filename.endswith(ext):
            return filename[:-len(ext)]
    return filename + '.dir'


@contextmanager
def cd(pth):
    """With context to temporarily change directory"""
    assert path.isdir(existing(pth)), pth

    cwd = os.getcwd()
    os.chdir(pth)
    try:
        yield
    finally:
        os.chdir(cwd)


def existing(pth):
    """Return `pth` after checking it exists"""
    if not path.exists(pth):
        raise IOError('"{0}" does not exist'.format(pth))
    return pth


if __name__ == '__main__':
    from subprocess import check_output
    print("Unpacking %s" % sys.argv[1])
    path, ext = unpack_archive(sys.argv[1])
    print("Extracted at %s as %s" % (path, ext))
    print check_output("file %s; ls -lR %s" % (path, path), shell=True)
