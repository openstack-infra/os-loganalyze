#!/usr/bin/env python
#
# Copyright (c) 2013 IBM Corp.
# Copyright (c) 2014 Hewlett-Packard Development Company, L.P.
# Copyright (c) 2014 Rackspace Australia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import collections
import fileinput
import os.path
import re
import sys
import types
import wsgiref.util
import zlib

import os_loganalyze.util as util

try:
    import swiftclient
except ImportError:
    pass


class UnsafePath(Exception):
    pass


class NoSuchFile(Exception):
    pass


def does_file_exist(fname):
    """Figure out if we'll be able to read this file.

    Because we are handling the file streams as generators, we actually raise
    an exception too late for us to be able to handle it before apache has
    completely control. This attempts to do the same open outside of the
    generator to trigger the IOError early enough for us to catch it, without
    completely changing the logic flow, as we really want the generator
    pipeline for performance reasons.

    This does open us up to a small chance for a race where the file comes
    or goes between this call and the next, however that is a vanishingly
    small possibility.
    """
    try:
        f = open(fname)
        f.close()
        return True
    except IOError:
        return False


def log_name(environ):
    path = wsgiref.util.request_uri(environ, include_query=0)
    match = re.search('htmlify/(.*)', path)
    if match:
        raw = match.groups(1)[0]
        return raw

    return None


def safe_path(root, log_name):
    """Pull out a safe path from a url.

    Basically we need to ensure that the final computed path
    remains under the root path. If not, we return None to indicate
    that we are very sad.
    """
    if log_name:
        newpath = os.path.abspath(os.path.join(root, log_name))
        if newpath.find(root) == 0:
            return newpath

    return None


def _get_swift_connection(swift_config):
    # TODO(jhesketh): refactor the generator into a class so we can keep a
    # persistent connection. For now, emulate a static variable on this method
    # called 'con'.
    if not _get_swift_connection.con:
        _get_swift_connection.con = swiftclient.client.Connection(
            authurl=swift_config['authurl'],
            user=swift_config['user'],
            key=swift_config['password'],
            os_options={'region_name': swift_config['region']},
            tenant_name=swift_config['tenant'],
            auth_version=2.0
        )
    return _get_swift_connection.con
_get_swift_connection.con = None


class SwiftIterableBuffer(collections.Iterable):
    file_headers = {}

    def __init__(self, logname, config):
        self.logname = logname
        self.resp_headers = {}
        self.obj = None
        self.file_headers['filename'] = logname

        if not config.has_section('swift'):
            sys.stderr.write('Not configured to use swift..\n')
            sys.stderr.write('logname: %s\n' % logname)
        else:
            try:
                swift_config = dict(config.items('swift'))
                # NOTE(jhesketh): While _get_siwft_connection seems like it
                # should be part of this class we actually still need it
                # outside to maintain the connection across multiple objects.
                # Each SwiftIterableBuffer is a new object request, not
                # necessarily a new swift connection (hopefully we can reuse
                # connections). I think the place to put the get connection
                # in the future would be in the server.py (todo).
                con = _get_swift_connection(swift_config)

                chunk_size = int(swift_config.get('chunk_size', 64))
                if chunk_size < 1:
                    chunk_size = None

                self.resp_headers, self.obj = con.get_object(
                    swift_config['container'], logname,
                    resp_chunk_size=chunk_size)
                self.file_headers.update(self.resp_headers)
            except Exception as e:
                # Only print the traceback if the error was anything but a
                # 404. File not found errors are handled separately.
                if 'http_status' not in dir(e) or e.http_status != 404:
                    import traceback
                    sys.stderr.write("Error fetching from swift.\n")
                    sys.stderr.write('logname: %s\n' % logname)
                    traceback.print_exc()

    def __iter__(self):
        ext = os.path.splitext(self.logname)[1]
        if ext == '.gz':
            # Set up a decompression object assuming the deflate
            # compression algorithm was used
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)

        if isinstance(self.obj, types.GeneratorType):
            buf = next(self.obj)
            partial = ''
            while buf:
                if ext == '.gz':
                    string = partial + d.decompress(buf)
                else:
                    string = partial + buf
                split = string.split('\n')
                for line in split[:-1]:
                    yield line + '\n'
                partial = split[-1]
                try:
                    buf = next(self.obj)
                except StopIteration:
                    break
            if partial != '':
                yield partial
        else:
            output = self.obj
            if ext == '.gz':
                output = d.decompress(output)

            split = output.split('\n')
            for line in split[:-1]:
                yield line + '\n'
            partial = split[-1]
            if partial != '':
                yield partial


class DiskIterableBuffer(collections.Iterable):
    file_headers = {}

    def __init__(self, logname, logpath, config):
        self.logname = logname
        self.logpath = logpath
        self.resp_headers = {}
        self.obj = fileinput.FileInput(self.logpath,
                                       openhook=fileinput.hook_compressed)
        self.file_headers['filename'] = logname
        self.file_headers.update(util.get_headers_for_file(logpath))

    def __iter__(self):
        return self.obj


def get_file_generator(environ, root_path, config=None):
    logname = log_name(environ)
    logpath = safe_path(root_path, logname)
    file_headers = {}
    if not logpath:
        raise UnsafePath()
    file_headers['filename'] = os.path.basename(logpath)

    file_generator = None
    # if we want swift only, we'll skip processing files
    use_files = (util.parse_param(environ, 'source', default='all')
                 != 'swift')
    if use_files and does_file_exist(logpath):
        file_generator = DiskIterableBuffer(logname, logpath, config)
    else:
        # NOTE(jhesketh): If the requested URL ends in a trailing slash we
        # assume that this is meaning to load an index.html from our pseudo
        # filesystem. This means we should't store objects with a trailing
        # slash in their name as os-loganalzye won't load them.
        if logname[-1] == '/':
            file_generator = SwiftIterableBuffer(
                os.path.join(logname, 'index.html'), config)
            if not file_generator.obj:
                # Maybe our assumption was wrong, lets go back to trying the
                # original object name.
                file_generator = SwiftIterableBuffer(logname, config)
        else:
            file_generator = SwiftIterableBuffer(logname, config)
            if not file_generator.obj:
                # The object doesn't exist. Try again appending index.html
                logname = os.path.join(logname, 'index.html')
                file_generator = SwiftIterableBuffer(logname, config)

    if not file_generator.obj:
        raise NoSuchFile()

    return file_generator
